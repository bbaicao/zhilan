"""图谱检索通道用例（阶段 5 · 条目 5.3 · 子条目 `5.3.1` / `5.3.3`）。

覆盖开工包 §5 的**通道面**断言：

| 用例 | 断言 |
|---|---|
| ``test_a2_*`` | **A2**（6 个键进 config；`GRAPHRAG_ENABLE` 有真实读取点；其余键**不是死键**） |
| ``test_a3_*`` | **A3**（关 = 零变化：零 `search_graph` 调用 + 材料与两路逐条一致） |
| ``test_a4_*`` | **A4**（开 = 真多回一条：图通道拿回主命中拿不到的块，且 `graph_score` 非空） |
| ``test_a5_*`` | **A5**（三条故障注入都照常走完整链路，不炸穿） |
| ``test_a8_*`` | **A8**（六类反例：弱别名 / 强别名 / 大小写空白 / 跨库 / 软删 / 非 ready） |
| ``test_a9_*`` | **A9**（名额、总数 ≤ FUSE_LIMIT、与主命中及 `link.py` 同 `chunk_id` 只出一次） |
| ``test_a11_*`` | **A11**（关态：零 `search_graph` 调用、零新增 SQL） |
| ``test_a12_*`` | **A12**（与 5.2 缓存一致：材料全字段 round-trip / 存活校验同样生效 / 指纹含 `graph` 子对象） |
| ``test_a13_*`` | **A13**（模块零出网：源码里没有任何 HTTP 客户端引用） |
| ``test_pure_*`` | 纯函数层：别名归一化、token 提取、种子强度、名额公式、默认值钉子 |

**本文件不连真服务**：向量路被钉成假件（与 `tests/eval/harness.py` 同款），重排关掉，
开关默认关——`retrieve()` 走的是「关键词路 + 零向量 + 零重排」这条可复现路径。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import pytest_asyncio

from app.core.config import settings
from app.models import Note, WikiLink
from app.retrieval import graph
from app.retrieval.types import FUSE_LIMIT, Material
from tests.retrieval.conftest import seed_document


class _EmptyStore:
    """空结果假向量库（离线没有 embedding 配额，向量路显式不可用）。"""

    def query(self, vector, limit: int = 5, filters=None):  # noqa: ANN001
        return []


@pytest.fixture(autouse=True)
def _pin_defaults(monkeypatch):
    """钉住默认态 + 隔离出网点（**本文件所有用例都适用**）。

    - ``GRAPHRAG_ENABLE=False``：本机 ``.env`` 一旦写上就整片随机变红（口径同
      ``tests/api/conftest.py::pin_graphrag_off``）；
    - ``RAG_ENABLE_RERANK=False``：本机 ``.env`` 有老仓库遗留的 ``true``（见 3.1 证据），
      不钉住每次检索都会多打一条「未绑定服务 → 回落」的警告；
    - 向量路两个薄包装换成假件：不钉住会**真打上游 embedding** 并连本机 Qdrant。
    """
    from app.retrieval import vector as vector_mod
    from app.vector.embed import FakeEmbedding

    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", False)
    store = _EmptyStore()
    monkeypatch.setattr(vector_mod, "get_vector_store", lambda **kwargs: store)
    monkeypatch.setattr(
        vector_mod, "get_embedding_client", lambda *, fake=False: FakeEmbedding(dim=16)
    )
    graph.reset_counters()
    yield
    graph.reset_counters()


@pytest_asyncio.fixture
async def graph_on(monkeypatch):
    """开态助手：开开关 + 清计数（**开态用例自己负责清**，见 `pin_graphrag_off`）。"""
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()
    return graph


async def _seed_link(db, source, target, *, resolved: bool = True, kb_id: int = 1):
    """造一条 ``wiki_links`` 边（``documents`` 之间的互链）。

    ``resolved`` 默认 True：未解析的边在检索侧**不产生片段**（目标不存在），只作反例。
    """
    db.add(
        WikiLink(
            source_type="document",
            source_id=int(source.id),
            kb_id=int(kb_id),
            target_title=(target.filename or "").strip(),
            target_document_id=int(target.id),
            resolved=bool(resolved),
        )
    )
    await db.commit()


async def _seed_note(db, *, title: str, content: str, kb_id: int = 1, tags=None, category: str = ""):
    note = Note(title=title, content=content, kb_id=int(kb_id), tags=tags, category=category)
    db.add(note)
    await db.commit()
    return note


async def _chunk_id_of(db, document, ordinal: int = 1) -> int:
    from sqlalchemy import select

    from app.models import Chunk

    row = (
        await db.execute(
            select(Chunk.id).where(Chunk.document_id == int(document.id), Chunk.ordinal == ordinal)
        )
    ).scalar_one()
    return int(row)


async def _sql_counter(db, monkeypatch):
    """把 ``session.execute`` 包一层计数（A11 的「零新增 SQL」靠它）。"""
    calls: list[str] = []
    original = db.execute

    async def _wrapped(statement, *args, **kwargs):
        calls.append(str(statement)[:60])
        return await original(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", _wrapped)
    return calls


# --------------------------------------------------------------------------
# A2 —— 配置键与读取点
# --------------------------------------------------------------------------


def test_a2_config_keys_exist_and_module_defaults_match():
    """6 个键都在 ``config.py`` 里，且**模块兜底默认值与键默认值逐个相同**（漂移即红）。

    本机 `.env` 无任何 `GRAPHRAG_*`（开工包 §0.2 的实测结论）⇒ 这里读到的就是键默认值。
    """
    assert settings.GRAPHRAG_ENABLE is False
    assert settings.GRAPHRAG_TOP_K == graph.DEFAULT_TOP_K == 6
    assert settings.GRAPHRAG_SCORE_FACTOR == graph.DEFAULT_SCORE_FACTOR == 0.5
    assert settings.GRAPHRAG_HOP_DECAY == graph.DEFAULT_HOP_DECAY == 0.5
    assert settings.GRAPHRAG_WEAK_ALIAS_WEIGHT == graph.DEFAULT_WEAK_ALIAS_WEIGHT == 0.5
    assert settings.GRAPHRAG_MAX_TERMS == graph.DEFAULT_MAX_TERMS == 8


def test_a2_switch_has_a_real_read_point(monkeypatch):
    """``enabled()`` 是唯一读取点，且**真的读**（读的是 ``settings.GRAPHRAG_ENABLE``）。"""
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    assert graph.enabled() is True
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)
    assert graph.enabled() is False


def test_a2_other_knobs_are_not_dead_keys(monkeypatch):
    """5 个旋钮**各自改变一个可观测结果**（`REDIS_ENABLE` 那种死键的对照组）。"""
    # TOP_K：名额
    monkeypatch.setattr(settings, "GRAPHRAG_TOP_K", 2)
    assert graph.graph_budget(0) == 2
    monkeypatch.setattr(settings, "GRAPHRAG_TOP_K", 6)
    assert graph.graph_budget(0) == 6
    assert graph.graph_budget(9) == min(6, FUSE_LIMIT - 9)

    # WEAK_ALIAS_WEIGHT：弱别名种子强度
    node = graph._Node(
        kind="document",
        source_id=1,
        kb_id=1,
        title="周报.md",
        category="工作日志",
        strong=["周报"],
        weak=["工作日志"],
    )
    monkeypatch.setattr(settings, "GRAPHRAG_WEAK_ALIAS_WEIGHT", 0.5)
    assert graph._seed_weight(node, ["工作日志"]) == pytest.approx(0.5)
    monkeypatch.setattr(settings, "GRAPHRAG_WEAK_ALIAS_WEIGHT", 0.25)
    assert graph._seed_weight(node, ["工作日志"]) == pytest.approx(0.25)

    # MAX_TERMS：token 上限
    monkeypatch.setattr(settings, "GRAPHRAG_MAX_TERMS", 2)
    assert len(graph._query_tokens("双路召回与融合排序方法")) == 2
    monkeypatch.setattr(settings, "GRAPHRAG_MAX_TERMS", 8)
    assert len(graph._query_tokens("双路召回与融合排序方法")) > 2

    # SCORE_FACTOR / HOP_DECAY：邻居分（用纯函数直接算，不经数据库）
    monkeypatch.setattr(settings, "GRAPHRAG_SCORE_FACTOR", 0.5)
    monkeypatch.setattr(settings, "GRAPHRAG_HOP_DECAY", 0.5)
    assert graph._hop_decay() * graph._score_factor() == pytest.approx(0.25)
    monkeypatch.setattr(settings, "GRAPHRAG_HOP_DECAY", 0.2)
    assert graph._hop_decay() * graph._score_factor() == pytest.approx(0.1)


# --------------------------------------------------------------------------
# 纯函数层
# --------------------------------------------------------------------------


def test_pure_normalize_alias_folds_whitespace_case_and_extension():
    assert graph.normalize_alias("  检索 与 融合.MD ") == "检索 与 融合"
    assert graph.normalize_alias("Report.DOCX") == "report"
    assert graph.normalize_alias("Straße.md") == graph.normalize_alias("STRASSE.md")
    # 只剥「点 + 字母开头」的尾巴：版本号不会被误剥
    assert graph.normalize_alias("规划 v1.2") == "规划 v1.2"


def test_pure_query_tokens_drops_single_chars_and_dedupes():
    tokens = graph._query_tokens("向量库使用 Qdrant")
    assert all(len(token) >= 2 for token in tokens)
    assert len(tokens) == len(set(tokens))
    assert "qdrant" in tokens
    assert "向量" in tokens


def test_pure_alias_hit_is_forward_containment_only():
    """``token ⊆ alias`` 算命中；``alias ⊆ token`` **刻意不算**（见模块 docstring）。"""
    assert graph._alias_hit("检索与融合", ["检索"]) is True
    assert graph._alias_hit("检索与融合", ["检索与融合"]) is True
    assert graph._alias_hit("数据库", ["数据库系统的选型"]) is False
    assert graph._alias_hit("数据库", ["库"]) is False  # 单字 token 不参与


def test_pure_seed_weight_takes_max_not_sum():
    """同一节点被多个别名命中取**最大**（强别名压过弱别名，不累加）。"""
    node = graph._Node(
        kind="document",
        source_id=1,
        kb_id=1,
        title="周报.md",
        category="工作日志",
        strong=["周报"],
        weak=["工作日志"],
    )
    assert graph._seed_weight(node, ["周报", "工作日志"]) == pytest.approx(1.0)
    assert graph._seed_weight(node, ["工作日志"]) == pytest.approx(
        graph.DEFAULT_WEAK_ALIAS_WEIGHT
    )
    assert graph._seed_weight(node, ["不相关"]) is None


def test_pure_budget_never_exceeds_room_left_by_main_hits():
    """名额 = ``min(TOP_K, FUSE_LIMIT − 主命中)``；主命中满额时**一条也不加**。"""
    assert graph.graph_budget(0) == graph.DEFAULT_TOP_K
    assert graph.graph_budget(FUSE_LIMIT) == 0
    assert graph.graph_budget(FUSE_LIMIT + 5) == 0
    assert graph.graph_budget(-3) == graph.DEFAULT_TOP_K


def test_the_channel_can_never_outrank_a_main_hit():
    """**「主命中永远优先」的结构性保证**（不是调参碰巧对）。

    图通道最大的 RRF 贡献 ``系数/(RRF_K+1)`` 必须**严格小于**主命中最小的 RRF 贡献
    ``1/(RRF_K + max(KEYWORD_TOP_K, VECTOR_TOP_K))``。这条不变式一破，
    `tests/eval` 的 MRR 就会掉（实测过：0.9833 → 0.8833）。
    """
    from app.retrieval.types import KEYWORD_TOP_K, RRF_K, VECTOR_TOP_K

    best_graph = graph._channel_weight() / (RRF_K + 1)
    worst_main = 1.0 / (RRF_K + max(KEYWORD_TOP_K, VECTOR_TOP_K))
    assert best_graph < worst_main, (
        f"图通道权重过大（{best_graph:.5f} ≥ {worst_main:.5f}）："
        "图通道材料可能排到主命中之前，A14「不劣化」不再有结构保证"
    )
    assert graph._channel_weight() == graph.DEFAULT_SCORE_FACTOR


def test_graph_weight_default_keeps_two_channel_behaviour():
    """``graph_weight`` 缺省 1.0：不传图通道的老调用点行为逐字不变。"""
    from app.retrieval.fuse import fuse as fuse_fn
    from app.retrieval.types import RRF_K as K

    vector_hits = [Material(chunk_id=3, document_id=3, content="c")]
    keyword_hits = [Material(chunk_id=3, document_id=3, content="c")]
    assert fuse_fn(vector_hits, keyword_hits)[0].score == pytest.approx(2 / (K + 1))
    graph = [Material(chunk_id=9, document_id=9, content="g", graph_score=1.0)]
    fused = fuse_fn([], [], graph_hits=graph, graph_weight=0.5)
    assert fused[0].score == pytest.approx(0.5 / (K + 1))


# --------------------------------------------------------------------------
# A8 —— 六类反例（参数化）
# --------------------------------------------------------------------------



@pytest.mark.asyncio
async def test_a8_strong_alias_seed(db, graph_on):
    """强别名（文件名去扩展名）命中 ⇒ 出种子材料。"""
    await seed_document(db, ["正文与标题无关。"], filename="系统概览.md")
    hits = await graph.search_graph(db, "概览", main_count=0)
    assert [item.filename for item in hits] == ["系统概览.md"]
    assert hits[0].graph_score == pytest.approx(1.0)



@pytest.mark.asyncio
async def test_a8_weak_alias_seed(db, graph_on):
    """弱别名（``category`` / ``tags``）命中 ⇒ 出种子材料，且强度是弱权重。"""
    await seed_document(db, ["正文与分类无关。"], filename="甲.md", category="工作日志")
    hits = await graph.search_graph(db, "工作日志", main_count=0)
    assert [item.filename for item in hits] == ["甲.md"]
    assert hits[0].graph_score == pytest.approx(graph.DEFAULT_WEAK_ALIAS_WEIGHT)



@pytest.mark.asyncio
async def test_a8_alias_case_and_whitespace_insensitive(db, graph_on):
    """别名大小写与空白差异**不得**影响命中。"""
    await seed_document(db, ["正文无关。"], filename="Report  Final.MD")
    hits = await graph.search_graph(db, "report final", main_count=0)
    assert [item.filename for item in hits] == ["Report  Final.MD"]



@pytest.mark.asyncio
async def test_a8_cross_kb_does_not_match(db, graph_on):
    """跨 ``kb_id`` **不得**匹配（``link.py:63`` 的原文教训：张冠李戴的邻边）。"""
    await seed_document(db, ["正文无关。"], filename="系统概览.md", kb_id=2)
    assert await graph.search_graph(db, "概览", None, main_count=0) != []  # 不限范围 ⇒ 命中
    assert await graph.search_graph(db, "概览", {"kb_ids": [1]}, main_count=0) == []



@pytest.mark.asyncio
async def test_a8_soft_deleted_document_does_not_match(db, graph_on):
    """软删（回收站）的文档**不得**被图通道后门带进来。"""
    await seed_document(db, ["正文无关。"], filename="系统概览.md", deleted=True)
    assert await graph.search_graph(db, "概览", main_count=0) == []



@pytest.mark.asyncio
async def test_a8_not_ready_document_does_not_match(db, graph_on):
    """``index_status != ready`` 的文档**不得**被带进来（与关键词路同一道门禁）。"""
    await seed_document(db, ["正文无关。"], filename="系统概览.md", index_status="processing")
    assert await graph.search_graph(db, "概览", main_count=0) == []



@pytest.mark.asyncio
async def test_a8_unresolved_wiki_edge_makes_no_neighbor(db, graph_on):
    """未解析的 ``wiki_links`` 边不产生邻居（目标不存在）。"""
    a = await seed_document(db, ["系统概览 讲了这个模块。"], filename="系统概览.md")
    b = await seed_document(db, ["倒排表与近邻图都存在本地。"], filename="向量索引.md")
    await _seed_link(db, a, b, resolved=False)
    hits = await graph.search_graph(db, "系统概览", main_count=0)
    assert [item.filename for item in hits] == ["系统概览.md"]


# --------------------------------------------------------------------------
# A4 / A10（通道面）——开态真的多拿一条
# --------------------------------------------------------------------------



@pytest.mark.asyncio
async def test_a4_graph_brings_a_chunk_the_main_hits_miss(db):
    """A4：**图通道拿回主命中拿不到的块**，且那条材料的 ``graph_score`` 非空。

    语料：A 与 B 互链；问题只命中 **A 的身份词**（A 的块正文含它，B 的块正文**不含**）
    ⇒ 关态只有 A 的块，开态多出 B 的块（经由一跳）。
    """
    a = await seed_document(db, ["系统概览 讲的是整体架构。"], filename="系统概览.md")
    b = await seed_document(db, ["倒排表与近邻图都存在本地。"], filename="向量索引.md")
    await _seed_link(db, a, b)

    from app.retrieval import retrieve

    off, _text, _warning = await retrieve(db, "系统概览")
    assert [item.filename for item in off] == ["系统概览.md"]

    settings.GRAPHRAG_ENABLE = True
    graph.reset_counters()
    try:
        on, _text, _warning = await retrieve(db, "系统概览")
    finally:
        settings.GRAPHRAG_ENABLE = False
        graph.reset_counters()

    assert [item.filename for item in on] == ["系统概览.md", "向量索引.md"]
    brought = [item for item in on if item.filename == "向量索引.md"]
    assert brought and brought[0].graph_score is not None
    # 编号连续可用（`[n]` 可渲染）：由 `retrieve()` 统一编号，图通道只是多贡献了一条材料
    assert [item.ordinal for item in on] == [1, 2]
    assert brought[0].chunk_id == await _chunk_id_of(db, b)


# --------------------------------------------------------------------------
# A5 —— 三条故障注入都不炸穿
# --------------------------------------------------------------------------



@pytest.mark.asyncio
async def test_a5_alias_sql_failure_degrades_silently(db, graph_on, monkeypatch):
    await seed_document(db, ["检索手册 记录了向量库的选型。"], filename="检索手册.md")

    async def _boom(*args, **kwargs):
        raise RuntimeError("别名 SQL 炸了")

    monkeypatch.setattr(graph, "_load_document_nodes", _boom)
    from app.retrieval import retrieve

    materials, text, _warning = await retrieve(db, "检索手册")
    assert materials and "[1]" in text
    assert graph.failure_counts()["failures"] == 1



@pytest.mark.asyncio
async def test_a5_material_construction_failure_degrades_silently(db, graph_on, monkeypatch):
    await seed_document(db, ["正文与标题无关。"], filename="系统概览.md")

    async def _boom(*args, **kwargs):
        raise RuntimeError("材料构造炸了")

    monkeypatch.setattr(graph, "_node_materials", _boom)
    from app.retrieval import retrieve

    materials, text, _warning = await retrieve(db, "概览")
    assert materials == []  # 关键词路本来就零命中；重点是**没有抛穿**
    assert text == ""
    assert graph.failure_counts()["failures"] == 1



@pytest.mark.asyncio
async def test_a5_wiki_links_failure_degrades_silently(db, graph_on, monkeypatch):
    a = await seed_document(db, ["系统概览 讲的是整体架构。"], filename="系统概览.md")
    b = await seed_document(db, ["倒排表与近邻图都存在本地。"], filename="向量索引.md")
    await _seed_link(db, a, b)

    async def _boom(*args, **kwargs):
        raise RuntimeError("wiki_links 查询炸了")

    monkeypatch.setattr(graph, "load_wiki_adjacency", _boom)
    from app.retrieval import retrieve

    materials, text, _warning = await retrieve(db, "系统概览")
    assert [item.filename for item in materials] == ["系统概览.md"]
    assert "[1]" in text
    assert graph.failure_counts()["failures"] == 1


# --------------------------------------------------------------------------
# A9 —— 名额与去重
# --------------------------------------------------------------------------



@pytest.mark.asyncio
async def test_a9_top_k_caps_the_channel(db, graph_on, monkeypatch):
    for index in range(1, 8):
        await seed_document(db, [f"第{index}份正文。"], filename=f"系统概览{index}期.md")
    hits = await graph.search_graph(db, "系统概览", main_count=0)
    assert len(hits) == settings.GRAPHRAG_TOP_K == 6



@pytest.mark.asyncio
async def test_a9_budget_shrinks_with_main_hits(db, graph_on):
    for index in range(1, 8):
        await seed_document(db, [f"第{index}份正文。"], filename=f"系统概览{index}期.md")
    assert len(await graph.search_graph(db, "系统概览", main_count=0)) == 6
    assert len(await graph.search_graph(db, "系统概览", main_count=9)) == 3
    assert await graph.search_graph(db, "系统概览", main_count=FUSE_LIMIT) == []



@pytest.mark.asyncio
async def test_a9_main_hit_chunks_are_never_duplicated(db, graph_on):
    document = await seed_document(
        db, ["系统概览 讲的是整体架构。", "第二块内容。"], filename="系统概览.md"
    )
    chunk_one = await _chunk_id_of(db, document, 1)
    hits = await graph.search_graph(db, "系统概览", main_count=1, exclude_ids={chunk_one})
    assert chunk_one not in {item.chunk_id for item in hits}
    assert [item.block_ordinal for item in hits] == [2]



@pytest.mark.asyncio
async def test_a9_total_materials_never_exceed_fuse_limit_with_link_expand(db, graph_on):
    """图通道 + 既有 ``link.py`` 同时开：总数仍 ≤ ``FUSE_LIMIT``、``chunk_id`` 不重复（I3）。"""
    a = await seed_document(db, ["系统概览 讲的是整体架构。"], filename="系统概览.md")
    b = await seed_document(db, ["倒排表与近邻图都存在本地。"], filename="向量索引.md")
    c = await seed_document(db, ["许可证里写的是宽松条款。"], filename="许可证.md")
    await _seed_link(db, a, b)
    await _seed_link(db, a, c)
    from app.retrieval import retrieve

    materials, _text, _warning = await retrieve(db, "系统概览")
    ids = [item.chunk_id for item in materials]
    assert len(materials) <= FUSE_LIMIT
    assert len(ids) == len(set(ids)), "同一 chunk_id 只能出一条（I3 的地基）"


# --------------------------------------------------------------------------
# A11 / A3 —— 关态零变化
# --------------------------------------------------------------------------



@pytest.mark.asyncio
async def test_a11_disabled_means_zero_calls_and_zero_new_sql(db, monkeypatch):
    """A11：关 ⇒ **零 `search_graph` 调用**、零新增 SQL。"""
    await seed_document(db, ["系统概览 讲的是整体架构。"], filename="系统概览.md")
    calls: list[str] = []
    original = graph.search_graph

    async def _counting(*args, **kwargs):
        calls.append("called")
        return await original(*args, **kwargs)

    monkeypatch.setattr(graph, "search_graph", _counting)
    sql = await _sql_counter(db, monkeypatch)
    from app.retrieval import retrieve

    materials, _text, _warning = await retrieve(db, "系统概览")
    baseline_sql = len(sql)
    assert materials, "关态关键词路照常命中（这条用例的前提）"
    assert calls == [], "关态不许调用 search_graph（A11 的原文判据）"

    # 反证：开态**确实**会多查（否则上面那条断言可能只是「计数没生效」）
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()
    try:
        await retrieve(db, "系统概览")
    finally:
        monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)
        graph.reset_counters()
    assert calls == ["called"]
    assert len(sql) > baseline_sql



@pytest.mark.asyncio
async def test_a3_disabled_materials_match_the_two_channel_path(db, monkeypatch):
    """A3：关态材料与「直接走两路 fuse」逐条一致（图通道一条都不掺）。"""
    from app.retrieval import keyword
    from app.retrieval.fuse import fuse as fuse_fn

    await seed_document(db, ["系统概览 讲的是整体架构。"], filename="系统概览.md")
    await seed_document(db, ["倒排表与近邻图都存在本地。"], filename="向量索引.md")
    from app.retrieval import retrieve

    materials, _text, _warning = await retrieve(db, "系统概览")
    keyword_hits = await keyword.search_keywords(db, "系统概览", None)
    expected = fuse_fn([], keyword_hits)
    assert [item.chunk_id for item in materials] == [item.chunk_id for item in expected]
    assert all(item.graph_score is None for item in materials)


# --------------------------------------------------------------------------
# A12 —— 与 5.2 缓存一致
# --------------------------------------------------------------------------



@pytest.mark.asyncio
async def test_a12_graph_materials_round_trip_through_cache_serialization(db, graph_on):
    """① 图通道的材料**进得了缓存条目**：全字段 round-trip（含新字段 `graph_score`）。"""
    from app.cache.entry import deserialize_materials, serialize_materials

    await seed_document(db, ["正文与标题无关。"], filename="系统概览.md")
    hits = await graph.search_graph(db, "概览", main_count=0)
    assert hits and hits[0].graph_score is not None

    rows = serialize_materials(hits)
    assert rows[0]["graph_score"] == pytest.approx(hits[0].graph_score)
    rebuilt = deserialize_materials(rows)
    assert rebuilt == hits, "图通道材料没有全字段 round-trip"



@pytest.mark.asyncio
async def test_a12_liveness_check_applies_to_graph_materials(db):
    """② 命中前的**存活校验**对图通道材料同样生效（改一个块的内容 ⇒ 不命中）。"""
    from sqlalchemy import select

    from app.cache.entry import content_fingerprint, fingerprints_live
    from app.models import Chunk

    document = await seed_document(db, ["系统概览 讲的是整体架构。"], filename="系统概览.md")
    chunk_id = await _chunk_id_of(db, document)
    fingerprints = {str(chunk_id): content_fingerprint("系统概览 讲的是整体架构。")}
    assert await fingerprints_live(db, fingerprints) is True

    row = (await db.execute(select(Chunk).where(Chunk.id == chunk_id))).scalar_one()
    row.content = "系统概览 讲的是整体架构。（已重写）"
    await db.commit()
    assert await fingerprints_live(db, fingerprints) is False


def test_a12_retrieval_fingerprint_carries_the_graph_subobject(monkeypatch):
    """③ 指纹含 ``graph`` 子对象；**改了任一 `GRAPHRAG_*` ⇒ 换键**。"""
    from app.cache import entry as cache_entry

    knobs = (
        ("GRAPHRAG_ENABLE", True),
        ("GRAPHRAG_TOP_K", 3),
        ("GRAPHRAG_SCORE_FACTOR", 0.9),
        ("GRAPHRAG_HOP_DECAY", 0.1),
        ("GRAPHRAG_WEAK_ALIAS_WEIGHT", 0.7),
        ("GRAPHRAG_MAX_TERMS", 4),
    )
    original = {name: getattr(settings, name) for name, _value in knobs}
    try:
        before = cache_entry.retrieval_fingerprint()
        assert "graph" in before
        assert set(before["graph"]) == {
            "enabled",
            "top_k",
            "score_factor",
            "hop_decay",
            "weak_alias_weight",
            "max_terms",
        }
        for name, value in knobs:
            setattr(settings, name, value)
            changed = cache_entry.retrieval_fingerprint()
            assert changed != before, f"{name} 没有进指纹（改了它仍会命中旧答案）"
            assert changed["graph"] != before["graph"]
            setattr(settings, name, original[name])
            assert cache_entry.retrieval_fingerprint() == before
    finally:
        for name, value in original.items():
            setattr(settings, name, value)


def test_a12_missing_knob_falls_back_to_default(monkeypatch):
    """旋钮缺失（``None``）时指纹仍算得出来（不抛 → 不做假命中）。"""
    from app.cache import entry as cache_entry

    monkeypatch.setattr(settings, "GRAPHRAG_TOP_K", None, raising=False)
    assert cache_entry.retrieval_fingerprint()["graph"]["top_k"] == 6


# --------------------------------------------------------------------------
# A13 —— 零出网
# --------------------------------------------------------------------------


def test_a13_graph_module_has_no_network_client():
    """图通道全链路零出网：模块源码里没有任何 HTTP / socket 客户端引用。"""
    source = Path(graph.__file__).read_text(encoding="utf-8")
    for needle in ("httpx", "requests", "aiohttp", "urllib", "socket", "openai"):
        assert needle not in source, f"图通道模块引用了出网组件：{needle}"
    assert not re.search(r"^\s*(import|from)\s+\S*(httpx|requests|socket)", source, re.M)


# --------------------------------------------------------------------------
# 复现性（同一批输入两次调用逐条相同）
# --------------------------------------------------------------------------



@pytest.mark.asyncio
async def test_search_graph_is_deterministic(db, graph_on):
    for index in range(1, 5):
        await seed_document(db, [f"第{index}份正文。"], filename=f"系统概览{index}期.md")
    first = [item.chunk_id for item in await graph.search_graph(db, "系统概览", main_count=0)]
    second = [item.chunk_id for item in await graph.search_graph(db, "系统概览", main_count=0)]
    assert first == second and len(first) == 4


def test_material_graph_score_default_is_none():
    """新字段默认 ``None``——既有 30 处 ``Material(...)`` 构造点全走关键字，故加了也不动它们。"""
    assert Material(chunk_id=1, document_id=1, content="x").graph_score is None
