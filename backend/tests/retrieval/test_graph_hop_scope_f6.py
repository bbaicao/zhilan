"""专项 F6-C（``回归修复``）用例：**图通道一跳**不得绕过本轮有效范围。

## 这条缺陷是什么

``graph._run`` 把 ``filters`` 用在**种子**（``_load_nodes``）与**共现邻居**（同一批装载器）上，
但 **wiki 一跳**走的是另一条路：``_wiki_neighbors`` 只产 ``(kind, id)`` 键，回表在
``_node_by_key`` 里做——那里只过三道门（未软删 / ready / 别名非空），**不复核本轮范围**。
于是「问题里的词命中了某篇资料的**身份**」时，它的互链邻居会**无条件**被带进材料。

## 落点只有一个：``_node_by_key``

``_node_by_key`` 里 ``session.get(...)`` 拿到的就是**真实 ORM 行**，两个 matcher
（``matches_note`` / ``matches_document``）能**零额外查询**直接判；而到了 ``_node_materials``
（下游）文档的 ORM 行**已经不在作用域**（它只读 ``Chunk``，``_Node`` 又不带 tags / created_at）
⇒ 往那里挪会变成「多一次查询且拿不到那两列」。本文件因此按「三道门之后再加一道范围门」来钉。

## 关键设计：让越界邻居**只能**来自 wiki 一跳

- 宿主范围不含 ``categories`` / ``tags``，只按 ``kb_ids + source_types + created_after`` 收口
  ⇒ 范围内目标的**身份列里不含任何扩展词**，共现边（``_candidate_clauses`` 反查）找不到它；
- 种子与两个目标**同库同域**、一条已有的 ``wiki_links`` 边连过去
  ⇒ 该目标进得来，只可能是 wiki 一跳带进来的（归属唯一，差集才分得清）；
- 越界目标满足「**过得了三道门、过不了范围**」（未软删 / 正文非空 / 别名非空）。

## 用例与断言的对应

| 用例 | 断言 |
|---|---|
| ``test_c1_wiki_neighbor_*`` | **C1**（范围外 wiki 邻居不得进入 ``search_graph`` 与 ``retrieve()``；笔记域） |
| ``test_c1_document_domain_*`` | **C1**（同一个面在**文档域**再钉一遍） |
| ``test_c2_*`` | **C2**（种子 / 共现既有过滤保持原样；``source_types`` 不能被邻居绕过） |
| ``test_c3_no_scope_control_*`` | **C3**（无范围正控：逐条相同；合法 wiki 边**正常扩展**） |
| ``test_c3_disabled_*`` | **C3**（关态 ``search_graph`` **零调用**，开态反向对照） |
| ``test_c4_run_hands_*`` | **C4**（``_run`` 把范围交给回表；**M6 调用点变异的检出器**） |
| ``test_c_silent_degrade_*`` | **C-d / §0.2②**（``wiki_links`` 失败仍静默降级，**异常继续上抛**） |
| ``test_c_no_new_counter_names_*`` | **§0.2①**（不新增计数器名 —— 多出来的键会透传到 ``/api/agent/audit``） |

**本文件不连真服务也不依赖本机 ``.env``**：``tests/retrieval/`` 没有 conftest 级 autouse 钉子
⇒ 两把开关与出网点都在 :func:`_pin_defaults` 里显式钉死（E11 / E12）。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core.config import settings
from app.models import Chunk, Document, Note, WikiLink
from app.retrieval import graph
from app.retrieval.types import FUSE_LIMIT

#: 宿主（用户）本轮的资料范围：库 1 / 只**笔记** / 2026 年后（刻意不含 categories / tags，
#: 好让范围内目标的身份列不含扩展词 ⇒ 共现边找不到它 ⇒ 归属唯一）
SCOPE = {"kb_ids": [1], "source_types": ["note"], "created_after": "2026-01-01"}
SCOPE_DOC = {"kb_ids": [1], "source_types": ["document"], "created_after": "2026-01-01"}

QUESTION = "Allowed"

_NEW = datetime(2026, 9, 20)
_OLD = datetime(2020, 1, 1)

#: ``graph._COUNTERS`` 的**既有** 8 个键（键名与 ``failure_counts()`` 逐字一致）
_KNOWN_COUNTERS = {
    "calls",
    "seeds",
    "neighbors",
    "materials",
    "failures",
    "no_terms",
    "no_budget",
    "no_seeds",
}


class _EmptyStore:
    """空结果假向量库（离线没有 embedding 配额，向量路显式不可用）。"""

    def query(self, vector, limit: int = 5, filters=None):  # noqa: ANN001
        return []


def _nothing_really(*args, **kwargs):  # pragma: no cover - 只用于替换解释器行为
    raise AssertionError("本文件不该走网络")


@pytest.fixture(autouse=True)
def _pin_defaults(monkeypatch):
    """钉住两把开关 + 隔离出网点（**本文件所有用例都适用**；E11 / E12）。

    ``RAG_LINK_EXPAND=False``：尾部一跳扩展由 ``test_link_scope_f6.py`` 单独覆盖；
    这里关掉它，越界材料就**只可能**来自图通道（归属唯一 —— 同 ``f6_graph_hop_probe.py`` 的隔离）。
    """
    from app.retrieval import vector as vector_mod
    from app.vector.embed import FakeEmbedding

    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)
    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", False)
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", False)
    store = _EmptyStore()
    monkeypatch.setattr(vector_mod, "get_vector_store", lambda **kwargs: store)
    monkeypatch.setattr(
        vector_mod, "get_embedding_client", lambda *, fake=False: FakeEmbedding(dim=16)
    )
    graph.reset_counters()
    yield
    graph.reset_counters()


# --------------------------------------------------------------------------
# 造行助手（本文件自足；越界邻居必须满足「过得了三道门、过不了范围」）
# --------------------------------------------------------------------------


async def _seed_note(
    db, *, title: str, content: str, kb_id: int = 1, tags=None, category: str = "", created_at=None
) -> Note:
    note = Note(title=title, content=content, kb_id=int(kb_id), tags=tags, category=category)
    if created_at is not None:
        note.created_at = created_at
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


async def _seed_document(
    db, *, filename: str, content: str = "正文", kb_id: int = 1, category: str = "", created_at=None
) -> Document:
    document = Document(
        filename=filename,
        file_type="md",
        status="ready",
        index_status="ready",
        kb_id=int(kb_id),
        category=category,
        chunk_count=1,
    )
    if created_at is not None:
        document.created_at = created_at
    db.add(document)
    await db.commit()
    await db.refresh(document)
    db.add(Chunk(document_id=document.id, content=content, ordinal=0))
    await db.commit()
    return document


async def _seed_edge(
    db,
    *,
    kind: str,
    source_id: int,
    target_id: int,
    kb_id: int = 1,
    target_title: str | None = None,
) -> WikiLink:
    """同域（note↔note / document↔document）的一条已解析边。

    ``target_title`` 是 NOT NULL 列，且与 ``(source_type, source_id)`` 组成唯一键
    （``uk_wiki_links_source_target``）⇒ 同一个种子的两条边必须给不同标题；检索侧不读它。
    """
    edge = WikiLink(
        source_type=kind,
        source_id=int(source_id),
        kb_id=int(kb_id),
        target_title=str(target_title if target_title is not None else f"目标 {kind} {target_id}"),
        resolved=True,
    )
    if kind == "note":
        edge.target_note_id = int(target_id)
    else:
        edge.target_document_id = int(target_id)
    db.add(edge)
    await db.commit()
    return edge


async def _wiki_scenario_notes(db):
    """笔记域：种子（在范围内）+ 范围内邻居 + 范围外邻居（只坏日期这一维）。"""
    seed = await _seed_note(db, title=QUESTION, content=f"{QUESTION} text", created_at=_NEW)
    inside = await _seed_note(db, title="Inside Note", content="Inside payload", created_at=_NEW)
    outside = await _seed_note(db, title="Outside", content="Outside payload", created_at=_OLD)
    await _seed_edge(db, kind="note", source_id=seed.id, target_id=inside.id, target_title=inside.title)
    await _seed_edge(db, kind="note", source_id=seed.id, target_id=outside.id, target_title=outside.title)
    return seed, inside, outside


async def _wiki_scenario_documents(db):
    """文档域：与笔记域同构（``_node_by_key`` 的文档分支另走一条）。"""
    seed = await _seed_document(
        db, filename=f"{QUESTION}.md", content=f"{QUESTION} text", created_at=_NEW
    )
    inside = await _seed_document(db, filename="Inside.md", content="Inside payload", created_at=_NEW)
    outside = await _seed_document(
        db, filename="Outside.md", content="Outside payload", created_at=_OLD
    )
    await _seed_edge(
        db, kind="document", source_id=seed.id, target_id=inside.id, target_title=inside.filename
    )
    await _seed_edge(
        db, kind="document", source_id=seed.id, target_id=outside.id, target_title=outside.filename
    )
    return seed, inside, outside


# ==========================================================================
# C1 —— 范围外的 wiki 邻居不得进入产出（笔记域 / 文档域）
# ==========================================================================


@pytest.mark.asyncio
async def test_c1_wiki_neighbor_out_of_scope_never_reaches_search_graph_or_retrieve(db, monkeypatch):
    """**笔记域**：开态下，范围外的 wiki 邻居不得进 ``search_graph`` 产出，也不得进最终材料。"""
    seed, inside, outside = await _wiki_scenario_notes(db)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()

    hits = await graph.search_graph(db, QUESTION, SCOPE, main_count=0, exclude_ids=set())
    assert [item.note_id for item in hits] == [int(seed.id), int(inside.id)]
    assert outside.id not in [item.note_id for item in hits], "范围外邻居不得进图通道产出"

    from app.retrieval import retrieve

    materials, _text, _warning = await retrieve(db, QUESTION, SCOPE)
    assert outside.id not in [item.note_id for item in materials], "也不得进最终材料"
    assert sorted(item.note_id for item in materials) == sorted([int(seed.id), int(inside.id)])


@pytest.mark.asyncio
async def test_c1_document_domain_wiki_neighbor_respects_the_scope(db, monkeypatch):
    """**文档域**：同一条面再钉一遍（越界文档也满足「过得了三道门、过不了范围」）。"""
    seed, inside, outside = await _wiki_scenario_documents(db)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()

    hits = await graph.search_graph(db, QUESTION, SCOPE_DOC, main_count=0, exclude_ids=set())
    assert [item.filename for item in hits] == [f"{QUESTION}.md", "Inside.md"]
    assert "Outside.md" not in [item.filename for item in hits]

    # 反向对照：这三行**本来就**都在库内、边也都已解析（否则上面那条断言可能只是「没造出来」）
    unscoped = await graph.search_graph(db, QUESTION, None, main_count=0, exclude_ids=set())
    assert "Outside.md" in [item.filename for item in unscoped]
    assert [item.filename for item in unscoped] == [f"{QUESTION}.md", "Inside.md", "Outside.md"]


# ==========================================================================
# C2 —— 三类邻居判定一致：既有过滤保持原样，且不能被邻居绕过
# ==========================================================================


@pytest.mark.asyncio
async def test_c2_seed_and_cooccurrence_filters_are_unchanged(db, monkeypatch):
    """种子与共现边仍是**既有**过滤（只加 wiki 那一道，不重写另两道）。

    - ``allowed_decoy``：身份列含扩展词（共现反查的候选）但**日期在范围外** ⇒ 既不进种子也不进邻居；
    - ``cross_kb``：身份列含扩展词、日期在范围内，但**库在范围外** ⇒ 同样进不来。
    """
    seed, inside, outside = await _wiki_scenario_notes(db)
    await _seed_note(db, title="Allowed Decoy", content="Decoy payload", created_at=_OLD)
    await _seed_note(
        db, title="Allowed Cross", content="Cross payload", created_at=_NEW, kb_id=2
    )
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()

    hits = await graph.search_graph(db, QUESTION, SCOPE, main_count=0, exclude_ids=set())
    assert [item.note_id for item in hits] == [int(seed.id), int(inside.id)]

    # 反向对照：无范围时这两个候选**确实**进得来（证明上面的断言不是「什么都没造出来」）
    graph.reset_counters()
    unscoped = await graph.search_graph(db, QUESTION, None, main_count=0, exclude_ids=set())
    reached = {item.filename for item in unscoped}
    assert {"Allowed Decoy", "Allowed Cross"} <= reached, sorted(reached)


@pytest.mark.asyncio
async def test_c2_source_types_is_not_bypassed_by_the_wiki_hop(db, monkeypatch):
    """``source_types=["note"]`` 时，同库同名的文档**不得**成为种子（matcher 自带来源门）。"""
    await _seed_document(db, filename="Allowed.md", content="Doc payload", created_at=_NEW)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()

    assert await graph.search_graph(db, QUESTION, SCOPE, main_count=0, exclude_ids=set()) == []

    graph.reset_counters()
    unscoped = await graph.search_graph(db, QUESTION, None, main_count=0, exclude_ids=set())
    assert [item.filename for item in unscoped] == ["Allowed.md"], "无范围时它确实是种子"


# ==========================================================================
# C3 —— 无范围正控逐条相同 + 合法 wiki 边正常扩展 + 关态零调用
# ==========================================================================


@pytest.mark.asyncio
async def test_c3_no_scope_control_is_step_for_step_unchanged(db, monkeypatch):
    """``filters=None`` ⇒ 输出逐条相同（顺序 / 分数 / 名额），范围外邻居照样扩。"""
    seed, inside, outside = await _wiki_scenario_notes(db)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()

    hits = await graph.search_graph(db, QUESTION, None, main_count=0, exclude_ids=set())
    assert [item.note_id for item in hits] == [int(seed.id), int(inside.id), int(outside.id)]
    assert [item.graph_score for item in hits] == [1.0, 0.25, 0.25], "合法邻居分不许变"
    assert len(hits) <= FUSE_LIMIT


@pytest.mark.asyncio
async def test_c3_legit_wiki_edge_still_expands_under_a_scope(db, monkeypatch):
    """**C5 的正控**：非空范围下，**范围内**的合法 wiki 一跳必须**仍然**扩进来。

    「``filters`` 非空 ⇒ wiki 邻居一律不扩」这种退化解法会让本用例红。
    """
    seed, inside, _outside = await _wiki_scenario_notes(db)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()

    hits = await graph.search_graph(db, QUESTION, SCOPE, main_count=0, exclude_ids=set())
    assert len(hits) > 1, "范围内的 wiki 一跳必须仍然扩得出来（不许整体关掉）"
    neighbor = next(item for item in hits if item.note_id == int(inside.id))
    assert neighbor.graph_score == 0.25, "降权口径不变（HOP_DECAY × SCORE_FACTOR）"

    from app.retrieval import retrieve

    materials, _text, _warning = await retrieve(db, QUESTION, SCOPE)
    assert int(inside.id) in [item.note_id for item in materials], "最终材料里也要有它"


@pytest.mark.asyncio
async def test_c3_disabled_means_zero_search_graph_calls(db, monkeypatch):
    """关态：``retrieve()`` 连 ``search_graph`` 都不调（A11 / C-c 的原文判据）。"""
    await _wiki_scenario_notes(db)
    calls: list[str] = []
    original = graph.search_graph

    async def _counting(*args, **kwargs):  # noqa: ANN001
        calls.append("called")
        return await original(*args, **kwargs)

    monkeypatch.setattr(graph, "search_graph", _counting)
    from app.retrieval import retrieve

    materials, _text, _warning = await retrieve(db, QUESTION, SCOPE)
    assert materials, "关态关键词路照常命中（这条用例的前提）"
    assert calls == [], "关态不许调用 search_graph"
    assert graph.failure_counts() == {}, "关态一个计数都不许动"

    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()
    await retrieve(db, QUESTION, SCOPE)
    assert calls == ["called"], "反向对照：开态确实会调"


# ==========================================================================
# C4 —— 从真实入口验证接线（M6 调用点变异的检出器）
# ==========================================================================


@pytest.mark.asyncio
async def test_c4_run_hands_the_scope_to_the_neighbor_lookup(db, monkeypatch):
    """``_run`` 必须把本轮 ``filters`` 交给 ``_node_by_key``（回表那一步）。"""
    seed, inside, _outside = await _wiki_scenario_notes(db)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()

    seen: dict = {}
    real = graph._node_by_key

    async def _spy(session, key, *, filters=None):  # noqa: ANN001
        seen[key] = filters
        return await real(session, key, filters=filters)

    monkeypatch.setattr(graph, "_node_by_key", _spy)

    hits = await graph.search_graph(db, QUESTION, SCOPE, main_count=0, exclude_ids=set())

    assert ("note", int(inside.id)) in seen, "范围内的邻居必须真的走到回表那一步"
    assert all(value == SCOPE for value in seen.values()), "每一处回表都要带本轮范围"
    assert [item.note_id for item in hits] == [int(seed.id), int(inside.id)]


# ==========================================================================
# 钉子 —— 时间维度参与判定时，`created_at` 必须**按列读**（实测踩到的坑）
# ==========================================================================


@pytest.mark.asyncio
async def test_c_time_bounded_scope_still_expands_in_scope_neighbors(db, monkeypatch):
    """⚠️ 回归钉（本项实测踩到）：范围里带时间维度时，**不得直接读 ORM 行的 ``created_at``**。

    这条列是 ``server_default=func.now()``，MySQL 方言不支持 ``RETURNING`` ⇒ 同一会话里
    「刚写、没 refresh 过」的行上它是**过期**的；直接读属性会触发属性级懒加载，异步会话里抛
    ``MissingGreenlet``，被 ``search_graph`` 的静默降级吃掉 ⇒ 症状是「图通道整条不出材料」。

    ``tests/retrieval/conftest.py::seed_document`` 正是**不 refresh** 的写法 ⇒ 拿它造目标行
    才能钉住这条路：**范围内邻居必须照样被扩进来**（越界那条仍要被拒）。
    """
    from tests.retrieval.conftest import seed_document

    seed = await seed_document(
        db, [f"{QUESTION} text"], filename=f"{QUESTION}.md", start_ordinal=0
    )
    inside = await seed_document(db, ["Inside payload"], filename="Inside.md", start_ordinal=0)
    outside = await _seed_document(
        db, filename="Outside.md", content="Outside payload", created_at=_OLD
    )
    await _seed_edge(
        db, kind="document", source_id=seed.id, target_id=inside.id, target_title=inside.filename
    )
    await _seed_edge(
        db, kind="document", source_id=seed.id, target_id=outside.id, target_title=outside.filename
    )
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()

    hits = await graph.search_graph(db, QUESTION, SCOPE_DOC, main_count=0, exclude_ids=set())

    assert [item.filename for item in hits] == [f"{QUESTION}.md", "Inside.md"], (
        "时间维度参与判定时，范围内邻居仍须扩出来（读 created_at 炸了会整条通道降级）"
    )


# ==========================================================================
# C-d / §0.2 —— 静默降级不许被吞、不许新增计数器名
# ==========================================================================


@pytest.mark.asyncio
async def test_c_silent_degrade_still_catches_the_wiki_failure(db, monkeypatch):
    """``wiki_links`` 查询失败仍是**静默降级**，且异常继续上抛到 ``search_graph`` 的统一处理器。

    （在 ``_run`` 的 wiki 循环里包一层吞异常的 ``try/except`` 会打掉这条路径 ⇒ 本用例是它的钉子。）
    """
    await _wiki_scenario_notes(db)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()

    async def _boom(*args, **kwargs):  # noqa: ANN001
        raise RuntimeError("wiki_links 查询炸了")

    monkeypatch.setattr(graph, "load_wiki_adjacency", _boom)

    assert await graph.search_graph(db, QUESTION, SCOPE, main_count=0, exclude_ids=set()) == []
    assert graph.failure_counts()["failures"] == 1, "异常必须上抛到 search_graph 的处理器"


@pytest.mark.asyncio
async def test_c_no_new_counter_names_are_introduced(db, monkeypatch):
    """§0.2①：计数器名集合不许变（多出来的键会透传进 ``/api/agent/audit`` 的 ``counters.graph``）。"""
    await _wiki_scenario_notes(db)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()

    await graph.search_graph(db, QUESTION, SCOPE, main_count=0, exclude_ids=set())
    assert set(graph.failure_counts()) <= _KNOWN_COUNTERS, "不许新增计数器名（要观测就用既有 8 键 + 日志）"


def test_c_module_has_no_network_client():
    """§0.2 同族：``graph.py`` 源码里不得出现任何网络客户端（改前已有，改后不许破）。"""
    from pathlib import Path

    source = Path(graph.__file__).read_text(encoding="utf-8")
    for name in ("httpx", "requests", "aiohttp", "urllib", "socket", "openai"):
        assert name not in source, f"图通道模块不许引用 {name}"


def test_c_link_module_is_isolated_in_this_file():
    """本文件的隔离是**开关级**的：尾部一跳扩展在这里始终关着（越界材料只能来自图通道）。"""
    assert settings.RAG_LINK_EXPAND is False


@pytest.mark.asyncio
async def test_c_soft_deleted_neighbor_is_still_blocked_by_the_existing_gate(db, monkeypatch):
    """三道门（未软删 / ready / 别名非空）**保持原样**：范围复核只加不减。"""
    from datetime import datetime as dt

    seed = await _seed_note(db, title=QUESTION, content=f"{QUESTION} text", created_at=_NEW)
    gone = Note(
        title="Inside Note",
        content="Inside payload",
        kb_id=1,
        created_at=_NEW,
        deleted_at=dt(2026, 9, 21, 0, 0, 0),
    )
    db.add(gone)
    await db.commit()
    await db.refresh(gone)
    await _seed_edge(db, kind="note", source_id=seed.id, target_id=gone.id, target_title=gone.title)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()

    hits = await graph.search_graph(db, QUESTION, SCOPE, main_count=0, exclude_ids=set())
    assert [item.note_id for item in hits] == [int(seed.id)], "软删邻居仍进不来"
