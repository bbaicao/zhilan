"""关键词路：普通 SQL 词项倒排 + 应用层 BM25（U3）。

索引期维护 tf/长度；查询只读范围内统计及查询词 posting，正文只为候选回读。
SQLite/MySQL 共用分词、门槛和公式；可用正文版本与 SQL 发布指针同事务切换。

**语义对齐老基线**（``<legacy-repo>\\backend\\app\\rag\\bm25.py`` + ``tokenize.py``，只读快照）：

1. **分词**：中文按「单字 + 相邻双字」、英文/数字按整词；标点与空白**切断**相邻关系
   （避免产出跨标点的伪双字）；停用词不产出、也不参与双字组合；
2. **硬门槛** :func:`passes_gate`：有非单字词时要求命中数 ≥ ``MIN_KEYWORD_TERMS`` **且**
   IDF 加权查询词覆盖率 ≥ ``MIN_KEYWORD_COVERAGE``；只剩单字时按单字重叠兜底。
   这是「零重叠诱饵查询不全库召回」的机制（I7 的前提）；
3. **打分**：自研 BM25（``k1=1.2`` / ``b=0.75``）× 加权重叠度比例（单字 0.3、其余 1.0，
   同老基线），再乘**真短语加成** :data:`PHRASE_BONUS`——查询里的连续片段在块正文中**连续
   出现**时加权，于是「共享 2-gram 但短语被打散」的对抗样本**排在真命中之后**；
4. **1 字查询（D-b）**：2-gram 索引里没有 1-gram 词项（选型记录实测 0 命中）⇒ 显式走
   SQL ``LIKE '%字%'`` 兜底，命中给**低分**（:data:`FALLBACK_SCORE`，排在正常关键词命中之后）。

**与老基线的一处有意差异**（有依据，见条目 3.1 证据）：老基线停用词表含「向」，会把
「向量」切成只剩「量」（单字），使产品核心词退化成「谁含 量 字多」。本实现把「向」移出停用词表，
其余停用词逐字保留——「向量」因此能以真双字词参与门槛与打分。
"""

from __future__ import annotations

import logging
import math
from collections import Counter

from sqlalchemy import LargeBinary, and_, cast, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lexical import is_single_char, posting_terms, segments, term_key, tokenize, unpack_postings
from app.models import Chunk, Document, LexicalMaterial, LexicalTerm, Note, active_chunks
from app.pipeline import lexical_index  # noqa: F401 — 注册所有 ORM 正文写入的词项维护。
from app.retrieval.types import (
    KEYWORD_TOP_K,
    Material,
    document_where,
    filter_tags,
    includes_documents,
    includes_notes,
    matches_document,
    matches_note,
    note_chunk_id,
    note_where,
)

log = logging.getLogger(__name__)

# 只有 ready 的文档分块可检索（统筹 §3.8 门禁；`vectorized` 是**独立轴**，不看它）
READY_STATUS = "ready"

# --- 打分常量 -----------------------------------------------------------------

K1 = 1.2
B = 0.75
# 单字在加权重叠度里的权重（双字词 / 英文词按 1.0 计，同老基线）
SINGLE_CHAR_WEIGHT = 0.3
# 硬门槛（默认值取自老基线 RAG_MIN_KEYWORD_TERMS / RAG_MIN_KEYWORD_COVERAGE）
MIN_KEYWORD_TERMS = 1
MIN_KEYWORD_COVERAGE = 0.20
# 查询里只剩单字时的兜底：最多要求几个单字重叠
SINGLE_CHAR_GATE = 3
# 真短语加成：查询片段在正文里连续出现
PHRASE_BONUS = 1.5
# 1 字查询 LIKE 兜底命中给的分数（低分，排在正常关键词命中之后）
FALLBACK_SCORE = 0.05
NOTE_TITLE_WEIGHT = 0.15


def token_weight(token: str) -> float:
    """单字降权，双字词 / 英文词按 1.0 计。"""
    return SINGLE_CHAR_WEIGHT if is_single_char(token) else 1.0


def weighted_overlap(query_tokens: set[str], doc_tokens: set[str]) -> float:
    """两集合的加权重叠度（单字 0.3，双字 / 英文 1.0）。"""
    return sum(token_weight(token) for token in query_tokens & doc_tokens)


def query_weight(query_tokens: set[str]) -> float:
    """查询侧总权重，用于把加权重叠度归一化到 0~1。"""
    return sum(token_weight(token) for token in query_tokens) or 1.0


def token_idf(df: dict[str, int], total_docs: int, token: str) -> float:
    """平滑 IDF（Robertson 形态，``log`` 内 +1 保证恒非负）。

    老基线用 ``max(1.0, log(...) + 1.0)`` 来兜住「小语料下 IDF 为负」的病态情形；
    当前采用 log 内 +1 的平滑公式，通常不等于旧公式的权重；
    注释只说明非负性质，不宣称评分或排序与旧公式等价。
    """
    if total_docs <= 0:
        return 1.0
    docs_with = df.get(token, 0)
    return math.log(1.0 + (total_docs - docs_with + 0.5) / (docs_with + 0.5))


def query_coverage(
    query_terms: list[str], doc_tokens: set[str], df: dict[str, int], total_docs: int
) -> float:
    """IDF 加权查询词覆盖率：命中词的 IDF 权重占查询全部词权重的比例。

    用 IDF 加权而不是数词数——「公司 / 怎么 / 什么」这类到处出现的词权重低，
    即使命中也不该把片段送进候选。
    """
    denominator = sum(token_idf(df, total_docs, token) for token in query_terms)
    if denominator <= 0:
        return 0.0
    covered = sum(token_idf(df, total_docs, token) for token in query_terms if token in doc_tokens)
    return covered / denominator


def corpus_terms(query_tokens: set[str], df: dict[str, int]) -> list[str]:
    """查询里的**非单字词中、确实存在于语料**的那些（``df > 0``）。

    **本实现相对老基线的一处修正（实测驱动）**：老基线把查询里所有非单字词都算进覆盖率
    分母，而这些词在语料里一次都没出现时 IDF 取到**最大值**（``df=0`` 是「最稀有」），
    于是「向量库用什么存储？」这类**问题式查询**会被自己那几个语料里根本不存在的伪双字
    （库用 / 用什 / 什么 / 么存）**稀释到门槛之下 → 召回为 0**（实测：9 条语料下覆盖率
    0.152 < 0.20，真命中文档一条都进不来）。

    语料里不存在的词**对任何文档都不可命中**，把它算进分母只会惩罚所有候选；
    故覆盖率只用「语料里存在」的词计算。这**不放宽**门槛——真正决定「有没有命中」的仍是
    非单字词命中数与覆盖率，只是分母不再被 OOV 噪声灌水。
    """
    return [token for token in query_tokens if not is_single_char(token) and df.get(token, 0) > 0]


def passes_gate(
    query_tokens: set[str], doc_tokens: set[str], df: dict[str, int], total_docs: int
) -> bool:
    """关键词硬门槛（老基线 ``passes_keyword_gate`` 的口径 + 上面那条 OOV 修正）。

    - 查询里有非单字词：至少命中 ``MIN_KEYWORD_TERMS`` 个，且覆盖率 ≥ ``MIN_KEYWORD_COVERAGE``；
      非单字词**在语料里一个都不存在**（OOV / 打字错，如「量子纠缠」）⇒ 直接不召回
      （这正是零重叠诱饵查询落到拒答的机制，I7）；
    - 查询里只剩单字（极短中文查询）：最多要求 3 个单字重叠兜底，不把短查询一律误杀。
    """
    multi = corpus_terms(query_tokens, df)
    if multi:
        covered = [token for token in multi if token in doc_tokens]
        if len(covered) < max(1, MIN_KEYWORD_TERMS):
            return False
        return query_coverage(multi, doc_tokens, df, total_docs) >= MIN_KEYWORD_COVERAGE
    if any(not is_single_char(token) for token in query_tokens):
        # 查询里有非单字词，但语料里一个都不存在 ⇒ 没有任何可命中的依据
        return False

    singles = [token for token in query_tokens if is_single_char(token)]
    covered_singles = [token for token in singles if token in doc_tokens]
    need = max(1, min(SINGLE_CHAR_GATE, len(singles)))
    return len(covered_singles) >= need


def _bm25_raw(
    query_terms: set[str],
    term_freqs: dict[str, int],
    doc_len: int,
    avg_len: float,
    df: dict[str, int],
    total_docs: int,
) -> float:
    """BM25（k1 / b）原始分；查询词按**集合**去重（老基线 ``get_scores(set(tokens))`` 同口径）。"""
    if avg_len <= 0:
        return 0.0
    norm = K1 * (1.0 - B + B * (doc_len / avg_len))
    score = 0.0
    for token in query_terms:
        freq = term_freqs.get(token, 0)
        if freq <= 0:
            continue
        score += token_idf(df, total_docs, token) * (freq * (K1 + 1.0)) / (freq + norm)
    return score


def _has_phrase(text: str, segs: list[str]) -> bool:
    """查询的某个连续片段是否在正文里**连续出现**（真短语语义）。"""
    if not segs:
        return False
    lowered = (text or "").lower()
    return any(seg in lowered for seg in segs)


def _escape_like(text: str) -> str:
    """转义 ``LIKE`` 的通配符（1 字查询可能是 ``%`` / ``_``）。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _load_corpus(session: AsyncSession, filters: dict | None) -> list:
    """取范围内的**可检索**分块行（含文档侧字段）。

    过滤：``documents.deleted_at IS NULL``（软删只置标记、**向量不删**，两路都必须滤）、
    ``index_status == 'ready'``；``kb_id`` / ``category`` / 时间区间**下推 SQL**，
    ``tags`` 等其余维度由 :func:`matches_document` 在 Python 侧收口。
    """
    stmt = (
        select(
            Chunk.id,
            Chunk.document_id,
            Chunk.content,
            Chunk.ordinal,
            Chunk.page,
            Document.filename,
            Document.category,
            Document.kb_id,
            Document.tags,
            Document.created_at,
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Document.deleted_at.is_(None),
            active_chunks(),
            Document.index_status == READY_STATUS,
            *document_where(filters),
        )
        .order_by(Chunk.id)
    )
    rows = (await session.execute(stmt)).all()
    return [
        row
        for row in rows
        if matches_document(
            filters,
            kb_id=row[7],
            category=row[6],
            tags=row[8],
            created_at=row[9],
        )
    ]


def _row_to_material(row, *, score: float) -> Material:
    return Material(
        chunk_id=int(row[0]),
        document_id=int(row[1]),
        content=row[2] or "",
        block_ordinal=int(row[3] or 0),
        page=int(row[4]) if row[4] is not None else None,
        filename=row[5] or "",
        category=row[6] or "",
        kb_id=int(row[7] or 1),
        keyword_score=score,
    )


async def _load_note_corpus(session: AsyncSession, filters: dict | None) -> list:
    """取范围内的**非空**笔记行（阶段 4 · 条目 4.5.2；``回归修复`` 的 B-lite）。

    与 :func:`_load_corpus` 同构：``notes.deleted_at IS NULL``（软删只置标记，必须滤）、
    ``kb_id`` / ``category`` / 时间区间**下推 SQL**，``tags`` 由 :func:`matches_note`
    在 Python 侧收口。**正文为空的笔记不进语料**——老基线 ``bm25.py:120`` 的
    ``if content and content.strip()`` 同款判据：空正文对 BM25 只是拉低平均长度、白占名额。

    ``Note.content`` 在 v2 是清洗后正文（frontmatter 已在保存时剥离），与建边的扫描口径同源。
    """
    stmt = (
        select(
            Note.id,
            Note.title,
            Note.content,
            Note.category,
            Note.kb_id,
            Note.tags,
            Note.created_at,
        )
        .where(Note.deleted_at.is_(None), *note_where(filters))
        .order_by(Note.id)
    )
    rows = (await session.execute(stmt)).all()
    return [
        row
        for row in rows
        if (row[2] or "").strip()
        and matches_note(
            filters,
            kb_id=row[4],
            category=row[3],
            tags=row[5],
            created_at=row[6],
        )
    ]


def _note_to_material(row, *, score: float) -> Material:
    """笔记行 → 材料。

    字段落点（**每一处都有下游依赖，改前先看**）：

    - ``chunk_id`` = :func:`note_chunk_id`（**负数**）——融合的同源去重键，见该函数的理由；
    - ``document_id`` = ``0``：笔记没有 ``chunks`` 行，取 0 让
      ``generation.citation.attribution_rows`` 的 ``document_id <= 0`` 判据成立
      （否则会往 ``message_attributions.chunk_id`` 外键里塞假行、连带丢掉整轮文档归因）；
    - ``filename`` = 笔记标题：资料区标记行与 ``Ref.title`` 都取它（老基线 ``_Doc`` 的第 4 参）；
    - ``block_ordinal`` = ``0`` / ``page`` = ``None``：内部片段坐标单独承载，不改旧引用身份。
    """
    return Material(
        chunk_id=note_chunk_id(int(row[0])),
        document_id=0,
        content=row[2] or "",
        block_ordinal=0,
        page=None,
        filename=row[1] or f"笔记 {row[0]}",
        category=row[3] or "",
        kb_id=int(row[4] or 1),
        keyword_score=score,
        note_id=int(row[0]),
    )


def _link_source_where(filters: dict | None, source) -> list:
    """扩链复用倒排时，只允许读取宿主已核验的那个来源。"""
    target = (filters or {}).get("_link_source")
    if target is None:
        return []
    kind = "document" if source is Document else "note"
    if (
        not isinstance(target, (tuple, list)) or len(target) != 2
        or target[0] != kind or isinstance(target[1], bool)
        or not isinstance(target[1], int) or target[1] <= 0
    ):
        return [literal(False)]
    return [source.id == target[1]]


async def _search_single_char(
    session: AsyncSession, char: str, filters: dict | None, limit: int
) -> list[Material]:
    """1 字查询的 ``LIKE`` 兜底（D-b）：命中即低分，按 ``chunk_id`` 升序取前 ``limit`` 条。"""
    pattern = f"%{_escape_like(char)}%"
    stmt = (
        select(
            Chunk.id,
            Chunk.document_id,
            Chunk.content,
            Chunk.ordinal,
            Chunk.page,
            Document.filename,
            Document.category,
            Document.kb_id,
            Document.tags,
            Document.created_at,
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(
            Document.deleted_at.is_(None),
            active_chunks(),
            Document.index_status == READY_STATUS,
            Chunk.content.like(pattern, escape="\\"),
            *document_where(filters),
            *_link_source_where(filters, Document),
        )
        .order_by(Chunk.id)
        .limit(max(1, int(limit)))
    )
    source_ids = await _source_ids(session, Document, filters)
    rows = []
    for ids in _id_batches(source_ids):
        rows.extend((await session.execute(
            stmt.where(Document.id.in_(ids)) if ids is not None else stmt
        )).all())
    rows.sort(key=lambda row: row[0])
    return [
        _row_to_material(row, score=FALLBACK_SCORE)
        for row in rows
        if matches_document(
            filters, kb_id=row[7], category=row[6], tags=row[8], created_at=row[9]
        )
    ][:max(0, int(limit))]


async def _search_single_char_notes(
    session: AsyncSession, char: str, filters: dict | None, limit: int
) -> list[Material]:
    """1 字查询的笔记 ``LIKE`` 兜底（阶段 4 · 4.5.2）：与文档侧同一口径（命中即低分）。

    没有这条，``source_types=["note"]`` + 1 字查询会**静默零命中**——正是 ``回归修复``
    要消掉的那种沉默（范围切「仅笔记」时两路皆空）。
    """
    pattern = f"%{_escape_like(char)}%"
    stmt = (
        select(
            Note.id,
            Note.title,
            _fragment_text(),
            Note.category,
            Note.kb_id,
            Note.tags,
            Note.created_at,
            LexicalMaterial.fragment_ordinal,
            LexicalMaterial.span_start,
            LexicalMaterial.span_end,
        )
        .join(Note, Note.id == LexicalMaterial.note_id)
        .where(
            Note.deleted_at.is_(None), _fragment_text().like(pattern, escape="\\"),
            *note_where(filters),
            *_link_source_where(filters, Note),
        )
        .order_by(Note.id, LexicalMaterial.fragment_ordinal)
        .limit(max(1, int(limit)))
    )
    source_ids = await _source_ids(session, Note, filters)
    rows = []
    for ids in _id_batches(source_ids):
        rows.extend((await session.execute(
            stmt.where(Note.id.in_(ids)) if ids is not None else stmt
        )).all())
    rows.sort(key=lambda row: (row[0], row[7]))
    return [
        _fragment_to_material(row, score=FALLBACK_SCORE)
        for row in rows
        if matches_note(
            filters, kb_id=row[4], category=row[3], tags=row[5], created_at=row[6]
        )
    ][:max(0, int(limit))]


async def _source_ids(session: AsyncSession, source, filters: dict | None) -> list[int] | None:
    """标签先在轻量来源行上筛选，再供单字 SQL LIKE/LIMIT 使用。"""
    if not filter_tags(filters):
        return None
    where = document_where if source is Document else note_where
    matches = matches_document if source is Document else matches_note
    stmt = select(
        source.id, source.kb_id, source.category, source.tags, source.created_at,
    ).where(source.deleted_at.is_(None), *where(filters), *_link_source_where(filters, source))
    if source is Document:
        stmt = stmt.where(Document.index_status == READY_STATUS)
    rows = (await session.execute(stmt)).all()
    return [
        row[0] for row in rows if matches(
            filters, kb_id=row[1], category=row[2], tags=row[3], created_at=row[4],
        )
    ]


def _id_batches(ids: list[int] | None):
    if ids is None:
        yield None
    else:
        for start in range(0, len(ids), 400):
            yield ids[start:start + 400]


def _fragment_text():
    return func.substr(
        Note.content, LexicalMaterial.span_start + 1,
        LexicalMaterial.span_end - LexicalMaterial.span_start,
    )


def _fragment_to_material(row, *, score: float) -> Material:
    material = _note_to_material(row, score=score)
    material.fragment_ordinal = int(row[7])
    material.span_start = int(row[8])
    material.span_end = int(row[9])
    return material


def _index_scope(
    filters: dict | None, *, metadata: bool = False,
    targets: list[tuple[str, int]] | None = None, identities: bool = False,
):
    """普通列上的有效材料范围；标签/时间和版本都在排名及 LIMIT 之前。"""
    branches = []
    if includes_documents(filters):
        columns = [LexicalMaterial.id, LexicalMaterial.token_count]
        if metadata:
            columns += [
                literal("document").label("kind"), Document.kb_id, Document.category,
                Document.tags, Document.created_at,
            ]
        if identities:
            columns += [Document.id.label("source_id")]
        branches.append(
            select(*columns)
            .join(Chunk, Chunk.id == LexicalMaterial.chunk_id)
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Document.deleted_at.is_(None), Document.index_status == READY_STATUS,
                active_chunks(), *document_where(filters), *_link_source_where(filters, Document),
                *([Document.id.in_([value for kind, value in targets if kind == "document"])]
                  if targets is not None else []),
            )
        )
    if includes_notes(filters):
        columns = [LexicalMaterial.id, LexicalMaterial.token_count]
        if metadata:
            columns += [
                literal("note").label("kind"), Note.kb_id, Note.category,
                Note.tags, Note.created_at,
            ]
        if identities:
            columns += [Note.id.label("source_id")]
        branches.append(
            select(*columns)
            .join(Note, Note.id == LexicalMaterial.note_id)
            .where(
                Note.deleted_at.is_(None), *note_where(filters), *_link_source_where(filters, Note),
                *([Note.id.in_([value for kind, value in targets if kind == "note"])]
                  if targets is not None else []),
            )
        )
    return (union_all(*branches) if len(branches) > 1 else branches[0]).subquery()


async def _candidate_materials(
    session: AsyncSession, ids: list[int], *, documents: bool = True, notes: bool = True,
) -> dict[int, Material]:
    out = {}
    # ponytail: 只为最终 top limit 回读正文及构造 Material；参数分批避免 bind 数上限。
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        if documents:
            rows = (await session.execute(
                select(
                    LexicalMaterial.id, Chunk.id, Chunk.document_id, Chunk.content,
                    Chunk.ordinal, Chunk.page, Document.filename, Document.category,
                    Document.kb_id, Document.tags, Document.created_at,
                )
                .join(Chunk, Chunk.id == LexicalMaterial.chunk_id)
                .join(Document, Document.id == Chunk.document_id)
                .where(LexicalMaterial.id.in_(batch))
            )).all()
            out.update({int(row[0]): _row_to_material(row[1:], score=0.0) for row in rows})
        if notes:
            rows = (await session.execute(
                select(
                    LexicalMaterial.id, Note.id, Note.title, _fragment_text(),
                    Note.category, Note.kb_id, Note.tags, Note.created_at,
                    LexicalMaterial.fragment_ordinal, LexicalMaterial.span_start, LexicalMaterial.span_end,
                )
                .join(Note, Note.id == LexicalMaterial.note_id)
                .where(LexicalMaterial.id.in_(batch))
            )).all()
            out.update({int(row[0]): _fragment_to_material(row[1:], score=0.0) for row in rows})
    return out


async def _query_postings(
    session: AsyncSession, query_terms: set[str], scope, *,
    documents: bool, notes: bool, note_buckets: set[int] | None = None,
):
    columns = (
        LexicalTerm.term_hash, LexicalTerm.owner_material_id,
        LexicalTerm.term, LexicalTerm.postings,
    )
    keys = [term_key(term) for term in query_terms]
    statements = []
    if documents:
        statement = select(*columns).join(scope, scope.c.id == LexicalTerm.owner_material_id)
        if notes:
            statement = statement.join(
                LexicalMaterial, LexicalMaterial.id == LexicalTerm.owner_material_id,
            ).where(LexicalMaterial.chunk_id.is_not(None))
        statements.append(statement.where(LexicalTerm.term_hash.in_(keys)))
    if notes:
        owner_notes = LexicalMaterial.note_id.is_not(None)
        if note_buckets is not None:
            owner_notes = and_(
                owner_notes,
                ((LexicalMaterial.note_id - 1) // lexical_index.NOTE_SOURCE_GROUP_SIZE).in_(
                    sorted(note_buckets)
                ),
            )
        statements.append(select(*columns).join(
            LexicalMaterial, LexicalMaterial.id == LexicalTerm.owner_material_id,
        ).where(LexicalTerm.term_hash.in_(keys), owner_notes))
    # 笔记桶的锚可能已软删或不在库/标签范围内；锚只负责解码，资格按实际成员判断。
    statement = union_all(*statements) if len(statements) > 1 else statements[0]
    return (await session.execute(statement)).all()


def _decode_candidates(postings, query_terms: set[str], eligible: set[int] | None):
    decoded = []
    candidate_ids = set()
    for key, owner_id, long_term, payload in postings:
        term = posting_terms(key, long_term)
        if term not in query_terms:
            continue
        for material_id, body_tf, title_tf in unpack_postings(payload, owner_id=owner_id):
            if eligible is not None and material_id not in eligible:
                continue
            decoded.append((material_id, term, body_tf, title_tf))
            if body_tf > 0:
                candidate_ids.add(material_id)
    return decoded, candidate_ids


async def _rank_metadata(
    session: AsyncSession, candidate_ids: set[int], filters: dict | None,
    segs: list[str], *, documents: bool, notes: bool, single_char: str | None = None,
):
    sql_phrase = all("\u4e00" <= char <= "\u9fff" for seg in segs for char in seg)
    lengths = {}
    identities = {}
    rank_sources = []
    if documents:
        # 不再重复联接范围子查询；从候选材料 PK 起步，来源资格仍按实际分块核对。
        document_guard = (
            select(literal(1)).select_from(Document)
            .where(
                Document.id == Chunk.document_id,
                Document.deleted_at.is_(None), Document.index_status == READY_STATUS,
                active_chunks(), *document_where(filters), *_link_source_where(filters, Document),
            ).correlate(Chunk).exists()
        )
        rank_sources.append((
            LexicalMaterial.__table__.join(Chunk, Chunk.id == LexicalMaterial.chunk_id),
            Chunk.content, [document_guard],
        ))
    if notes:
        rank_sources.append((
            LexicalMaterial.__table__.join(Note, Note.id == LexicalMaterial.note_id),
            _fragment_text(),
            [Note.deleted_at.is_(None), *note_where(filters), *_link_source_where(filters, Note)],
        ))
    rank_statements = []
    for rank_from, body, clauses in rank_sources:
        if single_char is not None:
            clauses = [*clauses, body.like(f"%{_escape_like(single_char)}%", escape="\\")]
            phrase_column = literal(True)
        elif sql_phrase:
            # 中文 lower 不变；二进制 INSTR 避开 MySQL collation，和 Python 子串判据同义。
            # ponytail: Latin/数字仍走原 Python lower，避免 K/İ 等 Unicode 两方言差异。
            phrase_column = or_(*(
                func.instr(cast(body, LargeBinary), cast(literal(seg), LargeBinary)) > 0
                for seg in segs
            )) if segs else literal(False)
        else:
            phrase_column = body
        rank_statements.append(
            select(
                LexicalMaterial.id, LexicalMaterial.token_count, LexicalMaterial.chunk_id,
                LexicalMaterial.note_id, LexicalMaterial.fragment_ordinal, phrase_column,
            ).select_from(rank_from).where(*clauses)
        )
    for batch in _id_batches(sorted(candidate_ids)):
        for stmt in rank_statements:
            for material_id, token_count, chunk_id, note_id, ordinal, phrase_value in (
                await session.execute(stmt.where(LexicalMaterial.id.in_(batch)))
            ).all():
                lengths[int(material_id)] = int(token_count)
                identities[int(material_id)] = (
                    int(chunk_id) if chunk_id is not None else note_chunk_id(int(note_id)),
                    note_id is not None, int(ordinal or 0),
                    bool(phrase_value) if sql_phrase or single_char is not None
                    else _has_phrase(phrase_value, segs),
                )
    return lengths, identities


def _body_frequencies(decoded, lengths):
    df: Counter[str] = Counter()
    bodies: dict[int, dict[str, int]] = {}
    headings: dict[int, set[str]] = {}
    for material_id, term, body_tf, title_tf in decoded:
        if material_id not in lengths:
            continue
        if body_tf > 0:
            df[term] += 1
            bodies.setdefault(material_id, {})[term] = body_tf
        if title_tf > 0:
            headings.setdefault(material_id, set()).add(term)
    return df, bodies, headings


def _score_candidates(query_terms, df, bodies, headings, lengths, identities, stats):
    total_docs, total_length = stats
    avg_len = total_length / total_docs
    total_query_weight = query_weight(query_terms)
    scored = []
    for material_id, freqs in bodies.items():
        if not passes_gate(query_terms, set(freqs), df, total_docs):
            continue
        chunk_id, is_note, ordinal, phrase = identities[material_id]
        raw = _bm25_raw(query_terms, freqs, lengths[material_id], avg_len, df, total_docs)
        overlap = weighted_overlap(query_terms, set(freqs)) / total_query_weight
        score = raw * overlap
        if phrase:
            score *= PHRASE_BONUS
        if is_note:
            score *= 1.0 + NOTE_TITLE_WEIGHT * (
                weighted_overlap(query_terms, headings.get(material_id, set())) / total_query_weight
            )
        scored.append((0 if phrase else 1, score, chunk_id, ordinal, material_id))
    scored.sort(key=lambda item: (item[0], -item[1], item[2], item[3]))
    return scored


async def search_source_keywords(
    session: AsyncSession, question: str, sources: list[tuple[str, int]],
    filters: dict | None = None,
) -> dict[tuple[str, int], Material]:
    """受信扩链目标批量检索；每来源独立 N/df/平均长度/门槛和 top1。"""
    text = (question or "").strip()
    if not text:
        return {}
    requested = []
    seen = set()
    for target in sources:
        if (
            isinstance(target, (tuple, list)) and len(target) == 2
            and target[0] in ("document", "note") and isinstance(target[1], int)
            and not isinstance(target[1], bool) and target[1] > 0
            and (includes_documents if target[0] == "document" else includes_notes)(filters)
        ):
            key = (target[0], target[1])
            if key not in seen:
                seen.add(key)
                requested.append(key)
    query_terms = set(tokenize(text))
    if len(text) > 1 and not query_terms:
        return {}
    result = {}
    for start in range(0, len(requested), 400):
        targets = requested[start:start + 400]
        documents = any(kind == "document" for kind, _value in targets)
        notes = any(kind == "note" for kind, _value in targets)
        target_filters = {
            **(filters or {}), "source_types": [
                kind for kind, enabled in (("document", documents), ("note", notes)) if enabled
            ],
        }
        scope = _index_scope(target_filters, targets=targets)
        rows = (await session.execute(select(
            _index_scope(target_filters, metadata=True, identities=True, targets=targets)
        ))).all()
        material_sources = {}
        stats = {}
        for material_id, token_count, kind, kb_id, category, tags, created_at, source_id in rows:
            matches = matches_document if kind == "document" else matches_note
            if not matches(filters, kb_id=kb_id, category=category, tags=tags, created_at=created_at):
                continue
            key = (kind, int(source_id))
            material_sources[int(material_id)] = key
            local = stats.setdefault(key, [0, 0])
            local[0] += 1
            local[1] += int(token_count)
        if not material_sources:
            continue
        if len(text) == 1:
            candidate_ids = set(material_sources)
            decoded = []
        else:
            postings = await _query_postings(
                session, query_terms, scope, documents=documents, notes=notes,
                note_buckets={
                    (value - 1) // lexical_index.NOTE_SOURCE_GROUP_SIZE
                    for kind, value in targets if kind == "note"
                },
            )
            decoded, candidate_ids = _decode_candidates(
                postings, query_terms, set(material_sources),
            )
        lengths, identities = await _rank_metadata(
            session, candidate_ids, target_filters, segments(text),
            documents=documents, notes=notes, single_char=text if len(text) == 1 else None,
        )
        winners = {}
        if len(text) == 1:
            for material_id in lengths:
                key = material_sources[material_id]
                chunk_id, _note, ordinal, _phrase = identities[material_id]
                item = (0, FALLBACK_SCORE, chunk_id, ordinal, material_id)
                if key not in winners or (chunk_id, ordinal) < (winners[key][2], winners[key][3]):
                    winners[key] = item
        else:
            local_decoded = {}
            for posting in decoded:
                local_decoded.setdefault(material_sources[posting[0]], []).append(posting)
            for key, terms in local_decoded.items():
                df, bodies, headings = _body_frequencies(terms, lengths)
                ranked = _score_candidates(
                    query_terms, df, bodies, headings, lengths, identities, stats[key],
                )
                if ranked:
                    winners[key] = ranked[0]
        materials = await _candidate_materials(
            session, [item[4] for item in winners.values()],
            documents=any(key[0] == "document" for key in winners),
            notes=any(key[0] == "note" for key in winners),
        )
        for key in targets:
            if key in winners and winners[key][4] in materials:
                item = materials[winners[key][4]]
                item.keyword_score = winners[key][1]
                result[key] = item
    return result


async def search_keywords(
    session: AsyncSession,
    question: str,
    filters: dict | None = None,
    limit: int = KEYWORD_TOP_K,
) -> list[Material]:
    """范围内 N/df/平均长度共用统计面；只读取查询词 posting 及过门槛候选正文。"""
    text = (question or "").strip()
    if not text:
        return []
    want_documents = includes_documents(filters)
    want_notes = includes_notes(filters)
    if not (want_documents or want_notes):
        return []
    if len(text) == 1:
        hits: list[Material] = []
        if want_documents:
            hits += await _search_single_char(session, text, filters, limit)
        if want_notes:
            hits += await _search_single_char_notes(session, text, filters, limit)
        # 顺序：文档在前、笔记在后（**逐条保留**既有的文档侧行为：没有笔记时 `hits[:limit]`
        # 与改造前逐条相同）。两类都是同一个低分，不宜按分数混排——没有可比依据。
        return hits[: max(0, int(limit))]

    query_terms = set(tokenize(text))
    if not query_terms:
        return []
    segs = segments(text)

    scope = _index_scope(filters)
    eligible: set[int] | None = None
    if filter_tags(filters):
        # ponytail: JSON 标签沿用两方言共享判据，只遍历元数据，复杂度 O(范围材料数)；
        # 若标签范围的测量成为瓶颈，升级为独立标签倒排，正文查询无需重写。
        rows = (await session.execute(select(_index_scope(filters, metadata=True)))).all()
        rows = [
            row for row in rows if (
                matches_document if row[2] == "document" else matches_note
            )(filters, kb_id=row[3], category=row[4], tags=row[5], created_at=row[6])
        ]
        eligible = {int(row[0]) for row in rows}
        stats = (len(rows), sum(row[1] for row in rows))
    else:
        stats = (await session.execute(
            select(func.count(), func.coalesce(func.sum(scope.c.token_count), 0)).select_from(scope)
        )).one()
    total_docs = int(stats[0])
    if not total_docs:
        return []

    postings = await _query_postings(
        session, query_terms, scope, documents=want_documents, notes=want_notes,
    )
    decoded, candidate_ids = _decode_candidates(postings, query_terms, eligible)
    lengths, identities = await _rank_metadata(
        session, candidate_ids, filters, segs, documents=want_documents, notes=want_notes,
    )
    df, bodies, headings = _body_frequencies(decoded, lengths)
    # 先按原短语层/公式/身份稳定排序，只回读最终 limit 的完整正文。
    scored = _score_candidates(
        query_terms, df, bodies, headings, lengths, identities,
        (total_docs, int(stats[1])),
    )
    selected = scored[: max(0, int(limit))]
    materials = await _candidate_materials(
        session, [item[4] for item in selected],
        documents=any(not identities[item[4]][1] for item in selected),
        notes=any(identities[item[4]][1] for item in selected),
    )
    result = []
    for _tier, score, _chunk_id, _ordinal, material_id in selected:
        material = materials.get(material_id)
        if material is not None:
            material.keyword_score = score
            result.append(material)
    return result


__all__ = [
    "FALLBACK_SCORE",
    "MIN_KEYWORD_COVERAGE",
    "MIN_KEYWORD_TERMS",
    "PHRASE_BONUS",
    "READY_STATUS",
    "SINGLE_CHAR_WEIGHT",
    "passes_gate",
    "query_coverage",
    "search_keywords",
    "search_source_keywords",
    "segments",
    "token_idf",
    "tokenize",
]
