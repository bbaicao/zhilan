"""图谱检索分支（阶段 5 · 条目 `5.3`）——**结构信号**做第三个检索通道，进 RRF 融合。

问题里的词若命中了某篇资料的**身份**（``documents.filename`` / ``notes.title`` /
``tags`` / ``category`` / ``wiki_links``），这本身就是一条和「正文里有这个词」不同的信号。
本模块把它变成与 :mod:`app.retrieval.keyword` / :mod:`app.retrieval.vector` **平级的第三个
通道**，产出进 :func:`app.retrieval.fuse.fuse` 的三路 RRF。

## 落点与边界（开工指令包 §0.0 的裁决，别在这里重新推导）

- **不移植 ``lightrag-hku`` 整包**：它的核心链硬依赖 ``numpy`` + ``networkx`` +
  ``nano-vectordb`` + ``tiktoken`` + ``json_repair`` + ``PyYAML``，且带 61 处 ``pipmaster``
  运行期自装 ⇒ 直接违反「不新增依赖」。这里只抄**机制**（别名取种子 / 一跳邻居扩展）。
- **不建表、不产生迁移**：节点与边**全部由既有数据在查询期派生**（``documents`` /
  ``notes`` / ``wiki_links``），用**有界 SQL** 现算。故本模块不 import 任何 ORM 之外的存储。
- **首版不做 LLM 抽实体**：索引期做抽取 = 每个分块约 2 次模型调用（一份 100 块的文档 ≈ 200 次，
  用户上传时没有提示），且 ``tests/verify_v2.py`` 的**禁网矩阵**会对任何出网尝试判红。
  真 GraphRAG 档的接口与提示词口径在开工包 §9.2 预留，将来「加一层」而不是重写。
- **零出网**：本模块是**纯 SQL + 纯 Python**，不 import 任何 HTTP 客户端。

## 数据模型（查询期派生，零存储）

| 概念 | 取值 | 怎么来 |
|---|---|---|
| 节点 | ``("document", id)`` / ``("note", id)`` | ``deleted_at IS NULL`` 且文档 ``index_status == 'ready'`` 的行 |
| **强别名** | 文档 ``filename`` 去扩展名；笔记 ``title`` | 直接读列 |
| **弱别名** | ``tags[]`` 每项、``category`` | ``tags`` 是 JSON 列（两方言语义不同）⇒ **在 Python 侧归一** |
| 边 ①（``wiki``） | ``wiki_links`` 里 ``resolved=True`` 且 ``kb_id`` 相同的边（出链 + 入链都算） | 与 link 复用 :func:`app.pipeline.knowledge_graph.load_wiki_adjacency` |
| 边 ②（``co``） | 两篇资料共享 ≥1 个别名 | 由种子节点的别名反查（**不落库**） |

别名归一化（唯一口径，单测钉住）：``折叠空白 → 去首尾 → 去扩展名 → casefold()``。
**不做**同义词 / 近义 / 词干化 / 拼音——那是「真语义档」的事。

## 通道分与融合

- **种子强度** ``seed_weight``：强别名命中 = ``1.0``，弱别名命中 = ``GRAPHRAG_WEAK_ALIAS_WEIGHT``；
  同一节点被多个别名命中取**最大**（不累加，避免「标签多者通吃」）；
- **一跳邻居** ``graph_score = seed_weight × GRAPHRAG_HOP_DECAY × GRAPHRAG_SCORE_FACTOR``；
  **种子自身**取 ``seed_weight``（不降权）；
- **一份材料只出一条**：同一 ``chunk_id`` 被多个种子 / 多条边带到 ⇒ 取**最大** ``graph_score``；
- **名额** ``budget = min(GRAPHRAG_TOP_K, FUSE_LIMIT − 主命中条数)``，**主命中永远优先**
  （名额公式**复用** :func:`app.retrieval.link.link_budget` 的口径，见该函数的理由）。

## 两处刻意的实现约束（补开工包未写死的细节，均已在 `回归检查3口径.md` 登记）

1. **反向包含不算命中**：判据是 ``token == alias`` 或 ``token ⊆ alias``（问题里的词是资料
   身份的一部分或全部）。**不做** ``alias ⊆ token``——那会让两字分类名被长查询词裹挟命中，
   是「问题里出现了某个词」而不是「问题在指那篇资料」。
2. **长度 <2 的别名与 token 一律不参与**：单字（尤其单字 ``tags``）在中文里几乎必然误命中；
   单字查询本来就由关键词路的 ``LIKE`` 兜底（:func:`app.retrieval.keyword._search_single_char`）。

## 关态 = 零影响

`enabled()` 是**唯一**的开关读取点：:func:`search_graph` 首行调它并返回 ``[]``，
``retrieve()`` 在调用本模块**之前**也调它——这样「关态零 ``search_graph`` 调用」这条断言
（A11 原文）才真的成立（只靠模块内早退，调用计数仍会 +1）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Chunk, Document, Note, active_chunks
from app.pipeline.knowledge_graph import Adjacency, load_wiki_adjacency, tags_list, wiki_neighbors
from app.retrieval.keyword import READY_STATUS, tokenize
from app.retrieval.link import _scope_created_at, link_budget
from app.retrieval.types import (
    FUSE_LIMIT,
    Material,
    document_where,
    matches_document,
    matches_note,
    note_chunk_id,
    note_where,
)

log = logging.getLogger(__name__)

# --- 配置默认值（与 `app/core/config.py` 的同名键一一对应）--------------------
# 这里再写一份字面量，是为了「键缺失时也能跑」（getattr 兜底）与给用例一个数值锚点；
# 两边漂移**不会自动红**，故 `tests/retrieval/test_graph_channel.py` 把每个键的默认值
# 与本模块的兜底值逐个钉住（口径同 `rerank.DEFAULT_MIN_RERANK_SCORE` 那一对）。

DEFAULT_TOP_K = 6
DEFAULT_SCORE_FACTOR = 0.5
DEFAULT_HOP_DECAY = 0.5
DEFAULT_WEAK_ALIAS_WEIGHT = 0.5
DEFAULT_MAX_TERMS = 8

#: 别名长度下限：短于此的别名与 token 一律不参与身份匹配（理由见模块 docstring 第 2 条）。
MIN_ALIAS_CHARS = 2

#: 有界 SQL 的**每域**行数上限系数：``terms × 本系数``（开工包 §2.7 第 5 条）。
CANDIDATE_LIMIT_FACTOR = 4

_WS_RE = re.compile(r"\s+")
# 只剥「点 + 字母开头 + 最多 7 个字母数字」的尾巴（``报告.md`` / ``a.docx``）。
# ⚠️ 刻意**不**接受数字开头：``规划 v1.2`` 用「点后任意 1–8 位字母数字」会被剥成 ``规划 v1``。
_EXT_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")

#: 模块内计数（可见性：**不加端点、不加帧键**；面板化出口 = ``GET /api/agent/audit`` 的
#: ``counters.graph``——条目 `5.5` 已收口，键名与 :func:`failure_counts` 逐字一致）。
_COUNTERS: dict[str, int] = {}


def _bump(name: str, amount: int = 1) -> None:
    _COUNTERS[name] = _COUNTERS.get(name, 0) + int(amount)


def failure_counts() -> dict[str, int]:
    """模块内计数快照（**复制**，调用方改不动内部状态）。

    键：``calls``（``search_graph`` 真进入的次数）/ ``seeds`` / ``neighbors`` /
    ``materials``（产出条数）/ ``failures``（静默降级的次数）/ 以及三个早退原因
    ``no_terms`` / ``no_budget`` / ``no_seeds``。
    """
    return dict(_COUNTERS)


def reset_counters() -> None:
    """清零模块内计数。

    ⚠️ **必须在两处调**（本仓踩过「进程全局计数跨用例泄漏」）：``tests/api/conftest.py``
    的 autouse 钉子 ``pin_graphrag_off``，以及每个开态用例自己开头。
    """
    _COUNTERS.clear()


# --- 开关与旋钮（**唯一**读开关的地方是 `enabled()`）--------------------------


def enabled() -> bool:
    """开关状态（读 ``GRAPHRAG_ENABLE``，默认 ``False``）。**唯一**的开关读取点。"""
    return bool(getattr(settings, "GRAPHRAG_ENABLE", False))


def _top_k() -> int:
    try:
        return max(0, int(getattr(settings, "GRAPHRAG_TOP_K", DEFAULT_TOP_K)))
    except (TypeError, ValueError):
        return DEFAULT_TOP_K


def _score_factor() -> float:
    try:
        return float(getattr(settings, "GRAPHRAG_SCORE_FACTOR", DEFAULT_SCORE_FACTOR))
    except (TypeError, ValueError):
        return DEFAULT_SCORE_FACTOR


def _hop_decay() -> float:
    try:
        return float(getattr(settings, "GRAPHRAG_HOP_DECAY", DEFAULT_HOP_DECAY))
    except (TypeError, ValueError):
        return DEFAULT_HOP_DECAY


def _weak_alias_weight() -> float:
    try:
        return float(getattr(settings, "GRAPHRAG_WEAK_ALIAS_WEIGHT", DEFAULT_WEAK_ALIAS_WEIGHT))
    except (TypeError, ValueError):
        return DEFAULT_WEAK_ALIAS_WEIGHT


def _max_terms() -> int:
    try:
        return max(1, int(getattr(settings, "GRAPHRAG_MAX_TERMS", DEFAULT_MAX_TERMS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_TERMS


def graph_budget(main_count: int, cap: int = FUSE_LIMIT) -> int:
    """图通道名额：``min(GRAPHRAG_TOP_K, cap − 主命中条数)``。

    ⚠️ **必须复用** :func:`app.retrieval.link.link_budget` 的口径而不是另写一份：
    ``FUSE_LIMIT = 12`` 与 ``citation.MAX_REFS = 12`` 对齐是 I3 的硬约束，超了会出现
    「渲染成 ``[13]`` 却没有引用卡」的材料（`link.py:43-57` 的原文注释）。
    **主命中一个不删**：图通道是增益步骤，名额用完就一条不加（0 名额时连查询都不发）。
    """
    return link_budget(max(0, int(main_count or 0)), cap=cap, max_links=_top_k())


def _channel_weight() -> float:
    """图通道的 **RRF 通道系数**（= ``GRAPHRAG_SCORE_FACTOR``）。

    为什么需要它（**自测抓到的第二处真缺陷，有实测数字**）：三路等权时，图通道的 rank-1
    材料拿到 ``1/61``，与关键词路 rank-1 的 ``1/61`` **打平**，再由 `fuse` 的
    ``chunk_id`` 升序 tie-break 定序 ⇒ 语料里「先建的那篇」被顶到前面，期望块掉到第 2 位。
    实测（`tests/eval` 30 正 + 10 诱饵）：``MRR 0.9833 → 0.8833``，**踩破 A14 的「MRR 不下降」**。

    口径红线是「**主命中永远优先**」，所以这里要的是**结构性保证**而不是调参碰巧对：

    - 图通道最大的 RRF 贡献 = ``系数 / (RRF_K + 1)``；
    - 主命中最小的 RRF 贡献 = ``1 / (RRF_K + max(KEYWORD_TOP_K, VECTOR_TOP_K))``
      （两路各自的召回上限就是它们）；
    - 默认系数 ``0.5`` ⇒ ``0.5/61 = 0.00820 < 1/80 = 0.01250`` ⇒
      **任何图通道材料都排不到任何主命中之前**，于是「关态的顺序」在开态**逐条不变**，
      Recall / MRR 只能持平或改善。

    系数一旦调到 ``0.7625`` 以上这条不变式就不成立——`tests/retrieval/test_graph_channel.py`
    的 ``test_the_channel_can_never_outrank_a_main_hit`` 把它钉死（改 `RRF_K` / `*_TOP_K`
    或调高系数都会红）。它同时仍是「一跳邻居分」的乘数（§2.4 的公式），两处都是
    「这条通道有多大声」的同一个语义。
    """
    return _score_factor()


# --- 别名归一化与匹配（纯函数，单测钉住）--------------------------------------


def normalize_alias(text: str) -> str:
    """别名归一化（唯一口径）：折叠空白 → 去首尾 → 去扩展名 → ``casefold()``。

    ``casefold()`` 而不是 ``lower()``：同一批英文别名的比较要跨语言稳定
    （``ß`` → ``ss``），与「大小写差异不得影响命中」这条用例（A8）同源。
    """
    out = _WS_RE.sub(" ", str(text or "")).strip()
    out = _EXT_RE.sub("", out).strip()
    return out.casefold()


def _dedupe_aliases(values) -> list[str]:
    """归一 + 去空 + 去重（保序）+ **丢掉长度不足的**（理由见模块 docstring）。"""
    out: list[str] = []
    for value in values or []:
        alias = normalize_alias(value)
        if len(alias) < MIN_ALIAS_CHARS or alias in out:
            continue
        out.append(alias)
    return out


def _split_aliases(title: str, tags, category: str) -> tuple[list[str], list[str]]:
    """一行资料 → ``(强别名, 弱别名)``。

    ``tags`` 的归一复用 :func:`app.pipeline.knowledge_graph.tags_list`——**图视图**
    （``GET /api/graph``）用的就是它，两处口径必须同源，否则「界面上的图」与
    「检索用的图」会互相矛盾。
    """
    return _dedupe_aliases([title]), _dedupe_aliases([*tags_list(tags), category])


def _alias_hit(alias: str, tokens: list[str]) -> bool:
    """问题里的词是否指向了这个别名：``token == alias`` 或 ``token ⊆ alias``。

    反向包含（``alias ⊆ token``）**刻意不做**——理由见模块 docstring 第 1 条。
    """
    if len(alias) < MIN_ALIAS_CHARS:
        return False
    return any(len(tok) >= MIN_ALIAS_CHARS and (tok == alias or tok in alias) for tok in tokens)


def _query_tokens(question: str) -> list[str]:
    """问题 → 身份匹配用的 token（**复用关键词路的分词**，有界）。

    复用 :func:`app.retrieval.keyword.tokenize` 而不是另造一套：两路对同一个问题必须
    有**同一套中文口径**（2-gram 展开已经解决「中文没有空格」），否则同一次检索里
    「关键词路以为的词」与「图通道以为的词」会不一致。
    """
    out: list[str] = []
    seen: set[str] = set()
    for token in tokenize(question or ""):
        if len(token) < MIN_ALIAS_CHARS or token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= _max_terms():
            break
    return out


def _seed_weight(node: "_Node", tokens: list[str]) -> float | None:
    """种子强度：强别名命中 ``1.0``、弱别名命中 ``GRAPHRAG_WEAK_ALIAS_WEIGHT``；都没中返回 ``None``。

    同一节点被多个别名命中取**最大**（不累加）。
    """
    best: float | None = None
    for alias in node.strong:
        if _alias_hit(alias, tokens):
            best = 1.0 if best is None else max(best, 1.0)
    weak = _weak_alias_weight()
    for alias in node.weak:
        if _alias_hit(alias, tokens):
            best = weak if best is None else max(best, weak)
    return best


def _shared_alias_weight(node: "_Node", seeds: list[tuple[float, "_Node"]]) -> float | None:
    """共现边（类型 2）：与某个种子共享 ≥1 个别名 ⇒ 返回**最强种子**的强度，否则 ``None``。"""
    owned = set(node.strong) | set(node.weak)
    if not owned:
        return None
    best: float | None = None
    for weight, seed in seeds:
        if seed.key == node.key:
            continue
        if owned & (set(seed.strong) | set(seed.weak)):
            best = weight if best is None else max(best, weight)
    return best


def _expansion_terms(seeds: list[tuple[float, "_Node"]]) -> list[str]:
    """共现边反查用的别名表：按**种子顺序**铺开（强别名在前），上限 ``GRAPHRAG_MAX_TERMS``。

    上限是硬的：反查 SQL 的 ``LIKE`` 子句数正比于本表长度，不封顶就成了「随语料增长的查询」。
    """
    out: list[str] = []
    cap = _max_terms()
    for _weight, node in seeds:
        for alias in [*node.strong, *node.weak]:
            if alias in out:
                continue
            out.append(alias)
            if len(out) >= cap:
                return out
    return out


# --- 节点与有界 SQL -----------------------------------------------------------


@dataclass
class _Node:
    """一个图节点（文档或笔记）在**本模块内**的轻量形态。"""

    kind: str  # "document" | "note"
    source_id: int
    kb_id: int
    title: str
    category: str
    strong: list[str] = field(default_factory=list)
    weak: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, int]:
        return (self.kind, self.source_id)


def _escape_like(text: str) -> str:
    """转义 ``LIKE`` 通配符（问题 token 里完全可能出现 ``%`` / ``_``）。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _candidate_clauses(title_column, tags_column, category_column, terms: list[str]):
    """「某个 term 出现在这一行的（标题 / 分类 / 标签）里」的 SQL 条件。

    ``tags`` 是 JSON 列、两方言语义不同 ⇒ 这里只做**粗筛**（``CAST`` 成字符串后 ``LIKE``），
    精确判定仍由 :func:`_split_aliases` 在 Python 侧按归一化后的标签做，口径同
    :func:`app.retrieval.types.matches_document` 对 ``tags`` 的处置（**不下推成真语义判定**）。

    ⚠️ **三列都用 ``LIKE %term%``，不用等值**：身份匹配的判据是「问题里的词是别名的一部分」
    （``token ⊆ alias``，见模块 docstring），所以「问题说 *工作*、文档分类是 *工作日志*」
    也得在 SQL 里筛得出来——用 ``category == term`` 会在粗筛阶段就把这种情况下全部丢掉，
    Python 侧的精确判定根本没机会看到它们（这是自测抓到的一处真缺陷）。
    """
    clauses: list = []
    for term in terms:
        pattern = f"%{_escape_like(term)}%"
        clauses.append(func.lower(title_column).like(pattern, escape="\\"))
        clauses.append(func.lower(category_column).like(pattern, escape="\\"))
        # JSON may store Unicode literally or escaped; exact aliases are checked below.
        for encoded in dict.fromkeys((
            term, json.dumps(term, ensure_ascii=False)[1:-1],
            json.dumps(term, ensure_ascii=True)[1:-1],
        )):
            clauses.append(func.lower(cast(tags_column, String)).like(
                f"%{_escape_like(encoded)}%", escape="\\",
            ))
    return or_(*clauses)


async def _load_document_nodes(
    session: AsyncSession, terms: list[str], filters: dict | None, limit: int
) -> list[_Node]:
    """按 ``terms`` 取候选**文档**节点（有界 SQL + 回表二次收口）。

    三道门禁都用**既有**口径，不另写一套筛选（那正是 ``回归修复`` 的成因）：
    ``deleted_at IS NULL`` + ``index_status == 'ready'``（软删与未就绪不可被图通道
    「后门」带进来，``link.py:120-124`` 的先例）+ ``document_where`` / ``matches_document``。
    """
    stmt = (
        select(
            Document.id,
            Document.filename,
            Document.category,
            Document.tags,
            Document.kb_id,
            Document.created_at,
        )
        .where(
            Document.deleted_at.is_(None),
            Document.index_status == READY_STATUS,
            *document_where(filters),
            _candidate_clauses(Document.filename, Document.tags, Document.category, terms),
        )
        .order_by(Document.id)
        .limit(max(1, int(limit)))
    )
    rows = (await session.execute(stmt)).all()
    out: list[_Node] = []
    for row in rows:
        if not matches_document(
            filters, kb_id=row[4], category=row[2], tags=row[3], created_at=row[5]
        ):
            continue
        strong, weak = _split_aliases(row[1] or "", row[3], row[2] or "")
        if not (strong or weak):
            continue
        out.append(
            _Node(
                kind="document",
                source_id=int(row[0]),
                kb_id=int(row[4] or 1),
                title=(row[1] or "").strip() or f"文档 {row[0]}",
                category=row[2] or "",
                strong=strong,
                weak=weak,
            )
        )
    return out


async def _load_note_nodes(
    session: AsyncSession, terms: list[str], filters: dict | None, limit: int
) -> list[_Node]:
    """按 ``terms`` 取候选**笔记**节点（与 :func:`_load_document_nodes` 同构）。

    正文为空的笔记不进候选——口径同 :func:`app.retrieval.keyword._load_note_corpus`
    （它对空正文笔记的判据也一样：空正文进不了材料，白占名额）。
    """
    stmt = (
        select(
            Note.id,
            Note.title,
            Note.content,
            Note.category,
            Note.tags,
            Note.kb_id,
            Note.created_at,
        )
        .where(
            Note.deleted_at.is_(None),
            *note_where(filters),
            _candidate_clauses(Note.title, Note.tags, Note.category, terms),
        )
        .order_by(Note.id)
        .limit(max(1, int(limit)))
    )
    rows = (await session.execute(stmt)).all()
    out: list[_Node] = []
    for row in rows:
        if not (row[2] or "").strip():
            continue
        if not matches_note(
            filters, kb_id=row[5], category=row[3], tags=row[4], created_at=row[6]
        ):
            continue
        # ``title`` 列放在第 5 位会与 ``Document`` 的列序混起来，故这里按索引显式取。
        strong, weak = _split_aliases(row[1] or "", row[4], row[3] or "")
        if not (strong or weak):
            continue
        out.append(
            _Node(
                kind="note",
                source_id=int(row[0]),
                kb_id=int(row[5] or 1),
                title=(row[1] or "").strip() or f"笔记 {row[0]}",
                category=row[3] or "",
                strong=strong,
                weak=weak,
            )
        )
    return out


async def _load_nodes(
    session: AsyncSession, terms: list[str], filters: dict | None
) -> list[_Node]:
    """两个域的候选节点合并（文档在前、笔记在后，各自有界）。

    行列上限 = ``len(terms) × CANDIDATE_LIMIT_FACTOR``，**每域各一份**——「把全部
    ``documents`` / ``notes`` 拉进内存再匹配」是开工包 §2.7 第 5 条明令禁止的形状。
    """
    if not terms:
        return []
    limit = max(1, len(terms)) * CANDIDATE_LIMIT_FACTOR
    nodes = await _load_document_nodes(session, terms, filters, limit)
    nodes += await _load_note_nodes(session, terms, filters, limit)
    return nodes


# --- 一跳邻居 -----------------------------------------------------------------


async def _wiki_neighbors(
    session: AsyncSession, node: _Node, adjacency: Adjacency | None = None,
) -> list[tuple[str, int]]:
    """沿 ``wiki_links`` 走一跳：出链目标 + 入链来源，**只认已解析且同库的边**。

    生产路径复用本轮批量邻接表；直接调用只加载这个种子，仍用同一边语义。
    """
    seed = (node.kb_id, *node.key)
    if adjacency is None:
        adjacency = await load_wiki_adjacency(session, [seed])
    return [(kind, source_id) for _, kind, source_id in wiki_neighbors(adjacency, seed)]


async def _node_by_key(
    session: AsyncSession, key: tuple[str, int], *, filters: dict | None = None
) -> _Node | None:
    """按 ``(kind, id)`` 取节点（**同样过三道门禁**：未软删 / ready / 别名非空）。

    链接边的目标 id 不能直接信：边是「解析当时」写下的，目标可能已被软删、退回未就绪，
    或根本没有 ``chunks`` 行。这里回表一次把它挡掉——**图通道不是绕过检索门禁的后门**。

    ``filters`` = **本轮有效范围**（专项 F6-C · ``回归修复``）：种子与共现邻居走的是
    :func:`_load_nodes`（SQL 下推 + 回表 Python 收口），而 **wiki 一跳走的是本函数**
    —— 上面那三道门只回答「行还在不在」，不回答「在不在用户设的范围内」。所以拿到真实
    ORM 行之后用与种子路**同一对** matcher（:func:`matches_note` / :func:`matches_document`）
    再复核一次，不匹配就返回 ``None``：复用「键查不到」的**同一条出口**
    （调用点 ``_run`` 已经写了 ``if node is not None``），不新增分支形态、不动控制流。

    ⚠️ 插入点**只有这里**：:func:`_node_materials` 那边**文档的 ORM 行已经不在作用域**
    （它只读 ``Chunk``，``_Node`` 又不带 ``tags`` / ``created_at``）⇒ 往那儿挪会变成
    「多一次查询且拿不到那两列」。
    """
    kind, source_id = key
    if kind == "note":
        note = await session.get(Note, int(source_id))
        if note is None or note.deleted_at is not None:
            return None
        if not (note.content or "").strip():
            return None
        strong, weak = _split_aliases(note.title or "", note.tags, note.category or "")
        if not (strong or weak):
            return None
        if not matches_note(
            filters,
            kb_id=note.kb_id,
            category=note.category,
            tags=note.tags,
            created_at=await _scope_created_at(session, Note, note.id, filters),
        ):
            return None
        return _Node(
            kind="note",
            source_id=int(note.id),
            kb_id=int(note.kb_id or 1),
            title=(note.title or "").strip() or f"笔记 {note.id}",
            category=note.category or "",
            strong=strong,
            weak=weak,
        )
    document = await session.get(Document, int(source_id))
    if document is None or document.deleted_at is not None:
        return None
    if (document.index_status or "") != READY_STATUS:
        return None
    strong, weak = _split_aliases(document.filename or "", document.tags, document.category or "")
    if not (strong or weak):
        return None
    if not matches_document(
        filters,
        kb_id=document.kb_id,
        category=document.category,
        tags=document.tags,
        created_at=await _scope_created_at(session, Document, document.id, filters),
    ):
        return None
    return _Node(
        kind="document",
        source_id=int(document.id),
        kb_id=int(document.kb_id or 1),
        title=(document.filename or "").strip() or f"文档 {document.id}",
        category=document.category or "",
        strong=strong,
        weak=weak,
    )


# --- 材料构造 -----------------------------------------------------------------


async def _node_materials(
    session: AsyncSession, node: _Node, score: float, room: int, exclude_ids: set[int]
) -> list[Material]:
    """把一个节点变成材料：文档取该文档的**分块**（``ordinal`` 升序，首块优先），
    笔记走 ``note_chunk_id``（**负数占位**，与 ``link.py`` 同款——``notes.id`` 与
    ``chunks.id`` 必撞号，直接用会把同号文档分块顶包）。

    ``exclude_ids`` = 主命中已有的 ``chunk_id``（含笔记的负数占位）：**已在主命中里的
    块不再由图通道带一遍**——名额是稀缺的，让它去拿主命中**拿不到**的块（这正是 A4 的语义）。
    去重口径与 :func:`fuse` 一致（都用 ``Material.chunk_id``）。

    正文**不截断**（与 ``link.py`` 有意的一处差异）：那边的片段是「主命中之后 append 的
    增益」，截到 ``LINK_SNIPPET_CHARS`` 是合理的；本通道的材料是**进 RRF 的一等公民**，
    与关键词路的同类材料必须逐字同形，否则「同一条块由哪条通道带出来」会渲染出两种正文。
    """
    if node.kind == "note":
        chunk_id = note_chunk_id(node.source_id)
        if chunk_id in exclude_ids:
            return []
        note = await session.get(Note, node.source_id)
        if note is None or note.deleted_at is not None:
            return []
        content = (note.content or "").strip()
        if not content:
            return []
        return [
            Material(
                chunk_id=chunk_id,
                document_id=0,
                content=content,
                block_ordinal=0,
                page=None,
                filename=node.title,
                category=node.category,
                kb_id=node.kb_id,
                graph_score=round(float(score), 6),
                note_id=int(node.source_id),
            )
        ]

    stmt = (
        select(Chunk.id, Chunk.content, Chunk.ordinal, Chunk.page)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.document_id == int(node.source_id), active_chunks())
    )
    if exclude_ids:
        stmt = stmt.where(Chunk.id.notin_(sorted(exclude_ids)))
    stmt = stmt.order_by(Chunk.ordinal).limit(max(1, int(room)))
    rows = (await session.execute(stmt)).all()
    out: list[Material] = []
    for row in rows:
        content = (row[1] or "").strip()
        if not content:
            continue
        out.append(
            Material(
                chunk_id=int(row[0]),
                document_id=int(node.source_id),
                content=content,
                block_ordinal=int(row[2] or 0),
                page=int(row[3]) if row[3] is not None else None,
                filename=node.title,
                category=node.category,
                kb_id=node.kb_id,
                graph_score=round(float(score), 6),
            )
        )
    return out


# --- 对外唯一入口 -------------------------------------------------------------


async def search_graph(
    session: AsyncSession,
    question: str,
    filters: dict | None = None,
    *,
    main_count: int = 0,
    exclude_ids: set[int] | None = None,
    cap: int = FUSE_LIMIT,
) -> list[Material]:
    """图通道召回：问题 token → 种子节点 → 一跳邻居 → 材料（**按通道分降序、有界**）。

    返回的材料带 :attr:`Material.graph_score`（新字段，只增不改义），可直接喂
    :func:`app.retrieval.fuse.fuse` 的 ``graph_hits``。

    - ``main_count`` = 主命中（关键词 + 向量去重后）条数，用来算名额（:func:`graph_budget`）；
    - ``exclude_ids`` = 主命中已有的 ``chunk_id``，图通道不再重复带出；
    - 任何异常 ⇒ **静默降级**返回 ``[]``（纯增益步骤不许炸穿，I5）：只记 ``logger.warning``
      与计数，不往调用方抛、也不往 warnings 里掺内部细节。
    """
    if not enabled():
        return []
    _bump("calls")
    tokens = _query_tokens(question)
    if not tokens:
        _bump("no_terms")
        return []
    budget = graph_budget(main_count, cap)
    if budget <= 0:
        _bump("no_budget")
        return []
    try:
        return await _run(session, tokens, filters, budget, set(exclude_ids or ()))
    except Exception as exc:  # noqa: BLE001 —— 纯增益步骤，失败静默降级（I5）
        _bump("failures")
        log.warning(
            "图谱检索通道失败，已静默跳过（本轮按无图通道处理）：%s: %s",
            exc.__class__.__name__,
            exc,
        )
        return []


async def _run(
    session: AsyncSession,
    tokens: list[str],
    filters: dict | None,
    budget: int,
    exclude_ids: set[int],
) -> list[Material]:
    """正常路径：取种子 → 扩一跳 → 排序 → 逐节点取材料（直到名额用完）。"""
    # ① 种子：问题 token 命中别名
    candidates = await _load_nodes(session, tokens, filters)
    scored: dict[tuple[str, int], tuple[float, _Node]] = {}
    seeds: list[tuple[float, _Node]] = []
    for node in candidates:
        weight = _seed_weight(node, tokens)
        if weight is None:
            continue
        seeds.append((weight, node))
        _keep_max(scored, node, weight)
    _bump("seeds", len(seeds))

    # ② 一跳邻居：wiki 边（精确目标）+ 共现边（共享别名）
    #
    # ⚠️ **两条边的邻居分不同源**（自测抓到的第一处真缺陷）：wiki 边是「种子的出链/入链」，
    # 邻居与种子**不一定共享别名**，所以它的强度只能取**那个种子自己**的强度；
    # 共现边才是「共享 ≥1 个别名」。两条都按 §2.4 同一公式降权（× 跳数衰减 × 通道系数）。
    neighbor_weights: dict[tuple[str, int], float] = {}
    neighbors: list[_Node] = []
    seen_keys = set(scored)
    adjacency = await load_wiki_adjacency(
        session, [(seed.kb_id, *seed.key) for _, seed in seeds],
    )

    for weight, seed in seeds:
        for key in await _wiki_neighbors(session, seed, adjacency):
            neighbor_weights[key] = max(neighbor_weights.get(key, 0.0), weight)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            # ⚠️ `filters` 必须传（专项 F6-C · 回归修复）：不传 ⇒ 回表只过三道门、
            # 不复核范围 ⇒ wiki 一跳会把**本轮范围外**的资料带进材料（种子与共现边本来就有过滤）。
            node = await _node_by_key(session, key, filters=filters)
            if node is not None:
                neighbors.append(node)

    terms = _expansion_terms(seeds)
    if terms:
        for node in await _load_nodes(session, terms, filters):
            weight = _shared_alias_weight(node, seeds)
            if weight is None:
                continue
            neighbor_weights[node.key] = max(neighbor_weights.get(node.key, 0.0), weight)
            if node.key in seen_keys:
                continue
            seen_keys.add(node.key)
            neighbors.append(node)

    decayed = _hop_decay() * _score_factor()
    for node in neighbors:
        weight = neighbor_weights.get(node.key)
        if weight is None:
            continue
        _keep_max(scored, node, weight * decayed)
    _bump("neighbors", len(neighbors))

    if not scored:
        _bump("no_seeds")
        return []

    # ③ 排序：通道分降序 → kind → id（确定性，同一批输入两次调用逐条相同）
    ordered = sorted(scored.values(), key=lambda item: (-item[0], item[1].kind, item[1].source_id))

    # ④ 取材料：一个节点一份，直到名额用完
    out: list[Material] = []
    for score, node in ordered:
        room = budget - len(out)
        if room <= 0:
            break
        out.extend(await _node_materials(session, node, score, room, exclude_ids))
    _bump("materials", len(out))
    if out:
        log.info(
            "图谱检索通道：种子 %d 个 / 邻居 %d 个 → 产出 %d 条材料（名额 %d）。",
            len(seeds),
            len(neighbors),
            len(out),
            budget,
        )
    return out


def _keep_max(
    scored: dict[tuple[str, int], tuple[float, _Node]], node: _Node, score: float
) -> None:
    """同一节点只留**最强**的一条（不累加——避免「标签多者通吃」）。"""
    current = scored.get(node.key)
    if current is None or score > current[0]:
        scored[node.key] = (float(score), node)


__all__ = [
    "MIN_ALIAS_CHARS",
    "enabled",
    "failure_counts",
    "graph_budget",
    "normalize_alias",
    "reset_counters",
    "search_graph",
]
