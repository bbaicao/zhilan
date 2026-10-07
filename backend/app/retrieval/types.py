"""检索层公共类型、过滤范围口径与资料区渲染（阶段 3 · 条目 3.1）。

本模块只放**两条路共用**的东西：

1. 材料快照类型 :class:`Material`（Qdrant point / ``chunks`` 行 / 融合结果的统一载体）；
2. 包级常量（条目 3.1 已定口径 D-d：用模块常量，**不新增配置键**）；
3. ``Filters`` 落库形态（dict）→ SQL 条件 / Qdrant payload 过滤 / Python 二次收口的**同一套口径**
   （三条路径共用一份实现，避免「两路筛选不一致」）；
4. 渲染给模型的**资料区**（``answer.build_messages`` 的 ``materials_text``）。

检索算法本身在 ``keyword`` / ``vector`` / ``fuse`` / ``rerank`` 四个模块里。
"""

from __future__ import annotations

import logging
from hashlib import sha256
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from app.models import Document, Note

log = logging.getLogger(__name__)

# --- 包级常量（条目 3.1 · D-d：模块常量，不新增配置键）----------------------

# 两路各召回多少条进融合（老基线取 top_k*2；这里按 D-d 定稿的固定值）
VECTOR_TOP_K = 20
KEYWORD_TOP_K = 20
# RRF 平滑常数（老基线 RAG_RRF_K 同为 60）
RRF_K = 60
# 融合后的材料上限：与条目 3.2 的 Ref 上限 12 对齐（「最多 12 条引用」从检索侧就封顶）
FUSE_LIMIT = 12

_BLANK_LINE = re.compile(r"\n[ \t]*\n")


@dataclass
class Material:
    """一条候选资料（分块快照）。

    字段分工（**勿混用两个 ordinal**）：

    - ``chunk_id`` = ``chunks.id``（**就是 Qdrant point id**，融合去重与引用归因都以它为准）；
    - ``block_ordinal`` = ``chunks.ordinal``（块在文档内的序号，用于拼冻结的 ``chunk_key``）；
    - ``ordinal`` = **本轮快照编号 [n]**（1 起连续），由 :func:`number` 在融合后一次性赋上；
      渲染与条目 3.2 的 ``Ref.ordinal`` 共用同一份，保证「模型看到的编号 = 引用卡片的编号」。

    ``url``（阶段 4 · 条目 4.1 **只增**）：网页材料的原地址。库内材料恒 ``None``，
    于是 ``source_type`` / ``chunk_key`` 也一并按「是否有 url」分流——这是**既有字段语义的
    自然延伸**，不是新造一套类型：Agent 轮的联网工具产出的材料因此能直接喂进
    ``citation.refs_from_materials``，不必复制一份引用组装逻辑（I3）。
    """

    chunk_id: int
    document_id: int
    content: str
    filename: str = ""
    category: str = ""
    kb_id: int = 1
    block_ordinal: int = 0
    # 只有 PDF 有页号；txt/md/office 恒 None（阶段 0.2 既定语义，取值时不要假设存在）
    page: int | None = None

    # 融合分（RRF）与两路子分，仅用于排序与证据展示
    score: float = 0.0
    vector_score: float | None = None
    keyword_score: float | None = None
    rerank_score: float | None = None

    # 快照编号 [n]（1 起）；0 = 尚未编号
    ordinal: int = 0

    #: 网页材料的原地址（阶段 4 · 4.1 新增）；库内材料恒 ``None``
    url: str | None = None

    #: 笔记材料的笔记 id（阶段 4 · 4.5.2 新增）；文档 / 网页材料恒 ``None``。
    #:
    #: ⚠️ **为什么不复用 ``document_id``**：``document_id`` 有两个既成的下游语义——
    #: ① ``Ref.source_id``（前端回链用）；② ``generation.citation.attribution_rows``
    #: 的「有没有真实分块行」判据（``document_id <= 0`` 就跳过，因为
    #: ``message_attributions.chunk_id`` 是指向 ``chunks`` 的外键）。笔记没有 ``chunks`` 行
    #: ⇒ 必须让 ② 判成立，否则会往外键里塞假行、连带**丢掉整轮**的文档归因。
    note_id: int | None = None

    #: **图通道分**（阶段 5 · 条目 5.3 新增，**只增不改义**）：由
    #: :mod:`app.retrieval.graph` 产出的材料带它，值是「别名强度 × 跳数衰减 × 通道系数」。
    #:
    #: ⚠️ **它不参与引用与归因的分数口径**：``generation.citation._normalized_scores`` /
    #: ``attribution_rows`` 取的一直是 :attr:`score`（融合后的 RRF 分），本字段只是
    #: 「这条材料是怎么被选中的」的**证据字段**（A4 断言与图通道单测都靠它识别）。
    graph_score: float | None = None

    # U3：仅内部片段和窗口坐标；Ref.note 身份仍恒 note:<id>:0。
    fragment_ordinal: int | None = None
    span_start: int | None = None
    span_end: int | None = None
    covered_ordinals: tuple[int, ...] = ()
    windowed: bool = False

    @property
    def retrieval_key(self) -> tuple:
        """内部去重包含来源类型与笔记片段，不复用正／负 chunk_id 编码。"""
        if self.is_web:
            return ("web", self.url or "")
        if self.is_note:
            return ("note", self.kb_id, int(self.note_id), self.fragment_ordinal or 0)
        return ("document", int(self.chunk_id))

    @property
    def is_web(self) -> bool:
        """是不是网页材料（有原地址）。"""
        return bool((self.url or "").strip())

    @property
    def is_note(self) -> bool:
        """是不是笔记材料（阶段 4 · 4.5.2）。"""
        return self.note_id is not None

    @property
    def source_ref(self) -> tuple[str, int] | None:
        """材料的**库内来源身份** ``(type, id)``：笔记用 ``note_id``、文档用 ``document_id``。

        一跳扩展（:mod:`app.retrieval.link`）用它去 ``wiki_links`` 里找边；网页材料不是库内
        来源，返回 ``None``（链接边表在本机 MySQL，与联网结果不在同一个 id 空间）。
        """
        if self.is_web:
            return None
        if self.note_id is not None:
            return ("note", int(self.note_id))
        return ("document", int(self.document_id))

    @property
    def source_type(self) -> str:
        """02 §3 的 ``Ref.type``：网页材料 ``web``、笔记材料 ``note``，其余 ``document``。

        ⚠️ 阶段 3 时本属性恒 ``"document"``（v2 语料只有文档分块）；阶段 4.1 起按 ``url``
        分流出 ``web``；阶段 4 · 条目 4.5.2（``回归修复`` 的 B-lite）起按 ``note_id``
        分流出 ``note``——**只补关键词路**，向量路不动（note id 与 chunk id 会同号）。
        """
        if self.is_web:
            return "web"
        return "note" if self.is_note else "document"

    @property
    def chunk_key(self) -> str:
        """冻结的 ``Ref.chunk_id`` 形态（02 §3）：``kb:<kb_id>:<source>:<id>:<ordinal>``。

        三种来源各自分流：

        - 网页材料没有 kb / 文档 id，用 ``web:<sha1[:16]>``（老基线 ``web_tools._chunk_id``
          同形）——它同时是 Agent 轮的材料去重键，必须**跨进程稳定**（故用 sha1，不用 ``hash()``）；
        - 笔记材料是**整条一条**（没有分块概念）⇒ 序位恒 ``0``，与老基线
          ``prefix_chunk_id(kb_id, chunk_key("note", note_id, 0))`` 逐字同形；
        - 文档材料按块（``block_ordinal``）。
        """
        if self.is_web:
            return web_chunk_key(self.url or "")
        if self.note_id is not None:
            return f"kb:{self.kb_id}:note:{int(self.note_id)}:0"
        return f"kb:{self.kb_id}:document:{self.document_id}:{self.block_ordinal}"


def material_identities(materials) -> list[dict]:
    """Retain snapshot identity and coordinates without recording source text."""
    return [
        {
            "chunk_id": item.chunk_id, "chunk_key": item.chunk_key,
            "source_id": item.source_ref[1] if item.source_ref else None,
            "source_type": item.source_type, "kb_id": int(item.kb_id),
            "ordinal": item.ordinal if type(item.ordinal) is int and item.ordinal > 0 else None,
            "content_sha256": sha256(item.content.encode("utf-8")).hexdigest(),
            "span_start": item.span_start, "span_end": item.span_end,
            "page": item.page, "covered_ordinals": list(item.covered_ordinals),
            "vector_score": item.vector_score, "keyword_score": item.keyword_score,
            "graph_score": item.graph_score, "rerank_score": item.rerank_score,
        }
        for item in (materials or [])
    ]


def web_chunk_key(url: str) -> str:
    """网页材料的冻结身份：``web:<sha1(url)[:16]>``（老基线同形；跨进程稳定）。"""
    from hashlib import sha1

    return f"web:{sha1((url or '').encode('utf-8')).hexdigest()[:16]}"


def web_chunk_id(url: str) -> int:
    """网页材料的 ``chunk_id`` 占位（**负数**）。

    ``Material.chunk_id`` 全链路按 int 用（去重键、``Ref.score`` 归一表的键、归因行的 FK）。
    网页材料没有真实的 ``chunks.id``，所以取 sha1 前 8 字节的负数——**负数与真实分块 id
    （自增正整数）不可能撞**，是这套共用类型能同时承载两种来源的关键。
    """
    from hashlib import sha1

    digest = sha1((url or "").encode("utf-8")).hexdigest()[:8]
    return -int(digest, 16)


def note_chunk_id(note_id: int) -> int:
    """笔记材料的 ``chunk_id`` 占位（**负数**，阶段 4 · 4.5.2）。

    ``Material.chunk_id`` 是融合的**同源去重键**（:func:`app.retrieval.fuse.fuse` 按 int 去重）。
    ``notes.id`` 与 ``chunks.id`` 是两张表各自的自增序列、**必撞号**（这正是决策包里「不动向量路」
    的理由）⇒ 笔记不能直接用 ``note_id`` 当 ``chunk_id``：一条 id=5 的笔记会把 id=5 的文档分块
    合并掉，正文被顶包。

    取负数与 :func:`web_chunk_id` 同一手法：真实分块 id 恒为正自增 ⇒ 不可能撞。
    两处负数的值域也无实质重叠风险（笔记是小的连续负整数，网页是 sha1 截断的大负整数），
    且**两类材料从不出现在同一次融合里**（网页材料由 Agent 联网工具产出，不进 ``retrieve()``）。
    """
    return -int(note_id)


def number(materials: list[Material]) -> list[Material]:
    """给快照编号 ``1..N``（渲染 ``[n]`` 与条目 3.2 的 ``Ref.ordinal`` 共用同一份）。"""
    for index, item in enumerate(materials or [], start=1):
        item.ordinal = index
    return materials


def new_retrieval_id() -> str:
    """铸造本轮**检索快照 id**：``rl-<12hex>``（02 §3 的 ``Ref.retrieval_id`` 形态）。

    由调用点（``chat_routes.build_turn_plan``）铸造，与这一轮的 ``materials`` 一起冻结；
    阶段 4 的 Agent 轮同样在这里取 id，保证「每条落库引用都能溯源到一次检索」。
    """
    return f"rl-{uuid4().hex[:12]}"


# --- 过滤范围口径（Filters 落库 dict → 三条路径共用的同一套判断）------------


def _as_list(value) -> list:
    """单值写法（``kb_id`` / ``source_type``）与列表写法统一成列表。"""
    if value in (None, "", []):
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def filter_kb_ids(filters: dict | None) -> list[int]:
    """归一 ``kb_ids`` / ``kb_id``（兼容单值），返回去重升序的 int 列表。"""
    out: list[int] = []
    data = filters or {}
    for raw in [*_as_list(data.get("kb_ids")), *_as_list(data.get("kb_id"))]:
        try:
            out.append(int(raw))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def filter_categories(filters: dict | None) -> list[str]:
    """归一 ``categories``（去空、去重、保持原顺序）。"""
    out: list[str] = []
    for raw in _as_list((filters or {}).get("categories")):
        text = str(raw).strip()
        if text and text not in out:
            out.append(text)
    return out


def filter_tags(filters: dict | None) -> list[str]:
    """归一 ``tags``：同一条资料需**同时**具备全部标签（老基线 ``_json_has`` 逐条 AND）。"""
    out: list[str] = []
    for raw in _as_list((filters or {}).get("tags")):
        text = str(raw).strip()
        if text and text not in out:
            out.append(text)
    return out


def filter_source_types(filters: dict | None) -> list[str]:
    """归一 ``source_types`` / ``source_type``（小写）。"""
    out: list[str] = []
    data = filters or {}
    for raw in [*_as_list(data.get("source_types")), *_as_list(data.get("source_type"))]:
        text = str(raw).strip().lower()
        if text and text not in out:
            out.append(text)
    return out


def includes_documents(filters: dict | None) -> bool:
    """范围是否包含文档分块（未给 ``source_types`` = 不限制 = 包含）。"""
    types = filter_source_types(filters)
    return ("document" in types) if types else True


def includes_notes(filters: dict | None) -> bool:
    """范围是否包含笔记（阶段 4 · 条目 4.5.2；未给 ``source_types`` = 不限制 = 包含）。

    与 :func:`includes_documents` 成对：两路（关键词路 / 向量路）各自按它决定要不要取该来源。
    **只补关键词路**——向量路要动 id 空间（Qdrant point id = ``str(chunk.id)``，与
    ``notes.id`` 必撞号），那是阶段 5 的事（``回归修复`` 决策包的 B 项）。
    """
    types = filter_source_types(filters)
    return ("note" in types) if types else True


#: v2 检索**真能提供**的来源类型（阶段 4 · 4.5.2 起含笔记）。
#:
#: 只用于「这条范围是不是不可满足」的判定（``api/chat_routes`` 的 §3.8「范围空」行）——
#: 与「范围内有没有命中的行」是两件事：前者判拒答，后者判未命中。
SUPPORTED_SOURCE_TYPES: tuple[str, ...] = ("document", "note")


def includes_supported_source(filters: dict | None) -> bool:
    """范围里是否含 v2 真能提供的来源类型。

    未给 ``source_types`` ⇒ 不限制 ⇒ 含（``True``）。给了就要求至少命中
    :data:`SUPPORTED_SOURCE_TYPES` 之一：``["note"]`` 在 B-lite 落地后**是可满足的范围**
    （关键词路能取笔记），只有 ``["web"]`` 这类库内根本不存在的来源才判不可满足。
    """
    types = filter_source_types(filters)
    if not types:
        return True
    return any(item in SUPPORTED_SOURCE_TYPES for item in types)


def _parse_dt(raw) -> datetime | None:
    """把契约里的时间串解析成**naive UTC**（``documents.created_at`` 是 UTC naive）。

    解析不了就当作「该维度未指定」并记一条警告——比静默返回空集更不容易让人误判成「库里没资料」。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        log.warning("过滤条件里的时间无法解析，已忽略该维度：%r", raw)
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def document_where(filters: dict | None) -> list:
    """``filters`` → SQL 条件（**下推**到关键词路的语料查询）。

    只下推能直接表达的维度：``kb_id`` / ``category`` / ``created_at`` 区间。
    ``tags`` 是 JSON 列（两方言语义不同）⇒ 统一在 :func:`matches_document` 用 Python 收口。
    """
    clauses: list = []
    kbs = filter_kb_ids(filters)
    if kbs:
        clauses.append(Document.kb_id.in_(kbs))
    cats = filter_categories(filters)
    if cats:
        clauses.append(Document.category.in_(cats))
    after = _parse_dt((filters or {}).get("created_after"))
    if after is not None:
        clauses.append(Document.created_at >= after)
    before = _parse_dt((filters or {}).get("created_before"))
    if before is not None:
        clauses.append(Document.created_at <= before)
    return clauses


def note_where(filters: dict | None) -> list:
    """``filters`` → SQL 条件（**下推**到关键词路的笔记语料查询）。

    与 :func:`document_where` 同一套维度、同一套解析：``kb_id`` / ``category`` /
    ``created_at`` 区间能直接表达就下推；``tags`` 是 JSON 列（两方言语义不同）⇒
    统一在 :func:`matches_note` 用 Python 收口。``source_types`` 不在这里判——那是
    「要不要取这一路」的开关，由 :func:`includes_notes` 决定。
    """
    clauses: list = []
    kbs = filter_kb_ids(filters)
    if kbs:
        clauses.append(Note.kb_id.in_(kbs))
    cats = filter_categories(filters)
    if cats:
        clauses.append(Note.category.in_(cats))
    after = _parse_dt((filters or {}).get("created_after"))
    if after is not None:
        clauses.append(Note.created_at >= after)
    before = _parse_dt((filters or {}).get("created_before"))
    if before is not None:
        clauses.append(Note.created_at <= before)
    return clauses


def payload_filter(filters: dict | None) -> dict | None:
    """``filters`` → Qdrant payload 精确匹配参数（**下推**到向量路）。

    ⚠️ 向量库 payload 只有 ``document_id/ordinal/kb_id/filename/category`` 五个键，
    且协议只支持**单键单值精确匹配**（``VectorStore.query(filters=...)``）⇒ 只有「恰好一个
    kb_id / 恰好一个 category」时才能下推；其余维度（含 ``tags`` / ``source_types`` /
    ``created_*``）一律靠 :func:`matches_document` 回表后再收口。
    这是协议面限制，**不在本条目扩协议**（阶段 3 白名单：``app/vector/base.py`` 不动）。
    多值 ``kb_ids`` 由 :func:`fanout_filters` 先拆成单值、再逐份调用本函数（``回归修复``）。
    """
    out: dict = {}
    kbs = filter_kb_ids(filters)
    if len(kbs) == 1:
        out["kb_id"] = kbs[0]
    cats = filter_categories(filters)
    if len(cats) == 1:
        out["category"] = cats[0]
    return out or None


def fanout_filters(filters: dict | None) -> list[dict | None]:
    """:func:`payload_filter` 的**前置展开**：多值 ``kb_ids`` 拆成「每个 kb 值一份」的 filters。

    :func:`payload_filter` 只在「恰好一个 ``kb_id``」时才下推 kb 维度（协议只支持单键单值）
    ⇒ 多值原本会被整条丢掉（``回归修复``：``kb_ids=[1,2]`` 直接**不带过滤**发全库查询；
    ``kb_ids=[1,2] + categories=[A]`` 更隐蔽，只推 category、**kb 维度被静默丢弃**）。
    这里把多值展开成 N 份**单值** filters，由调用方逐份查询再合并
    （见 :func:`app.retrieval.vector.search_vectors`）——**不必扩 ``app/vector`` 协议**。

    - 未给 kb（``None`` / ``[]``）⇒ **原样一份**（调用方仍只发一次查询）；
    - 恰好一个 kb ⇒ **原样一份**（单值路径**逐条不变**，这是修复的硬约束）；
    - N 个 kb ⇒ N 份，每份把 kb 维度窄化成该值、**其余维度原样带上**
      （能下推的照旧下推，推不动的仍由 :func:`matches_document` 回表收口）。

    只展开 kb：``categories`` / ``tags`` / ``source_types`` / ``created_*`` 的边界**本轮不动**
    （多值 category 仍不下推、``tags`` 与 ``created_*`` 在 payload 里根本不可表达）。
    """
    kbs = filter_kb_ids(filters)
    if len(kbs) <= 1:
        return [filters]
    out: list[dict] = []
    for kb in kbs:
        item = dict(filters or {})
        item.pop("kb_id", None)  # 单值别名先清掉，免得与下面窄化后的 kb_ids 打架
        item["kb_ids"] = [kb]
        out.append(item)
    return out


def matches_document(
    filters: dict | None,
    *,
    kb_id: int | None,
    category: str | None,
    tags: list | None,
    created_at: datetime | None,
) -> bool:
    """回表后的**统一二次收口**（向量路与关键词路共用同一套语义）。

    ``tags`` 是 JSON 列、两方言语义不同，所以它只在这里判：同一条资料要**同时**具备全部标签。
    """
    if not filters:
        return True
    if not includes_documents(filters):
        return False
    kbs = filter_kb_ids(filters)
    if kbs and kb_id is not None and int(kb_id) not in kbs:
        return False
    cats = filter_categories(filters)
    if cats and (category or "") not in cats:
        return False
    want_tags = filter_tags(filters)
    if want_tags:
        owned = {str(item).strip() for item in (tags or []) if str(item).strip()}
        if not all(tag in owned for tag in want_tags):
            return False
    after = _parse_dt((filters or {}).get("created_after"))
    if after is not None and created_at is not None and created_at < after:
        return False
    before = _parse_dt((filters or {}).get("created_before"))
    if before is not None and created_at is not None and created_at > before:
        return False
    return True


def matches_note(
    filters: dict | None,
    *,
    kb_id: int | None,
    category: str | None,
    tags: list | None,
    created_at: datetime | None,
) -> bool:
    """笔记的**回表二次收口**（阶段 4 · 条目 4.5.2）。

    与 :func:`matches_document` 是同一套判据、同一套语义（kb / category / tags 全 AND /
    时间区间），唯一差别是**放行门用 :func:`includes_notes`**：``source_types=["note"]``
    必须让笔记通过——用文档那套 ``includes_documents`` 会把「只要笔记」的范围清空，
    正是 ``回归修复`` 的静默零命中。

    不合并成「一个函数带 kind 参数」是刻意的：两条路的语料查询在 SQL 侧就分开了
    （``keyword._load_corpus`` 的 ``chunks ⋈ documents`` 与笔记查询），回表收口跟着
    各自的语料走，读起来不会串。两组用例都把「错用另一个门」钉成了反向断言。
    """
    if not filters:
        return True
    if not includes_notes(filters):
        return False
    kbs = filter_kb_ids(filters)
    if kbs and kb_id is not None and int(kb_id) not in kbs:
        return False
    cats = filter_categories(filters)
    if cats and (category or "") not in cats:
        return False
    want_tags = filter_tags(filters)
    if want_tags:
        owned = {str(item).strip() for item in (tags or []) if str(item).strip()}
        if not all(tag in owned for tag in want_tags):
            return False
    after = _parse_dt((filters or {}).get("created_after"))
    if after is not None and created_at is not None and created_at < after:
        return False
    before = _parse_dt((filters or {}).get("created_before"))
    if before is not None and created_at is not None and created_at > before:
        return False
    return True


# --- 工具侧范围求交（专项 F6-A · `回归修复`）-----------------------------
#
# 模型交给工具的 ``filters`` 是**信任边界**：它只能在本轮宿主范围（HTTP 请求体的
# ``Filters.to_stored()``）之内**再收窄**，不能覆盖、更不能放宽宿主条件（讨论终稿 D1）。
# 这是**纯函数**：handler 调它、单测直接调它——不新造过滤框架，也不反向 import
# ``app.api.chat_schemas``（HTTP 模型属于上层，见 ``设计说明`` 的分包方向）。
#
# ⚠️ 本段**不得**出现 ``Path(...)`` / ``open(...)`` / ``resolve()`` 这类**路径消费调用**
# —— ``backend/scripts/check_frozen_paths.py`` 是静态闸门，``build.ps1`` 会跑它。

#: 工具 ``filters`` **认识的键**（含单值别名）；其余键报参数错误。
#:
#: 为什么不静默忽略拼错的键：忽略 = 少一层限制 = **放宽**，正是本项要堵的方向。
_TOOL_FILTER_KEYS: frozenset[str] = frozenset(
    {
        "kb_ids",
        "kb_id",
        "source_types",
        "source_type",
        "categories",
        "tags",
        "created_after",
        "created_before",
    }
)

#: 工具侧允许的来源类型（与 :data:`SUPPORTED_SOURCE_TYPES` 同源）。
#:
#: **只在工具侧用**：宿主侧仍按现有宽松口径保留 ``["web"]`` 这类此刻不可满足的来源
#: （不可删成全库，见 §2.4）；两侧取交为空时由 :func:`_intersect_scope_dimension` 判冲突。
_TOOL_SOURCE_TYPES: frozenset[str] = frozenset(SUPPORTED_SOURCE_TYPES)


def _tool_ids(key: str, value) -> list[int]:
    """工具 ``kb_ids`` / ``kb_id``：JSON 整数（**排除 bool**——它其实是 ``int`` 子类）。

    实测 ``filter_kb_ids({"kb_ids": [True]})`` → ``[1]``：宽松归一器会把布尔当整数收下，
    所以信任边界必须自己挡。字符串、小数、字典、列表里的 ``null`` 一并报错。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return []
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    out: list[int] = []
    for raw in items:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError(f"filters.{key} 只接受 JSON 整数，收到 {raw!r}")
        if int(raw) not in out:
            out.append(int(raw))
    return out


def _tool_texts(key: str, value, *, allowed: frozenset[str] | None = None) -> list[str]:
    """工具 ``categories`` / ``tags`` / ``source_types``：字符串列表（strip + 去重）。

    ``allowed`` 非空时（来源类型）先 strip + 小写再校验：**未知非空值报错**，
    不静默丢弃后变成「不限」。非字符串、以及非空列表里的空白项同样报错。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return []
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for raw in items:
        if not isinstance(raw, str):
            raise ValueError(f"filters.{key} 只接受字符串，收到 {raw!r}")
        text = raw.strip()
        if not text:
            raise ValueError(f"filters.{key} 不接受空白项（要放开这一维就整项省略或给 []）")
        if allowed is not None:
            text = text.lower()
            if text not in allowed:
                raise ValueError(
                    f"filters.{key} 不支持的取值 {raw!r}（只支持 "
                    + "、".join(sorted(allowed))
                    + "）"
                )
        if text not in out:
            out.append(text)
    return out


def _tool_dt(key: str, value) -> datetime | None:
    """工具日期维度：非空必须是可解析的 ISO 日期/时间字符串。

    ⚠️ **不能指望 :func:`_parse_dt` 报错**：它先 ``str(raw)`` 再 ``fromisoformat`` ⇒
    数字与容器会被静默转成字符串（``{"created_after": 20260919}`` 能过）。信任边界只能
    自己挡：只接受字符串与 ``datetime``，其余类型一律报错；``Z`` 与 offset 统一折成
    naive UTC（与 :func:`_parse_dt` 的比较口径一致）。

    ``datetime`` 分支不是死代码：JSON 走不到它（``dict[str, Any]`` 不造 ``datetime``），
    但**进程内直调**（单测 / 其它 Python 调用方）构造 ``KnowledgeSearchArgs`` 时能传进来
    ——放行它是「不把合法类型误判成坏值」，不是放宽。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"filters.{key} 不是可解析的 ISO 日期/时间：{value!r}") from exc
    else:
        raise ValueError(
            f"filters.{key} 只接受 ISO 日期/时间字符串，收到 {type(value).__name__}"
        )
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _dedup(items: list[str]) -> list[str]:
    """保序去重（``categories`` / ``source_types`` 用；``kb_ids`` 另按数值排序）。"""
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _scope_side(filters: dict | None, *, strict: bool) -> dict:
    """把**一侧**的范围归一成规范形（``strict=True`` = 工具信任边界校验）。

    两侧**各自归一之后再合并**：先合并两个原始 dict 会让「同侧单值别名」与列表值打架
    ——§2.4 已核实 ``to_stored()`` **不做别名归一**（只丢 ``None`` / ``[]`` / ``""``），
    「同侧别名先合并」是 :func:`filter_kb_ids` / :func:`filter_source_types` 的语义，
    两件事不能写在一处。

    宿主侧走**现有宽松口径**（坏日期按原规则忽略并记警告）——本项不把 HTTP 兼容规则
    偷偷改成 422。工具侧则严格校验（见上三个 ``_tool_*``）。
    """
    data = filters if filters is not None else {}
    if not isinstance(data, dict):
        raise ValueError(f"filters 必须是 JSON 对象，收到 {type(data).__name__}")
    if not strict:
        return {
            "kb_ids": filter_kb_ids(data),
            "source_types": filter_source_types(data),
            "categories": filter_categories(data),
            "tags": filter_tags(data),
            "created_after": _parse_dt(data.get("created_after")),
            "created_before": _parse_dt(data.get("created_before")),
        }
    unknown = sorted(str(key) for key in data if key not in _TOOL_FILTER_KEYS)
    if unknown:
        raise ValueError(
            "filters 里有不认识的键：" + "、".join(unknown) + "（拼错的键不会被忽略）"
        )
    st_multi = _tool_texts("source_types", data.get("source_types"), allowed=_TOOL_SOURCE_TYPES)
    st_single = _tool_texts("source_type", data.get("source_type"), allowed=_TOOL_SOURCE_TYPES)
    return {
        "kb_ids": sorted(
            set(_tool_ids("kb_ids", data.get("kb_ids"))) | set(_tool_ids("kb_id", data.get("kb_id")))
        ),
        "source_types": _dedup([*st_multi, *st_single]),
        "categories": _tool_texts("categories", data.get("categories")),
        "tags": _tool_texts("tags", data.get("tags")),
        "created_after": _tool_dt("created_after", data.get("created_after")),
        "created_before": _tool_dt("created_before", data.get("created_before")),
    }


def _intersect_scope_dimension(label: str, left: list, right: list) -> list:
    """「有限集合」维度：两侧都有限制取交，只有一侧有限制就保留该侧。

    空列表在**每一侧**的既有语义都是「这一侧不限」⇒ **绝不能**把「交集为空」编码成
    ``[]`` 返回：下游会把它读成「不限」（``Filters(kb_ids=[]).to_stored() is None``
    正是这个坑的探针证据）。交集为空 = **真冲突** = 抛错。
    """
    if not left or not right:
        return list(left or right)
    allowed = set(right)
    out = [item for item in left if item in allowed]
    if not out:
        # ⚠️ **不回显两侧的具体取值**：观察文本会随工具结果发给上游模型，而宿主那一侧是
        # **用户选的范围**（kb / 分类名等库内数据）。§2.5 只要求「点明冲突维度」，照做。
        raise ValueError(f"检索范围冲突：{label}与本轮范围没有交集，未执行检索。")
    return out


def intersect_scope_filters(
    host_filters: dict | None, requested_filters: dict | None
) -> dict | None:
    """**全仓唯一的 AND 合并实现**：把请求侧范围**收窄进**宿主范围，返回合并后的规范范围。

    ⚠️ **这是本仓唯一一处范围求交**（专项工具调用第 0 批 · 裁决 D15）：``intersect_tool_filters``
    只是它的**薄壳**（历史名，F6-A 的钉子按那个名字钉）。**禁止**在别处再写第二套合并逻辑
    ——「两个入口共用同一处范围求交函数」是本条的不变量（终稿 §二 · D14）。

    命名为什么从 ``*_tool_filters`` 泛化成 ``*_scope_filters``：第 2 批的 `list_documents` /
    `read_document` 等**本地资料工具**同样吃这一套收窄，而「范围只能收窄」与「工具是不是检索
    类」无关。泛化**只改名字与入口**，逐字段语义、抛错语义、返回形状一律未动。

    两个参与方：

    - ``host_filters``：本轮宿主范围（当轮 HTTP 请求体的 ``Filters.to_stored()``），
      走现有宽松归一；
    - ``requested_filters``：请求侧（当前唯一生产者 = 模型给 ``knowledge_search`` 的
      ``filters`` 参数）给的范围，走**严格校验**（这是信任边界；坏值一律报错，不静默归一为不限）。

    逐维度口径：

    - ``kb_ids`` / ``kb_id``、``source_types`` / ``source_type``、``categories``：
      两侧都有限制取交、只有一侧有限制保留该侧；同侧的单值/多值别名先按既有语义合并；
    - ``tags``：两侧**取并集**（单条资料必须同时具备每个标签 = AND 语义原样保留；
      取交会把宿主要求的标签漏掉，那等于放宽）；
    - ``created_after`` 取**更晚**者、``created_before`` 取**更早**者；
    - 有限集合交集为空、合并后起点晚于终点 ⇒ 抛 :class:`ValueError`（**不是**返回
      ``[]`` / ``{}`` / ``None``）；起点**等于**终点合法。

    ⚠️ **宿主侧同样受「起点晚于终点即冲突」的约束**（§2.3 按字面）：即便模型**一个
    filters 都没给**，宿主自带反向区间时这里也会抛冲突 ⇒ 工具那次变成 ``ok=False`` 的
    失败观察、**检索调用为 0**。改前同一请求是「正常检索、0 命中」；非 agent 路径至今
    仍是空结果（本项只改工具信任边界，D6/D7）⇒ 两条链路对该**退化输入**分路不同，
    这是**有意的**（D4：不可满足的范围必须在检索前拒绝，不能编码成空集冒充「无命中」）。
    另有 ``tests/agent/test_knowledge_scope_f6.py`` 的正控把它钉死。

    返回：成功时是**规范范围**——只保留 ``kb_ids`` / ``source_types``，不再残留
    ``kb_id`` / ``source_type`` 单值别名（免得下游又把别名并回来）；**双方都不限**时返回
    ``None``（维持「无范围」原行为）。输入字典与其中的列表**一个都不改**：下一次工具调用
    不能继承上一条的缩窄结果。
    """
    host = _scope_side(host_filters, strict=False)
    requested = _scope_side(requested_filters, strict=True)

    kb_ids = _intersect_scope_dimension("知识库", host["kb_ids"], requested["kb_ids"])
    source_types = _intersect_scope_dimension(
        "来源类型", host["source_types"], requested["source_types"]
    )
    categories = _intersect_scope_dimension("分类", host["categories"], requested["categories"])
    # 标签取**并集**：两侧都是「必须同时具备」的 AND 语义 ⇒ 并集才是 AND 的合并方式
    tags = [*host["tags"], *(item for item in requested["tags"] if item not in host["tags"])]

    after = max(
        (item for item in (host["created_after"], requested["created_after"]) if item is not None),
        default=None,
    )
    before = min(
        (item for item in (host["created_before"], requested["created_before"]) if item is not None),
        default=None,
    )
    if after is not None and before is not None and after > before:
        raise ValueError("检索范围冲突：起始时间晚于结束时间，未执行检索。")

    merged: dict = {}
    if kb_ids:
        merged["kb_ids"] = kb_ids
    if source_types:
        merged["source_types"] = source_types
    if categories:
        merged["categories"] = categories
    if tags:
        merged["tags"] = tags
    if after is not None:
        merged["created_after"] = after.isoformat()
    if before is not None:
        merged["created_before"] = before.isoformat()
    return merged or None


def intersect_tool_filters(
    host_filters: dict | None, requested_filters: dict | None
) -> dict | None:
    """``knowledge_search`` 的**历史名**入口 —— 薄壳，转调 :func:`intersect_scope_filters`。

    ⚠️ **为什么保留这个名字**（而不是直接改名）：``tests/agent/test_knowledge_scope_f6.py``
    的 A1–A9 与 A12 **按这个名字**钉 F6-A 的形状（``__all__`` 里含它、签名与位置入参隐含
    覆盖、返回规范形、抛错消息子串）——本批的纪律是「F6-A 的钉子**只许重跑、不许改判据**」，
    改名会把该文件打成红，那等于**用改判据掩盖回归**。

    ⚠️ **两个名字是同一份实现**（不是两份逻辑）：实现体只有 :func:`intersect_scope_filters`
    一处；本函数的 ``__doc__`` 与签名都不承担语义，改行为只许改那一边。
    """
    return intersect_scope_filters(host_filters, requested_filters)


# --- 资料区渲染 -------------------------------------------------------------


def normalize_body(content: str) -> str:
    """正文归一（老基线 ``P3-回归修复``）：统一换行、丢掉空行与全空白行、去掉首尾换行。

    块与块之间用空行分隔（``"\\n\\n".join``）；块内若留着空行，模型就分不清「这行空行是
    块内换行还是下一块的开头」，编号与内容的对应随之漂移。
    """
    text = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _BLANK_LINE.sub("\n", text)
    return "\n".join(line for line in text.split("\n") if line.strip()).strip()


#: 单条材料**正文**的字符上限（专项-上下文 · CX-06′）。
#:
#: **为什么需要**：主命中块受 ``RAG_CHUNK_SIZE``（800，``core/config.py:244``）约束、关联片段
#: 受 ``LINK_SNIPPET_CHARS``（800）约束、网页材料受 ``fetch_page`` 的 4000 约束，**只有笔记材料
#: 取全文**（``retrieval/graph.py:625,632``）⇒ **单条可无界**。
#:
#: **为什么取 4000**：与仓内既有的「单条」刻度**同口径** —— ``agent/runner.py:140`` 的
#: ``TOOL_CONTEXT_ITEM_LIMIT`` = 4000、``tools/fetch_page`` 的 ``FETCH_CONTENT_CLIP`` = 4000、
#: ``WEB_MAX_CONTENT``（``core/config.py:371``）= 4000。⇒ 文档块（≤800）、关联片段（≤800）、
#: **网页材料（≤4000）三类「逐字节不受影响」**，只有**笔记全文**这一类被兜住（它此前无上界）。
#:
#: ⚠️ **只做块内头尾裁，绝不丢条**：材料条数 / 编号 ``[n]`` 与 ``all_refs`` 必须同源（I3），
#: 整条丢弃不是「裁短」，是**改写溯源**（引用卡会错位）。
MATERIAL_ITEM_MAX_CHARS = 4000

#: 单条截断标记（与 ``agent.runner.TOOL_CONTEXT_TRUNCATION_MARK`` **同族口径**：
#: 标明原量、不静默截断）。
MATERIAL_ITEM_TRUNCATION_MARK = "（本条已截断，原 {chars} 字符；保留头尾）"


def clip_material_body(body: str, limit: int | None = None) -> str:
    """把**单条**材料正文裁到 ``limit`` 字符（默认 :data:`MATERIAL_ITEM_MAX_CHARS`）。

    - 上限**只作用于正文**：标记行 ``[n]（来源：标题）`` **不计**在预算内；
    - 未超限 ⇒ **逐字节原样返回**（正常材料走这条，行为与加上限之前**相同**）；
    - 超限 ⇒ **头尾各留一半** + 插入 :data:`MATERIAL_ITEM_TRUNCATION_MARK`，
      返回长度 ≤ ``limit``；``limit <= 0`` 视为「不裁」；
    - **纯函数**：同一输入两次调用逐字节相同（与 :func:`render_materials` 同一条纪律）。
    """
    cap = MATERIAL_ITEM_MAX_CHARS if limit is None else int(limit)
    text = body or ""
    if cap <= 0 or len(text) <= cap:
        return text
    mark = "\n" + MATERIAL_ITEM_TRUNCATION_MARK.format(chars=len(text)) + "\n"
    budget = max(0, cap - len(mark))
    head = budget // 2
    tail = budget - head
    if tail <= 0:
        return f"{text[:head]}{mark}"
    return f"{text[:head]}{mark}{text[-tail:]}"


def render_materials(materials: list[Material]) -> str:
    """把快照渲染成**带编号的资料区**（**除新增的单条上限**外逐字同形于老基线 ``rag/answer.py::build_context``）。

    - 标记行 ``[n]（<来源>：<标题>）``，编号取快照自带的 ``ordinal``；
      ``ordinal`` 缺失 / 非正时按位置回落（老基线唯一的那条回落路径，正常快照不会走到）；
    - **来源标签按材料类型分流**（阶段 4 · 4.5.2）：笔记 ``笔记``、网页 ``网页``、
      其余 ``文档``——与老基线 ``build_context`` 的三分支逐字一致（老基线 :171-180）。
      v2 阶段 3 只有文档一种来源时恒为 ``文档``，故此前的实现只写死了一个标签；
      笔记来源落地后若不分流，模型会看到「（文档：我的笔记）」并把它当文档引用；
    - 正文为空时只留标记行，不留悬空空行；
    - **单条正文上限**（专项-上下文 · CX-06′）：超 :data:`MATERIAL_ITEM_MAX_CHARS` 的**单条**
      走 :func:`clip_material_body`（头尾裁 + 标记）；**条数不变**（I3 同源），正常块不受影响；
    - 纯函数：同一份输入两次调用逐字节相同（不读全局、不排序、不随机）。
    """
    blocks: list[str] = []
    for position, item in enumerate(materials or [], start=1):
        number_ = item.ordinal if isinstance(item.ordinal, int) and item.ordinal >= 1 else position
        source_type = item.source_type
        if source_type == "note":
            label = "笔记"
        elif source_type == "web":
            label = "网页"
        else:
            label = "文档"
        head = f"[{number_}]（{label}：{item.filename}）"
        # 已选窗口保留代码缩进、围栏及段间空行；重排与生成读取同一正文。
        body = (
            item.content.strip("\r\n")
            if item.windowed or item.span_start is not None
            else clip_material_body(normalize_body(item.content))
        )
        blocks.append(f"{head}\n{body}" if body else head)
    return "\n\n".join(blocks)


__all__ = [
    "FUSE_LIMIT",
    "KEYWORD_TOP_K",
    "RRF_K",
    "SUPPORTED_SOURCE_TYPES",
    "Material",
    "VECTOR_TOP_K",
    "document_where",
    "fanout_filters",
    "filter_categories",
    "filter_kb_ids",
    "filter_source_types",
    "filter_tags",
    "includes_documents",
    "includes_notes",
    "includes_supported_source",
    "intersect_scope_filters",
    "intersect_tool_filters",
    "matches_document",
    "matches_note",
    "new_retrieval_id",
    "MATERIAL_ITEM_MAX_CHARS",
    "MATERIAL_ITEM_TRUNCATION_MARK",
    "clip_material_body",
    "normalize_body",
    "note_chunk_id",
    "note_where",
    "number",
    "payload_filter",
    "render_materials",
    "web_chunk_id",
    "web_chunk_key",
]
