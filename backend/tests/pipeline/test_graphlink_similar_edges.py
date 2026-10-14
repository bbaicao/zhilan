"""「导入资料自动连线」后端定向判据：代表向量 / 边判定 / 水印原子替换 / 关态零影响 / 生命周期。

覆盖范围（对应设计说明 4.2、4.3 与 P2 生命周期）：
- 纯函数：等距抽样、均值+L2 归一、top-K 并集 + 阈值 + 度上限的**确定性**剪枝；
- 落库：开态按库写出、跨模型空间不对拍、`GET /api/graph` 出现第三类边 `similar`；
- 水印：计算期间有人发布新文档 ⇒ 本次不提交（旧边逐字保留）；
- 关态：`GRAPHLINK_ENABLE` 假时新表**一个 SELECT 都不发**、旧行不被删、图形无 similar 边；
- 生命周期：硬删文档清两端边（kb 合并改向见 `tests/test_graphlink_kb_merge_rewire.py`）。

替身说明：连线链只依赖 `for_space` / `fetch_vectors` 两个方法，所以这里的 `_FakeStore`
刻意**不继承** `VectorStore`（不实现任何协议细节），用来钉「读向量走的是窄接口」。
"""

from __future__ import annotations

import math

import pytest
from sqlalchemy import event, select

from app.core import derived_counters
from app.core.config import settings
from app.models import Document, DocumentSimilarityEdge, IndexBuild
from app.pipeline import graphlink, knowledge_graph
from app.vector.base import VectorPoint, VectorStore
from app.vector.qdrant_store import QdrantVectorStore


#: 本地 Qdrant 只接受 UUID 形式的点 id（`_point_id` 归一后仍原样透传非数字串）。
_P1 = "11111111-1111-1111-1111-111111111111"
_P2 = "22222222-2222-2222-2222-222222222222"
_P3 = "33333333-3333-3333-3333-333333333333"


class _FakeStore:
    """只认点 id 的假向量库：够连线链用，且能证明它没读别的。"""

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.vectors = dict(vectors or {})

    def for_space(self, space_id: str) -> "_FakeStore":
        self.space_id = space_id
        return self

    def fetch_vectors(self, ids: list[str]) -> dict[str, list[float]]:
        return {key: list(self.vectors[key]) for key in ids if key in self.vectors}


def _enable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "GRAPHLINK_ENABLE", True, raising=False)
    monkeypatch.setattr(settings, "GRAPHLINK_TOP_K", 3, raising=False)
    monkeypatch.setattr(settings, "GRAPHLINK_MIN_SCORE", 0.65, raising=False)
    monkeypatch.setattr(settings, "GRAPHLINK_MAX_DEGREE", 3, raising=False)
    monkeypatch.setattr(settings, "GRAPHLINK_MAX_CHUNKS_PER_DOC", 64, raising=False)
    monkeypatch.setattr(settings, "GRAPHLINK_MAX_DOCS_PER_KB", 2000, raising=False)


async def _seed_doc(
    db, filename: str, *, kb_id: int = 1, space: str = "sp-a",
    vectors: tuple[list[float], ...] = ([1.0, 0.0],),
) -> tuple[Document, dict[str, list[float]]]:
    """造一篇「已发布」文档：Document + IndexBuild(space_id/point_ids)，返回 (文档, 点→向量)。"""
    doc = Document(
        filename=filename, file_type="txt", status="ready", index_status="ready", kb_id=kb_id,
    )
    db.add(doc)
    await db.flush()
    point_ids = [f"{filename}#{index}" for index in range(len(vectors))]
    build = IndexBuild(
        id=f"build-{filename}",
        document_id=doc.id,
        state="published",
        space_id=space,
        point_ids=point_ids,
        fingerprint={},
        vectorized=True,
    )
    db.add(build)
    await db.flush()
    doc.active_build_id = build.id
    doc.content_build_id = build.id
    await db.commit()
    return doc, {key: list(value) for key, value in zip(point_ids, vectors)}


async def _edge_rows(db) -> list[DocumentSimilarityEdge]:
    result = await db.execute(
        select(DocumentSimilarityEdge).order_by(
            DocumentSimilarityEdge.source_document_id, DocumentSimilarityEdge.target_document_id,
        )
    )
    return list(result.scalars().all())


def test_sample_indices_is_even_sorted_and_bounded() -> None:
    assert graphlink.sample_indices(10, 4) == [0, 2, 5, 7]
    assert graphlink.sample_indices(3, 64) == [0, 1, 2]
    assert graphlink.sample_indices(100, 64)[:2] == [0, 1]
    assert len(graphlink.sample_indices(100, 64)) == 64
    assert graphlink.sample_indices(100, 64) == graphlink.sample_indices(100, 64)
    assert graphlink.sample_indices(0, 5) == []
    assert graphlink.sample_indices(5, 0) == []


def test_mean_normalized_is_unit_and_refuses_unusable_input() -> None:
    got = graphlink.mean_normalized([[1.0, 0.0], [0.0, 1.0]])
    assert got == pytest.approx([0.7071067811865475, 0.7071067811865475])
    assert sum(value * value for value in got) == pytest.approx(1.0)
    single = graphlink.mean_normalized([[3.0, 4.0]])
    assert single == pytest.approx([0.6, 0.8])
    assert graphlink.mean_normalized([[1.0, 0.0], [1.0, 0.0, 0.0]]) is None, "维度不一致不许拍"
    assert graphlink.mean_normalized([[0.0, 0.0]]) is None, "零范数没有方向"
    assert graphlink.mean_normalized([]) is None


def test_compute_edges_threshold_normalizes_endpoints_and_orders() -> None:
    vectors = {1: [1.0, 0.0, 0.0], 2: [1.0, 0.0, 0.0], 3: [0.6, 0.8, 0.0]}
    got = graphlink.compute_edges(vectors, top_k=3, min_score=0.65, max_degree=3)
    assert got == [(1, 2, pytest.approx(1.0))], "0.6 的相似度低于阈值，不许成边"
    # 端点规范化 + 输入顺序无关：字典顺序反着给，结果逐条相同
    reversed_input = {3: [0.6, 0.8, 0.0], 2: [1.0, 0.0, 0.0], 1: [1.0, 0.0, 0.0]}
    assert graphlink.compute_edges(reversed_input, top_k=3, min_score=0.65, max_degree=3) == got


def test_compute_edges_degree_cap_leaves_dense_cluster_isolated_tail() -> None:
    vectors = {index: [1.0, 0.0] for index in range(1, 6)}
    got = graphlink.compute_edges(vectors, top_k=3, min_score=0.65, max_degree=3)
    assert [(left, right) for left, right, _ in got] == [
        (1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4),
    ]
    degree = {index: 0 for index in range(1, 6)}
    for left, right, _ in got:
        degree[left] += 1
        degree[right] += 1
    assert [degree[index] for index in range(1, 6)] == [3, 3, 3, 3, 0], "每篇上限 3，第 5 篇被挤空"
    assert graphlink.compute_edges(vectors, top_k=3, min_score=0.65, max_degree=3) == got


def test_fetch_vectors_default_raises_but_is_not_abstract() -> None:
    """协议里必须是**非抽象**默认实现：加了方法不许把既有测试替身打破。"""
    assert "fetch_vectors" not in VectorStore.__abstractmethods__
    with pytest.raises(NotImplementedError):
        VectorStore.fetch_vectors(object(), ["x"])


def _unit(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(item * item for item in values))
    return [item / norm for item in values]


def test_fetch_vectors_reads_back_vectors_and_dimension(tmp_path) -> None:
    store = QdrantVectorStore(path=str(tmp_path / "vectors"), collection="gl_unit_chunks")
    try:
        store.ensure_collection(3)
        store.upsert([
            VectorPoint(id=_P1, vector=[0.1, 0.2, 0.3], payload={"k": 1}),
            VectorPoint(id=_P2, vector=[3.0, 0.0, -1.0], payload={"k": 2}),
        ])
        got = store.fetch_vectors([_P1, _P2, _P3])
        assert set(got) == {_P1, _P2}, "只回真的存在的点（_P3 不存在）"
        # 余弦距离的集合里本地 Qdrant 存的是单位向量 ⇒ 比方向（尺度对余弦无意义）
        assert got[_P1] == pytest.approx(_unit([0.1, 0.2, 0.3]), abs=1e-6)
        assert got[_P2] == pytest.approx(_unit([3.0, 0.0, -1.0]), abs=1e-6)
        assert [len(value) for value in got.values()] == [3, 3]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_disabled_switch_never_reads_or_writes_new_table(db, engine, monkeypatch) -> None:
    """关态逐字零影响：新表连 SELECT 都不发，旧行不删，图谱不出现 similar 边。"""
    monkeypatch.setattr(settings, "GRAPHLINK_ENABLE", False, raising=False)
    first, _ = await _seed_doc(db, "a.txt", vectors=([1.0, 0.0],))
    second, _ = await _seed_doc(db, "b.txt", vectors=([1.0, 0.0],))
    db.add(DocumentSimilarityEdge(
        kb_id=1, source_document_id=first.id, target_document_id=second.id,
        score=0.93, space_id="sp-a", dim=2,
    ))
    await db.commit()

    statements: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        status = await graphlink.recompute_kb_edges(db, 1, store=_FakeStore())
        graph = await knowledge_graph.build_graph(db)
        removed = await graphlink.delete_edges_for_document(db, first.id)
        batch = await graphlink.recompute_for_kb_ids(db, [1])
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert status["status"] == "disabled"
    assert removed == 0
    assert batch == []
    assert not [item for item in statements if "document_similarity_edges" in item], (
        "关态不许碰新表 —— 一个 SELECT 都不许发"
    )
    assert [edge for edge in graph["edges"] if edge["kind"] == "similar"] == []
    assert set(graph["stats"]) == {"notes", "documents", "ghosts", "edges", "isolated"}
    rows = await _edge_rows(db)
    assert [(row.source_document_id, row.target_document_id) for row in rows] == [
        (first.id, second.id),
    ], "关态连清理都不做：旧行原样躺在表里"


@pytest.mark.asyncio
async def test_enabled_recompute_writes_edges_per_model_space_and_graph_shows_similar(
    db, monkeypatch,
) -> None:
    _enable(monkeypatch)
    first, one = await _seed_doc(db, "a.txt", space="sp-a", vectors=([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]))
    second, two = await _seed_doc(db, "b.txt", space="sp-a", vectors=([1.0, 0.0, 0.0],))
    third, three = await _seed_doc(db, "c.txt", space="sp-b", vectors=([1.0, 0.0, 0.0],))
    fourth, four = await _seed_doc(db, "d.txt", space="sp-b", vectors=([1.0, 0.0, 0.0],))
    store = _FakeStore({**one, **two, **three, **four})

    status = await graphlink.recompute_kb_edges(db, 1, store=store)

    assert status == {
        "status": "written", "kb_id": 1, "edges": 2, "documents": 4, "missing": 0,
    }
    rows = await _edge_rows(db)
    assert [
        (row.source_document_id, row.target_document_id, row.space_id, row.dim) for row in rows
    ] == [
        (min(first.id, second.id), max(first.id, second.id), "sp-a", 3),
        (min(third.id, fourth.id), max(third.id, fourth.id), "sp-b", 3),
    ], "跨模型空间不许对拍（sp-a 与 sp-b 之间 0 条边）"
    assert all(row.score == pytest.approx(1.0) for row in rows)

    graph = await knowledge_graph.build_graph(db)
    similar = sorted(edge["source"] for edge in graph["edges"] if edge["kind"] == "similar")
    assert similar == [f"document:{first.id}", f"document:{third.id}"]
    assert set(graph["stats"]) == {"notes", "documents", "ghosts", "edges", "isolated"}


@pytest.mark.asyncio
async def test_recompute_twice_same_data_is_identical(db, monkeypatch) -> None:
    _enable(monkeypatch)
    mapping: dict[str, list[float]] = {}
    for name in ("a.txt", "b.txt", "c.txt"):
        _, seeded = await _seed_doc(db, name, space="sp-a", vectors=([1.0, 0.0, 0.0],))
        mapping.update(seeded)
    store = _FakeStore(mapping)

    first = await graphlink.recompute_kb_edges(db, 1, store=store)
    snapshot_one = [(row.source_document_id, row.target_document_id, row.score) for row in await _edge_rows(db)]
    second = await graphlink.recompute_kb_edges(db, 1, store=store)
    snapshot_two = [(row.source_document_id, row.target_document_id, row.score) for row in await _edge_rows(db)]

    assert first["status"] == second["status"] == "written"
    assert first["edges"] == second["edges"] == 3
    assert snapshot_one == snapshot_two


@pytest.mark.asyncio
async def test_watermark_mismatch_keeps_previous_edges(db, monkeypatch) -> None:
    """计算期间有人发布新文档 ⇒ 水印不符 ⇒ 本次不提交，上一次结果逐字保留。"""
    _enable(monkeypatch)
    first, one = await _seed_doc(db, "a.txt", space="sp-a", vectors=([1.0, 0.0],))
    second, two = await _seed_doc(db, "b.txt", space="sp-a", vectors=([0.0, 1.0],))
    db.add(DocumentSimilarityEdge(
        kb_id=1, source_document_id=first.id, target_document_id=second.id,
        score=0.123, space_id="sp-a", dim=2,
    ))
    await db.commit()
    real = graphlink._read_representatives

    async def publish_during_compute(session, rows, store):
        fresh = Document(
            filename="late.txt", file_type="txt", status="ready", index_status="ready", kb_id=1,
        )
        session.add(fresh)
        await session.flush()
        build = IndexBuild(
            id="build-late.txt", document_id=fresh.id, state="published", space_id="sp-a",
            point_ids=["late.txt#0"], fingerprint={}, vectorized=True,
        )
        session.add(build)
        await session.flush()
        fresh.active_build_id = build.id
        fresh.content_build_id = build.id
        await session.commit()
        return await real(session, rows, store)

    monkeypatch.setattr(graphlink, "_read_representatives", publish_during_compute)
    before = derived_counters.failure_counts().get("similar_links", 0)

    status = await graphlink.recompute_kb_edges(
        db, 1, store=_FakeStore({**one, **two, "late.txt#0": [1.0, 0.0]}),
    )

    assert status["status"] == "stale"
    assert status["edges"] == 0
    rows = await _edge_rows(db)
    assert [(row.source_document_id, row.target_document_id) for row in rows] == [
        (first.id, second.id),
    ]
    assert rows[0].score == pytest.approx(0.123), "旧边连分数都不许动"
    assert derived_counters.failure_counts().get("similar_links", 0) == before + 1


@pytest.mark.asyncio
async def test_hard_delete_document_clears_both_endpoints(db, monkeypatch) -> None:
    _enable(monkeypatch)
    mapping: dict[str, list[float]] = {}
    docs = []
    for name in ("a.txt", "b.txt", "c.txt"):
        doc, seeded = await _seed_doc(db, name, space="sp-a", vectors=([1.0, 0.0],))
        mapping.update(seeded)
        docs.append(doc)
    await graphlink.recompute_kb_edges(db, 1, store=_FakeStore(mapping))
    assert len(await _edge_rows(db)) == 3

    removed = await graphlink.delete_edges_for_document(db, docs[1].id)

    assert removed == 2, "硬删一篇要清掉它两端的两条边"
    rows = await _edge_rows(db)
    assert [(row.source_document_id, row.target_document_id) for row in rows] == [
        (docs[0].id, docs[2].id),
    ]
