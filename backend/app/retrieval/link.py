"""双向链接的一跳扩展（``回归修复``）：主命中沿 ``wiki_links`` 多拉一跳。

**为什么新建这个模块而不是唤醒 ``pipeline/wikilink.py::expand_chunks``**（实测口径，2026-09-19）：

- 老基线的 ``expand_chunks`` 收发的类型是 ``RetrievedChunk``（``chunk_id`` 是**字符串**合成键
  ``note:5:0``），而 v2 的检索产物是 :class:`app.retrieval.types.Material`
  （``chunk_id`` 是 **int**，就是 ``chunks.id`` —— 归因表的真实外键）。阶段 3 的分数/去重/编号
  全按 int 建起来；把 Material 桥接成 ``RetrievedChunk`` 再转回来，**片段会丢掉真实的
  ``chunks.id``** ⇒ 引用卡有 [n]、``message_attributions`` 却写不出对应行（外键指不到东西）。
- 反向也不行：``pipeline/`` 不能 import ``retrieval/``（仓库根 设计说明 的分包边界：禁止反向 import）。
- 所以本模块用 ``Material`` 原生实现同一套语义，**复用 ``wikilink`` 的三个常量**
  （``LINK_SEEDS`` / ``LINK_SCORE_FACTOR`` / ``LINK_SNIPPET_CHARS``）保证两处口径不漂移。
  ``wikilink.expand_chunks`` / ``_link_fragment`` **已于 4.5 收尾随零调用点清理删除**
  （见该模块 docstring 的「已删除」段）——本模块是这条能力的**唯一实现**。

口径（空 ``question`` 保留旧直接调用，生产检索按 U3 取真命中）：

- 开关关闭或无命中时原样返回（**不发起任何查询**）；
- 只对**库内**种子扩展：链接边表在本机 MySQL，网页材料不在同一个 id 空间；
- 出链与入链都算（「我链向谁」与「谁链到我」都是相关）；
- 去重：目标已在主命中里、或别的边已经拉过，都跳过；
- 未解析边（目标不存在）不产生片段；软删 / 非 ready / 正文为空的目标同样跳过；
- 链接片段降权（种子分 × 0.5）；
- 扩展是纯增益步骤，失败静默降级（记日志），不往 warnings 里掺内部细节。
"""

from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import derived_counters
from app.core.config import settings
from app.models import Chunk, Document, Note, active_chunks
from app.pipeline.knowledge_graph import load_wiki_adjacency
from app.pipeline.wikilink import (
    LINK_SCORE_FACTOR,
    LINK_SEEDS,
    LINK_SNIPPET_CHARS,
)
from app.retrieval import keyword
from app.retrieval.keyword import READY_STATUS
from app.retrieval.rerank import RerankStats
from app.retrieval.types import (
    FUSE_LIMIT, Material, includes_documents, includes_notes, matches_document,
    matches_note, note_chunk_id,
)

log = logging.getLogger(__name__)


def relevant_materials(question: str, materials: list[Material]) -> list[Material]:
    """重排不可用时，扩链仍须有正文词项依据；关系边本身不算相关性。"""
    items = list(materials or [])
    query_terms = set(keyword.tokenize(question))
    if not query_terms or not items:
        return []
    frequencies = [Counter(keyword.tokenize(item.content)) for item in items]
    df = Counter(term for frequency in frequencies for term in frequency)
    avg_len = sum(sum(frequency.values()) for frequency in frequencies) / len(items)
    return [
        item for item, frequency in zip(items, frequencies)
        if keyword.passes_gate(query_terms, set(frequency), df, len(items))
        and keyword._bm25_raw(
            query_terms, frequency, sum(frequency.values()), avg_len, df, len(items),
        ) > 0
    ]


async def _query_fragment(
    session: AsyncSession, source_type: str, source_id: int, question: str,
    *, seed_score: float, filters: dict | None,
) -> Material | None:
    """复用同一词项门槛及短语排名，SQL 在 LIMIT 前锁定 current 来源和有效范围。"""
    if not (includes_notes if source_type == "note" else includes_documents)(filters):
        return None
    scope = {
        **(filters or {}), "source_types": [source_type],
        "_link_source": (source_type, int(source_id)),
    }
    hits = await keyword.search_keywords(session, question, scope, limit=1)
    hits = relevant_materials(question, hits)
    if not hits or hits[0].source_ref != (source_type, int(source_id)):
        return None
    fragment = hits[0]
    fragment.score = round(float(seed_score) * LINK_SCORE_FACTOR, 6)
    return fragment


def link_budget(main_count: int, cap: int = FUSE_LIMIT, max_links: int | None = None) -> int:
    """一跳扩展的名额：**主命中一个不删，扩展只吃剩下的**，总数不超过快照上限 ``cap``。

    决策包 ``S3-决策包-023-024`` §2.1 第 9 条点出的口径风险：老基线把链接片段**并到尾部**、
    上限 ``RAG_LINK_MAX``，于是材料数可以超过 ``FUSE_LIMIT = 12``；而
    ``generation.citation.MAX_REFS`` 也是 12 ⇒ 多出来的片段会被渲染成 ``[13]``/``[14]``
    却**没有对应的引用卡**，I3（「模型看到的编号 = 引用卡编号」）当场破。

    这里把口径定死成一句话：``min(配置上限, 快照上限 − 主命中条数)``。
    主命中永远优先（扩展是增益，不该挤掉主命中），总数永远 ≤ ``cap``，
    名额用完就不再扩展（0 名额时连查询都不发）。
    """
    room = max(0, int(cap) - int(main_count or 0))
    allowed = int(max_links) if max_links is not None else int(getattr(settings, "RAG_LINK_MAX", 0) or 0)
    return max(0, min(allowed, room))


async def _scope_created_at(session: AsyncSession, model, row_id: int, filters: dict | None):
    """范围里**真的**约束了时间时，才按列取这一行的 ``created_at``；否则返回 ``None``。

    两个理由（都不是洁癖）：

    ① **返回 ``None`` 的语义是既成的**：``matches_note`` / ``matches_document`` 对
       ``created_at=None`` 的处置就是「该维度不参与判定」，而范围里**没写**时间时它们本来
       也不看这一列 ⇒ 不读它不改变任何判定，还省一次查询；
    ② ⚠️ **不许直接写 ``row.created_at``**：这一列是 ``server_default=func.now()``，而 MySQL
       方言**不支持 ``RETURNING``** ⇒ 同一会话里刚 INSERT 过的行上，SQLAlchemy 把它标成
       「过期」，第一次属性访问会发起一次**属性级懒加载**；异步会话里那会抛
       ``MissingGreenlet``（greenlet 之外不许 IO）——调用点看到的却是「一跳扩展静默降级」，
       排查起来像灵异事件。按列 ``SELECT`` 走的是**显式 await**，两种方言都安全
       （本项实测踩到：`F6-BC-created_at未加载探针.log` + `F6-BC-MissingGreenlet复盘.md`）。
    """
    if not ((filters or {}).get("created_after") or (filters or {}).get("created_before")):
        return None
    stmt = select(model.created_at).where(model.id == int(row_id))
    return (await session.execute(stmt)).scalar_one_or_none()


async def _note_fragment(
    session: AsyncSession,
    note_id: int,
    *,
    seed_score: float,
    filters: dict | None = None,
    question: str = "",
) -> Material | None:
    """拉目标笔记的命中片段；空 question 仍取正文前段（回收站目标跳过）。

    **回表后复核本轮范围**（专项 F6-B · ``回归修复``）：``filters`` 非空时目标行必须过
    :func:`app.retrieval.types.matches_note`（kb / 分类 / 标签 / 时间四维，``source_types``
    由该 matcher 自带的 ``includes_notes`` 门生效）。复核**只加不减**——既有的三道门禁
    （不存在 / 软删 / 正文空）逐字保留。

    ⚠️ 判定必须留在**回表之后**：``Material`` 没有 ``tags`` / ``created_at`` 两列 ⇒
    不存在「事后过滤材料」这条捷径；而前置到 :func:`expand_materials` 的循环之前，
    会让既有的边加载降级路径不再被覆盖（用例仍绿但覆盖丢了一半）。
    """
    if question.strip():
        return await _query_fragment(
            session, "note", note_id, question, seed_score=seed_score, filters=filters,
        )
    note = await session.get(Note, int(note_id))
    if note is None or note.deleted_at is not None:
        return None
    if not matches_note(
        filters,
        kb_id=note.kb_id,
        category=note.category,
        tags=note.tags,
        created_at=await _scope_created_at(session, Note, note.id, filters),
    ):
        return None
    content = (note.content or "").strip()
    if not content:
        return None
    return Material(
        chunk_id=note_chunk_id(int(note.id)),
        document_id=0,
        content=content[:LINK_SNIPPET_CHARS],
        block_ordinal=0,
        page=None,
        filename=(note.title or "").strip() or f"笔记 {note.id}",
        category=note.category or "",
        # 身份取**目标行自己的** kb（专项 F6-B）：取种子库就是「把目标改名后冒充合规」——
        # 跨库坏边会被贴上种子库的标签，连范围复核都拦不住它。
        kb_id=int(note.kb_id or 1),
        score=round(float(seed_score) * LINK_SCORE_FACTOR, 6),
        note_id=int(note.id),
    )


async def _document_fragment(
    session: AsyncSession,
    document_id: int,
    *,
    seed_score: float,
    filters: dict | None = None,
    question: str = "",
) -> Material | None:
    """拉目标文档的 current 命中块；空 question 保留 ``ordinal == 0`` 直接调用口径。

    与老基线的两点差异（有依据，见模块 docstring 与证据）：

    - ``chunk_id`` 取**真实** ``chunks.id``（老基线用合成键 ``document:7:0``）——归因表的外键要它；
    - 多一道 ``index_status == 'ready'`` 门禁：v2 的文档只有 ready 才可检索
      （``keyword._load_corpus`` / ``vector`` 回表都拦这道），链接扩展不该成为绕过它的后门，
      否则会把未就绪的半成品正文以 ``[n]`` 的形式喂给模型。

    范围复核与身份口径同 :func:`_note_fragment`（专项 F6-B · ``回归修复``）：
    回表拿到 ``Document`` 行之后、取分块之前判 :func:`app.retrieval.types.matches_document`，
    ``kb_id`` 取目标行自己的。
    """
    if question.strip():
        return await _query_fragment(
            session, "document", document_id, question, seed_score=seed_score, filters=filters,
        )
    document = await session.get(Document, int(document_id))
    if document is None or document.deleted_at is not None:
        return None
    if (document.index_status or "") != READY_STATUS:
        return None
    if not matches_document(
        filters,
        kb_id=document.kb_id,
        category=document.category,
        tags=document.tags,
        created_at=await _scope_created_at(session, Document, document.id, filters),
    ):
        return None
    piece = (
        await session.execute(
            select(Chunk)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.document_id == int(document_id), Chunk.ordinal == 0, active_chunks())
            .limit(1)
        )
    ).scalars().first()
    if piece is None:
        return None
    content = (piece.content or "").strip()
    if not content:
        return None
    return Material(
        chunk_id=int(piece.id),
        document_id=int(document.id),
        content=content[:LINK_SNIPPET_CHARS],
        block_ordinal=int(piece.ordinal or 0),
        page=int(piece.page) if piece.page is not None else None,
        filename=(document.filename or "").strip() or f"文档 {document.id}",
        category=document.category or "",
        kb_id=int(document.kb_id or 1),
        score=round(float(seed_score) * LINK_SCORE_FACTOR, 6),
    )


async def expand_materials(
    session: AsyncSession,
    materials: list[Material],
    *,
    cap: int = FUSE_LIMIT,
    max_links: int | None = None,
    filters: dict | None = None,
    question: str = "",
    stats: RerankStats | None = None,
) -> list[Material]:
    """主命中沿链接边多拉一跳：出链与入链的目标片段并到结果**尾部**。

    返回 ``主命中 + 扩展片段``（主命中顺序与内容逐条不变）。名额见 :func:`link_budget`。
    ``max_links`` 可注入（单测不必改全局配置）。

    ``filters`` = **本轮有效范围**（专项 F6-B · ``回归修复``）：原样下传给目标检索，
    由 SQL 与回表共同复核。``None`` ⇒ 逐字退回既有行为（无范围 = 不限制）；
    被范围拒的目标**静默跳过**（与「目标不存在」同待遇，不占名额、不记事件——扩展是增益步骤）。
    ``question`` 非空时目标必须有词项命中，取实际命中片段；空串保留直接调用的历史首段口径。
    """
    items = list(materials or [])
    enabled = bool(getattr(settings, "RAG_LINK_EXPAND", False))
    link_stats = {
        "enabled": enabled, "candidates": 0, "kept": 0, "gate_removed": 0, "target_skipped": 0,
        "degraded": False, "gate": "keyword" if question.strip() else "legacy",
        "candidate_cap": max(0, int(cap)),
    }
    if stats is not None:
        stats.links = link_stats
    if not items or not enabled:
        return items
    budget = link_budget(len(items), cap=cap, max_links=max_links)
    if budget <= 0:
        return items
    try:
        existing = {ref for item in items if (ref := item.source_ref) is not None}
        scores: dict[tuple[str, int], float] = {}
        seeds = [seed for seed in items[:LINK_SEEDS] if seed.source_ref is not None]
        adjacency = await load_wiki_adjacency(
            session, [(seed.kb_id, *seed.source_ref) for seed in seeds],
        )
        for seed in seeds:
            for _, kind, source_id in adjacency.get((int(seed.kb_id or 1), *seed.source_ref), []):
                target = (kind, source_id)
                if target in existing:
                    continue
                score = float(seed.score or 0.0)
                scores[target] = max(scores.get(target, score), score)
        query = question.strip()
        # ponytail: 全部目标一次查词项，保留每来源原统计与排名；不截边，SQL 参数由词项入口分批。
        hits = (
            await keyword.search_source_keywords(session, question, list(scores), filters)
            if query and scores else {}
        )
        extra: list[Material] = []
        for target, score in scores.items():
            if query:
                fragment = hits.get(target)
                if fragment is not None and (
                    fragment.source_ref != target or not relevant_materials(question, [fragment])
                ):
                    fragment = None
                if fragment is not None:
                    fragment.score = round(float(score) * LINK_SCORE_FACTOR, 6)
            else:
                fragment_fn = _note_fragment if target[0] == "note" else _document_fragment
                fragment = await fragment_fn(
                    session, target[1], seed_score=score, filters=filters,
                )
            if fragment is not None:
                extra.append(fragment)
                if len(extra) >= budget:
                    break
            elif query:
                link_stats["target_skipped"] += 1
        link_stats["candidates"] = len(extra)
        link_stats["kept"] = len(extra)
        return items + extra
    except Exception as exc:  # noqa: BLE001 —— 纯增益步骤，失败静默降级（I5）
        log.warning("链接扩展失败，已静默跳过：%s", exc)
        # `回归修复` **#31**（2026-09-28 批次三）：这条「已静默跳过」原先只留日志、零计数
        # ⇒ 计进**既有键** `doc_links`（键义 = 文档侧链接边维护失败；扩链就是这条边上的纯增益步骤），
        # ⛔ **不新增键**（`COUNTER_KEYS` 只增不改义，本笔不扩键集）。
        derived_counters.bump("doc_links")
        link_stats["degraded"] = True
        return items


__all__ = ["expand_materials", "link_budget", "relevant_materials"]
