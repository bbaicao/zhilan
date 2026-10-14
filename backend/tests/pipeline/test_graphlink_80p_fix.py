"""「自动连线 80 分补修」定向判据：P1 失败保旧 / P2-A 水印身份与同快照提交 / P2-B 读者过滤 / P3 线程与去重。

规格源 = ``设计说明/自动连线-80分修改计划-20261013.md``（P1 / P2 / P3）：
- **P1**：读向量失败（半套输入）或异常（服务不可用）一律**整库不提交**，旧边**逐字段**原样保留，
  按 `failed` 记账；空库是合法输入（照样重算，结果 0 条边）；试算一行不写；正常对照仍能删旧写新。
- **P2-A**：水印身份 = `(document_id, active_build_id, space_id)`；同一 space 内重建（只有
  `active_build_id` / 分块变）也必须判 `stale`；计算阶段的读事务要在复核之前收干净 ——
  复核读与「删旧 + 插新」落在同一个事务的同一份快照里。
- **P2-B**：与当前构建/模型空间不兼容的旧边（`space_id` 不匹配或维度不等）不得进
  `GET /api/graph` 的 payload，且 payload 形状逐键不变（边仍只有 source/target/kind）。
- **P3**：读向量与 O(n²) 余弦都跑在 worker 线程（事件循环期间仍在服务其他请求）；
  重建入口按 kb 集合去重（每库合计只重算一次）。

替身与夹具复用 ``tests/pipeline/test_graphlink_similar_edges.py`` 的 `_FakeStore` / `_enable` /
`_seed_doc`（同一套窄接口），本文件不改既有判据、只补补修新增的那几条口径。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

import anyio
import pytest
from sqlalchemy import event, select

from app.core.config import settings
from app.models import Document, DocumentSimilarityEdge, IndexBuild
from app.pipeline import graphlink, knowledge_graph, reindex
from tests.pipeline.test_graphlink_similar_edges import _FakeStore, _enable, _seed_doc


#: 固定时间戳（软删标记）：逐字可复现，不用本机钟。
_QUIET = datetime(2026, 10, 13, 12, 0, 0)


async def _edge_snapshot(db) -> list[tuple]:
    """边的**逐字段**快照（含主键与两个身份坐标）——P1「一个字段都不许动」的判据载体。"""
    rows = (await db.execute(
        select(
            DocumentSimilarityEdge.id,
            DocumentSimilarityEdge.kb_id,
            DocumentSimilarityEdge.source_document_id,
            DocumentSimilarityEdge.target_document_id,
            DocumentSimilarityEdge.score,
            DocumentSimilarityEdge.space_id,
            DocumentSimilarityEdge.dim,
        ).order_by(DocumentSimilarityEdge.id)
    )).all()
    return [tuple(row) for row in rows]


async def _endpoints(db) -> list[tuple[int, int]]:
    return [(row[2], row[3]) for row in await _edge_snapshot(db)]


async def _repin(
    db, doc: Document, *, space: str, points: list[str], dim: int | None = None,
) -> str:
    """把一篇文档的当前构建换到新构建（复用 `content_build_id`：模拟**同一空间内重建**）。"""
    build_id = f"rebuilt-{doc.id}-{points[0]}"
    build = IndexBuild(
        id=build_id,
        document_id=doc.id,
        state="published",
        content_build_id=doc.content_build_id,
        space_id=space,
        point_ids=list(points),
        vectorized=True,
        fingerprint={} if dim is None else {"embedding": {"dim": dim}},
    )
    db.add(build)
    await db.flush()
    doc.active_build_id = build_id
    await db.commit()
    return build_id


@pytest.mark.asyncio
async def test_p1_read_failure_keeps_old_edges_field_by_field(db, monkeypatch) -> None:
    _enable(monkeypatch)
    first, one = await _seed_doc(db, "p1-a.txt")
    second, two = await _seed_doc(db, "p1-b.txt")
    await graphlink.recompute_kb_edges(db, 1, store=_FakeStore({**one, **two}))
    before = await _edge_snapshot(db)
    assert len(before) == 1, "对照组：输入齐备时必须写出这条边"

    class _Partial(_FakeStore):
        """只把 `p1-b` 的分块读丢（半套输入），其余照给 ⇒ missing 必须拦下整库提交。"""

        def fetch_vectors(self, ids):
            got = super().fetch_vectors(ids)
            return {key: value for key, value in got.items() if "p1-b" not in key}

    partial = await graphlink.recompute_kb_edges(db, 1, store=_Partial({**one, **two}))
    assert partial["status"] == "failed" and partial["edges"] == 0
    assert await _edge_snapshot(db) == before, "半套输入 ⇒ 旧边逐字段原样保留"

    class _Down(_FakeStore):
        def fetch_vectors(self, ids):
            raise ConnectionError("temporary vector service outage")

    down = await graphlink.recompute_kb_edges(db, 1, store=_Down({**one, **two}))
    assert down["status"] == "failed"
    assert await _edge_snapshot(db) == before, "向量服务不可用 ⇒ 旧边逐字段原样保留"

    dry = await graphlink.recompute_kb_edges(db, 1, store=_Down({**one, **two}), dry_run=True)
    assert dry["status"] == "dry" and dry["edges"] == 0
    assert await _edge_snapshot(db) == before, "试算一行不写"


@pytest.mark.asyncio
async def test_p1_empty_kb_is_a_legal_input_and_clears_stale_edges(db, monkeypatch) -> None:
    _enable(monkeypatch)
    first, one = await _seed_doc(db, "p1-empty-a.txt")
    second, two = await _seed_doc(db, "p1-empty-b.txt")
    await graphlink.recompute_kb_edges(db, 1, store=_FakeStore({**one, **two}))
    assert len(await _edge_snapshot(db)) == 1

    fresh = await graphlink.recompute_kb_edges(db, 99, store=_FakeStore())
    assert fresh["status"] == "written" and fresh["edges"] == 0 and fresh["documents"] == 0

    for doc in (first, second):
        doc.deleted_at = _QUIET
    await db.commit()
    emptied = await graphlink.recompute_kb_edges(db, 1, store=_FakeStore({**one, **two}))
    assert emptied["status"] == "written" and emptied["edges"] == 0
    assert await _edge_snapshot(db) == [], "空库照样重算：陈旧边被清掉，而不是 failed"


@pytest.mark.asyncio
async def test_p1_normal_control_deletes_old_and_writes_new(db, monkeypatch) -> None:
    _enable(monkeypatch)
    first, one = await _seed_doc(db, "p1-new-a.txt", vectors=([1.0, 0.0],))
    second, two = await _seed_doc(db, "p1-new-b.txt", vectors=([1.0, 0.0],))
    third, three = await _seed_doc(db, "p1-new-c.txt", vectors=([0.0, 1.0],))
    await graphlink.recompute_kb_edges(db, 1, store=_FakeStore({**one, **two, **three}))
    assert await _endpoints(db) == [(first.id, second.id)]

    await _repin(db, second, space="sp-a", points=["p1-new-b.txt#re0"])
    result = await graphlink.recompute_kb_edges(
        db, 1, store=_FakeStore({**one, "p1-new-b.txt#re0": [0.0, 1.0], **three}),
    )
    assert result["status"] == "written" and result["edges"] == 1
    assert await _endpoints(db) == [(second.id, third.id)], "正常对照：旧边被删、新边写出"


@pytest.mark.asyncio
async def test_p2a_watermark_identity_carries_the_active_build(db, monkeypatch) -> None:
    _enable(monkeypatch)
    first, _ = await _seed_doc(db, "p2a-wm-a.txt")
    rows = await graphlink._kb_rows(db, 1)
    assert len(rows[0]) == 5, "列序是外部契约：新列只能追加"
    assert rows[0][4] == first.active_build_id, "身份里读得到当前构建 id"

    same = [tuple(list(rows[0][:4]) + [first.active_build_id])]
    assert graphlink.kb_watermark(rows) == graphlink.kb_watermark(same)
    swapped = [tuple(list(rows[0][:4]) + ["another-build"])]
    assert graphlink.kb_watermark(rows) != graphlink.kb_watermark(swapped), (
        "同一 space、同一 content_build_id、只有 active_build_id 变 ⇒ 水印必须变（P2-A）"
    )


@pytest.mark.asyncio
async def test_p2a_same_space_rebuild_during_compute_is_rejected(db, monkeypatch) -> None:
    _enable(monkeypatch)
    first, one = await _seed_doc(db, "p2a-race-a.txt")
    second, two = await _seed_doc(db, "p2a-race-b.txt")
    await graphlink.recompute_kb_edges(db, 1, store=_FakeStore({**one, **two}))
    before = await _edge_snapshot(db)
    original = graphlink._read_representatives

    async def rebuilt_after_read(session, rows, store):
        result = await original(session, rows, store)
        await _repin(session, first, space="sp-a", points=["p2a-race-a.txt#re0"])
        return result

    monkeypatch.setattr(graphlink, "_read_representatives", rebuilt_after_read)
    result = await graphlink.recompute_kb_edges(
        db, 1, store=_FakeStore({**one, "p2a-race-a.txt#re0": [1.0, 0.0], **two}),
    )
    assert result["status"] == "stale", "同一 space 内重建也必须判 stale"
    assert await _edge_snapshot(db) == before, "身份变了 ⇒ 旧边原样保留"


@pytest.mark.asyncio
async def test_p2a_verify_reads_a_fresh_snapshot_without_touching_the_caller(
    db, engine, monkeypatch,
) -> None:
    """P2-A 的身份复核：**独立连接上的新快照**，且一次都不碰调用方的事务状态。

    早先的实现用同一条会话 `commit()+begin()` 刷新快照 —— 复核是新鲜了，但 `rollback()` 会
    **过期调用方的 ORM 实例**，调用方（发布钩子 / reindex 收尾）随后读 `first.id` 就在 async
    上下文里抛 `MissingGreenlet`。现在改为独立会话复核，这里判三件事：
    ① 两次输入读（计算前 / 复核）都在；② 复核读发生在**另一条连接**上，且那是该连接的第一条
    SQL ⇒ 新事务新快照（不是复用某条连接上的旧快照）；③ 调用方连接上，两次输入读之间没有任何
    commit/rollback ⇒ 调用方事务保持原样。
    """
    _enable(monkeypatch)
    first, one = await _seed_doc(db, "p2a-tx-a.txt")
    second, two = await _seed_doc(db, "p2a-tx-b.txt")

    journal: list[tuple[str, str, int]] = []
    sync = engine.sync_engine

    def _recorder(kind: str):
        def _record(conn) -> None:
            journal.append((kind, "", id(conn)))

        return _record

    def _statement(conn, cursor, statement, parameters, context, executemany) -> None:
        journal.append(("sql", " ".join(statement.split()), id(conn)))

    boundaries = [("begin", _recorder("begin")), ("commit", _recorder("commit")),
                  ("rollback", _recorder("rollback"))]
    for name, listener in boundaries:
        event.listen(sync, name, listener)
    event.listen(sync, "before_cursor_execute", _statement)
    try:
        result = await graphlink.recompute_kb_edges(db, 1, store=_FakeStore({**one, **two}))
    finally:
        for name, listener in boundaries:
            event.remove(sync, name, listener)
        event.remove(sync, "before_cursor_execute", _statement)

    assert result["status"] == "written"
    reads = [index for index, (kind, text, _) in enumerate(journal)
             if kind == "sql" and "ORDER BY documents.id" in text]
    first_read, verify = min(reads), max(reads)
    assert first_read < verify, "本用例需要「计算前读一次 + 复核再读一次」两次输入读"
    caller, prober = journal[first_read][2], journal[verify][2]
    assert prober != caller, "复核读必须换一条连接（独立会话 ⇒ 新事务新快照）"
    assert [index for index, (kind, _, conn) in enumerate(journal)
            if kind == "sql" and conn == prober][0] == verify, (
        "复核读必须是那条连接上的第一条 SQL：否则它读的是那条连接上的旧快照"
    )
    assert [item for item in journal[first_read + 1:verify]
            if item[0] != "sql" and item[2] == caller] == [], (
        "复核之前不许在调用方连接上 commit/rollback（读事务保持原样）"
    )
    delete_index = next(index for index, (kind, text, _) in enumerate(journal)
                        if kind == "sql" and text.startswith("DELETE FROM document_similarity_edges"))
    assert verify < delete_index, "复核必须发生在删旧写新之前"
    assert [item for item in journal[verify + 1:delete_index]
            if item[0] != "sql" and item[2] == caller] == [], (
        "复核之后到删旧之前，调用方连接上仍不许有 commit/rollback"
    )


@pytest.mark.asyncio
async def test_p2b_incompatible_edges_never_reach_the_payload(db, monkeypatch) -> None:
    """P2-B：退役模型空间 / 维度不等的旧边不进 payload；payload 形状与 stats 键一字不改。

    ⚠️ 表上有 `UNIQUE(kb_id, source_document_id, target_document_id)`（迁移 0011）⇒ 同一个点对
    只能存一条边，判据必须用**不同点对**分别承载「不兼容」的两种来源。
    """
    _enable(monkeypatch)
    first, _ = await _seed_doc(db, "p2b-live-a.txt")
    second, _ = await _seed_doc(db, "p2b-live-b.txt")
    third, _ = await _seed_doc(db, "p2b-space-a.txt")
    fourth, _ = await _seed_doc(db, "p2b-space-b.txt")
    fifth, _ = await _seed_doc(db, "p2b-dim-a.txt")
    sixth, _ = await _seed_doc(db, "p2b-dim-b.txt")
    seventh, _ = await _seed_doc(db, "p2b-mixed-a.txt")
    eighth, _ = await _seed_doc(db, "p2b-mixed-b.txt")
    ninth, _ = await _seed_doc(db, "p2b-legacy-a.txt")
    tenth, _ = await _seed_doc(db, "p2b-legacy-b.txt")
    for doc in (first, second, fifth, sixth, seventh, eighth):
        build = await db.get(IndexBuild, doc.active_build_id)
        build.fingerprint = {"embedding": {"dim": 3}}
    await db.commit()

    def edge(left: Document, right: Document, space: str, dim: int) -> DocumentSimilarityEdge:
        return DocumentSimilarityEdge(
            kb_id=1,
            source_document_id=left.id,
            target_document_id=right.id,
            score=0.9,
            space_id=space,
            dim=dim,
        )

    db.add_all([
        edge(first, second, "sp-a", 3),      # 当前空间 + 当前维度 ⇒ 可见
        edge(third, fourth, "sp-b", 3),      # 退役的模型空间（当前是 sp-a）⇒ 不可见
        edge(fifth, sixth, "sp-a", 2),       # 当前维度 3，旧边维度 2 ⇒ 不可见
        edge(seventh, eighth, "sp-a", 0),    # 当前维度 3，旧边维度未知 ⇒ 不可见
        edge(ninth, tenth, "sp-a", 0),       # 两端指纹都不带维度：无法证伪 ⇒ 可见
    ])
    await db.commit()

    graph = await knowledge_graph.build_graph(db)
    similar = [item for item in graph["edges"] if item["kind"] == "similar"]
    seen = {(item["source"], item["target"]) for item in similar}
    visible = {
        (f"document:{first.id}", f"document:{second.id}"),
        (f"document:{ninth.id}", f"document:{tenth.id}"),
    }
    invisible = {
        (f"document:{third.id}", f"document:{fourth.id}"),
        (f"document:{fifth.id}", f"document:{sixth.id}"),
        (f"document:{seventh.id}", f"document:{eighth.id}"),
    }
    assert seen == visible, f"退役空间 / 维度不等的旧边不进 payload：{seen}"
    assert len(similar) == 2 and seen.isdisjoint(invisible)
    assert all(set(item) == {"source", "target", "kind"} for item in graph["edges"])
    assert set(graph["stats"]) == {"notes", "documents", "ghosts", "edges", "isolated"}


@pytest.mark.asyncio
async def test_p3_vector_read_and_pair_scoring_run_off_the_event_loop(db, monkeypatch) -> None:
    _enable(monkeypatch)
    first, one = await _seed_doc(db, "p3-thread-a.txt")
    second, two = await _seed_doc(db, "p3-thread-b.txt")
    main_thread = threading.main_thread().ident
    seen: dict[str, int | None] = {}

    real_score, real_read = graphlink._score_spaces, graphlink._read_groups

    def spy_score(*args, **kwargs):
        seen["score"] = threading.get_ident()
        return real_score(*args, **kwargs)

    def spy_read(*args, **kwargs):
        seen["read"] = threading.get_ident()
        return real_read(*args, **kwargs)

    monkeypatch.setattr(graphlink, "_score_spaces", spy_score)
    monkeypatch.setattr(graphlink, "_read_groups", spy_read)
    result = await graphlink.recompute_kb_edges(db, 1, store=_FakeStore({**one, **two}))

    assert result["status"] == "written"
    assert set(seen) == {"read", "score"}, f"两条慢路径都要走到：{seen}"
    assert seen["score"] != main_thread and seen["read"] != main_thread, (
        f"读向量与 O(n²) 余弦都必须在 worker 线程里跑：{seen} vs main={main_thread}"
    )


@pytest.mark.asyncio
async def test_p3_event_loop_keeps_serving_while_pairs_are_scored(db, monkeypatch) -> None:
    """真心跳：O(n²) 正在 worker 线程里算时，事件循环还得能把 ticker 推起来。

    ⚠️ 这一条是独立证据，不是复核者探针 `test_pair_computation_yields_to_other_requests` 的替身：
    那条探针在 wrapper 里调 `asyncio.get_running_loop()`，一旦算力进了工作线程就抛 RuntimeError
    ⇒ 观察集为空 ⇒ `all([])` 恒真（**空集通过**）。这里用「计算中 ticker 累计到几次」判。
    """
    _enable(monkeypatch)
    count, dim = 240, 128
    for index in range(count):
        document = Document(
            filename=f"p3-cpu-{index}.txt", file_type="txt", kb_id=1,
            status="ready", index_status="ready",
        )
        db.add(document)
        await db.flush()
        build_id = f"p3-cpu-build-{index}"
        db.add(IndexBuild(
            id=build_id, document_id=document.id, state="published", fingerprint={},
            space_id="sp-a", point_ids=[f"p3-cpu-{index}"], vectorized=True,
        ))
        document.active_build_id = document.content_build_id = build_id
    await db.commit()
    values = {f"p3-cpu-{index}": [1.0] * dim for index in range(count)}

    real_score = graphlink._score_spaces
    state = {"running": False, "ticks": 0, "seconds": 0.0}

    def measured(*args, **kwargs):
        state["running"] = True
        start = time.perf_counter()
        try:
            return real_score(*args, **kwargs)
        finally:
            state["seconds"] = time.perf_counter() - start
            state["running"] = False

    async def ticker() -> None:
        while True:
            await anyio.sleep(0.001)
            if state["running"]:
                state["ticks"] += 1

    monkeypatch.setattr(graphlink, "_score_spaces", measured)
    async with anyio.create_task_group() as group:
        group.start_soon(ticker)
        result = await graphlink.recompute_kb_edges(
            db, 1, store=_FakeStore(values), dry_run=True,
        )
        group.cancel_scope.cancel()

    assert result["status"] == "dry" and result["documents"] == count
    assert result["edges"] > 0, "正对照：这一轮确实拍了对（不是空跑）"
    assert state["seconds"] >= 0.02, f"O(n²) 段耗时太短，本判据没有判别力：{state}"
    assert state["ticks"] >= 1, (
        f"配对计算期间事件循环一次都没转起来（拿到 {state}）—— 说明 O(n²) 还占着循环"
    )


@pytest.mark.asyncio
async def test_p3_rebuild_recomputes_each_kb_exactly_once(db, monkeypatch) -> None:
    _enable(monkeypatch)
    called: list[int] = []

    async def fake_recompute(session, kb_id, *, store=None, dry_run=False):
        called.append(int(kb_id))
        return {"status": "written", "kb_id": int(kb_id), "edges": 0, "documents": 0, "missing": 0}

    monkeypatch.setattr(graphlink, "recompute_kb_edges", fake_recompute)
    results = await graphlink.recompute_for_kb_ids(db, [3, 1, 3, 2, 1, 2, 3])
    assert called == [1, 2, 3], "三条重建路径共用的入口：每库合计只重算一次（去重 + 升序）"
    assert [item["kb_id"] for item in results] == [1, 2, 3]
    assert await graphlink.recompute_for_kb_ids(db, []) == []


@pytest.mark.asyncio
async def test_off_state_graph_read_never_touches_the_new_table_or_the_identity(
    db, engine, monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "GRAPHLINK_ENABLE", False, raising=False)
    first, _ = await _seed_doc(db, "off-a.txt")
    second, _ = await _seed_doc(db, "off-b.txt")
    db.add(DocumentSimilarityEdge(
        kb_id=1, source_document_id=first.id, target_document_id=second.id,
        score=0.9, space_id="sp-a", dim=2,
    ))
    await db.commit()

    statements: list[str] = []
    sync = engine.sync_engine

    def _statement(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(sync, "before_cursor_execute", _statement)
    try:
        graph = await knowledge_graph.build_graph(db)
    finally:
        event.remove(sync, "before_cursor_execute", _statement)

    assert not [text for text in statements if "document_similarity_edges" in text], (
        "关态连本表都不 SELECT"
    )
    assert not [text for text in statements if "document_builds.space_id" in text], (
        "关态不发 P2-B 的身份查询"
    )
    assert [item for item in graph["edges"] if item["kind"] == "similar"] == []


@pytest.mark.asyncio
async def test_p2a_stale_and_failed_leave_the_caller_session_usable(db, monkeypatch) -> None:
    """回归钉子（Lead 第二批复核）：stale / failed 之后调用方会话与 ORM 实例必须照常可用。

    现场证据：80 分补修复核运行日志 —— 早先实现在 stale
    分支 `rollback()`，把调用方的实例过期掉，既有绿色用例
    `tests/pipeline/test_graphlink_similar_edges.py::test_watermark_mismatch_keeps_previous_edges`
    随即在 `(first.id, second.id)` 处抛 `sqlalchemy.exc.MissingGreenlet`。影响面不止测试：
    `builds.py:408` 发布钩子、`reindex.py` 收尾都还持有 ORM 对象。
    """
    _enable(monkeypatch)
    first, one = await _seed_doc(db, "p2a-usable-a.txt")
    second, two = await _seed_doc(db, "p2a-usable-b.txt")
    await graphlink.recompute_kb_edges(db, 1, store=_FakeStore({**one, **two}))
    before = await _edge_snapshot(db)
    first_identity = (first.id, first.filename)
    second_id = second.id

    class _Unavailable(_FakeStore):
        def fetch_vectors(self, ids):
            raise ConnectionError("temporary vector service outage")

    failed = await graphlink.recompute_kb_edges(db, 1, store=_Unavailable({**one, **two}))
    assert failed["status"] == "failed"
    assert (first.id, first.filename) == first_identity, (
        "failed 之后实例被过期 ⇒ 这里就是 MissingGreenlet"
    )
    assert (await db.execute(
        select(Document.id).where(Document.id == first_identity[0])
    )).scalar() == first_identity[0], "failed 之后调用方会话还能发查询"
    assert await _edge_snapshot(db) == before, "failed ⇒ 旧边逐字段原样"

    original = graphlink._read_representatives

    async def rebuilt_after_read(session, rows, store):
        result = await original(session, rows, store)
        await _repin(session, first, space="sp-a", points=["p2a-usable-a.txt#re0"])
        return result

    monkeypatch.setattr(graphlink, "_read_representatives", rebuilt_after_read)
    stale = await graphlink.recompute_kb_edges(
        db, 1, store=_FakeStore({**one, "p2a-usable-a.txt#re0": [1.0, 0.0], **two}),
    )
    assert stale["status"] == "stale"
    assert (first.id, first.filename) == first_identity, (
        "stale 之后实例被过期 ⇒ 这里就是 MissingGreenlet"
    )
    assert (await db.execute(
        select(Document.id).where(Document.id == second_id)
    )).scalar() == second_id, "stale 之后调用方会话还能发查询"
    assert await _edge_snapshot(db) == before, "stale ⇒ 旧边逐字段原样"


@pytest.mark.asyncio
async def test_p3_batch_scope_defers_the_publish_hook_and_restores_it(
    db, engine, monkeypatch,
) -> None:
    """P3②：批量重建范围内逐篇钩子 ⇒ `deferred`（一条 SQL 都不发），出范围照常触发。"""
    _enable(monkeypatch)
    first, _ = await _seed_doc(db, "p3-batch-a.txt")
    calls: list[int] = []
    real = graphlink.recompute_kb_edges

    async def spy(session, kb_id, *, store=None, dry_run=False):
        calls.append(int(kb_id))
        return await real(session, kb_id, store=store, dry_run=dry_run)

    monkeypatch.setattr(graphlink, "recompute_kb_edges", spy)
    statements: list[str] = []
    sync = engine.sync_engine

    def _statement(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(sync, "before_cursor_execute", _statement)
    try:
        with graphlink.batch_scope():
            assert graphlink._BATCH_DEPTH.get() == 1
            with graphlink.batch_scope():
                assert graphlink._BATCH_DEPTH.get() == 2
            assert graphlink._BATCH_DEPTH.get() == 1, "嵌套范围逐层恢复"
            deferred = await graphlink.recompute_after_publish(db, first.id)
            assert deferred["status"] == "deferred"
            assert calls == [] and statements == [], (
                f"范围内连文档身份都不读（零 SQL）：calls={calls} sql={statements[:3]}"
            )
        assert graphlink._BATCH_DEPTH.get() == 0
        after = await graphlink.recompute_after_publish(db, first.id)
    finally:
        event.remove(sync, "before_cursor_execute", _statement)

    assert after["status"] != "deferred", "出了批次范围，逐篇发布钩子必须恢复触发"
    assert calls == [1], f"恢复后钩子只该调一次：{calls}"


@pytest.mark.asyncio
async def test_p3_batch_scope_is_task_local_and_never_leaks(db, monkeypatch) -> None:
    """P3②：暂缓态是**任务局部**的；批次抛错、被取消都必须恢复（否则下一次导入静默不连线）。"""
    _enable(monkeypatch)
    await _seed_doc(db, "p3-local-a.txt")
    seen: dict[str, int] = {}
    gate = anyio.Event()

    async def observer() -> None:
        await gate.wait()
        seen["child"] = graphlink._BATCH_DEPTH.get()

    async with anyio.create_task_group() as group:
        group.start_soon(observer)  # 先起任务：拿到的是**范围外**的上下文快照
        with graphlink.batch_scope():
            seen["inside"] = graphlink._BATCH_DEPTH.get()
            gate.set()
            await anyio.sleep(0.02)
        group.cancel_scope.cancel()

    assert seen == {"inside": 1, "child": 0}, (
        f"暂缓态只属于当前任务（不是进程全局开关）：并发任务照常触发 —— {seen}"
    )

    with pytest.raises(RuntimeError):
        with graphlink.batch_scope():
            raise RuntimeError("批次中途炸了")
    assert graphlink._BATCH_DEPTH.get() == 0, "抛错必须恢复（finally）"

    cancelled: dict[str, int] = {}

    with anyio.move_on_after(0.02):
        with graphlink.batch_scope():
            cancelled["depth"] = graphlink._BATCH_DEPTH.get()
            await anyio.sleep(5)
    assert cancelled["depth"] == 1
    assert graphlink._BATCH_DEPTH.get() == 0, "取消必须恢复（finally）"


@pytest.mark.asyncio
async def test_p3_batch_defers_every_publish_then_recomputes_each_kb_once(
    db, monkeypatch,
) -> None:
    """P3②：批次内 N 次发布零重算（N+1 → 1），收尾 `recompute_for_kb_ids` 每库只算一次。"""
    _enable(monkeypatch)
    first, one = await _seed_doc(db, "p3-count-a.txt")
    second, two = await _seed_doc(db, "p3-count-b.txt")
    calls: list[int] = []
    real = graphlink.recompute_kb_edges

    async def spy(session, kb_id, *, store=None, dry_run=False):
        calls.append(int(kb_id))
        return await real(session, kb_id, store=store, dry_run=dry_run)

    monkeypatch.setattr(graphlink, "recompute_kb_edges", spy)
    with graphlink.batch_scope():
        for document_id in (first.id, second.id, first.id):
            outcome = await graphlink.recompute_after_publish(db, document_id)
            assert outcome["status"] == "deferred"
        assert calls == [], f"批次内一次重算都不该发生：{calls}"
        tail = await graphlink.recompute_for_kb_ids(db, [1, 1, 1], store=_FakeStore({**one, **two}))

    assert calls == [1], f"收尾每库合计只算一次：{calls}"
    assert [item["status"] for item in tail] == ["written"]
    assert [item["edges"] for item in tail] == [1]


@pytest.mark.asyncio
async def test_p3_full_library_computation_stays_bounded(monkeypatch) -> None:
    """P3②：同一事件循环里同时只跑一份全库 O(n²)，第二份要排队（避免堆积多份全库计算）。"""
    state = {"active": 0, "peak": 0, "started": 0}

    def slow_score(*args, **kwargs):
        state["active"] += 1
        state["started"] += 1
        state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        state["active"] -= 1
        return []

    monkeypatch.setattr(graphlink, "_score_spaces", slow_score)

    async def one_pass() -> None:
        async with graphlink._compute_guard():
            await anyio.to_thread.run_sync(graphlink._score_spaces, {}, 3, 0.0, 3)

    async with anyio.create_task_group() as group:
        for _ in range(3):
            group.start_soon(one_pass)

    assert state["started"] == 3, f"三份都要跑完（不许被吞）：{state}"
    assert state["peak"] == 1, f"同一时刻只许一份全库计算在跑：{state}"


@pytest.mark.asyncio
async def test_p3_reindex_wraps_the_rebuild_in_one_batch_scope(db, monkeypatch) -> None:
    """P3②③：`reindex_documents` 整段确实进入批量范围（否则逐篇钩子照旧 N+1 次全库重算）。"""
    _enable(monkeypatch)
    entered: list[int] = []
    real_scope = graphlink.batch_scope

    def spy_scope():
        entered.append(1)
        return real_scope()

    async def no_targets(session, document_id):
        return []

    monkeypatch.setattr(graphlink, "batch_scope", spy_scope)
    monkeypatch.setattr(reindex, "_load_targets", no_targets)
    await reindex.reindex_documents(db)

    assert entered == [1], "批量重建必须整段包在一次 `batch_scope()` 里"
    assert reindex.reindex_documents is not reindex._reindex_documents
    assert callable(reindex.__dict__["reindex_documents"])
