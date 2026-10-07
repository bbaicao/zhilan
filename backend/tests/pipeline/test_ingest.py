"""ingest_document / 状态机 / 重复判定 / 失败可重试 / purge 集成测试。

用 ``db`` 夹具（真 MySQL 测试库）；每个用例前清 documents/chunks 保证隔离；
上传落档目录重定向到 tmp_path，避免往仓库目录写文件。
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.core.config import settings
from app.core.errors import DocumentParseError, DuplicateDocument
from app.models import Chunk, Document
from app.pipeline import ingest
from app.pipeline.builds import current_chunks
from app.vector.base import VectorHit
from app.vector.embed import FakeEmbedding
from app.vector.qdrant_store import QdrantVectorStore


@pytest_asyncio.fixture(autouse=True)
async def _clean_documents(db):
    """每个用例前清 documents/chunks（conftest 的 _CLEAN_ORDER 不含这两张表）。"""
    await db.execute(text("DELETE FROM chunks"))
    await db.execute(text("DELETE FROM documents"))
    await db.commit()
    yield


@pytest.fixture(autouse=True)
def _isolate_vector(monkeypatch):
    """隔离真 embedding / 真 Qdrant（条目 2.3 起 index_chunks 会走真实路径）。

    本文件只关心 MySQL 状态机与重复判定：embed 快速失败 → ``vectorized=false``
    （既有断言保持不变）；purge 的向量删除也快速失败 → 只 warning，MySQL 仍清。
    """

    class _NoEmbed:
        dim = 4

        async def embed_texts(self, texts):
            raise RuntimeError("测试隔离：不调用真实 embedding")

    class _NoStore:
        def for_space(self, space_id):
            return self

        def ensure_collection(self, dim):
            raise RuntimeError("测试隔离：不连 Qdrant")

        def upsert(self, points):
            raise RuntimeError("测试隔离：不连 Qdrant")

        def delete(self, ids):
            raise RuntimeError("测试隔离：不连 Qdrant")

    monkeypatch.setattr(ingest, "get_embedding_client", lambda *, fake=False: _NoEmbed())
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: _NoStore())


@pytest.fixture
def _upload_dir(monkeypatch, tmp_path):
    """把上传留档目录重定向到 tmp_path（property 不能被实例属性覆盖，改类属性）。"""
    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path))
    return tmp_path


@pytest.fixture
def _working_vector(monkeypatch, tmp_path):
    """已启用资料的成功重建必须真正完成离线向量写入。"""
    embedding = FakeEmbedding(dim=4)
    store = QdrantVectorStore(path=str(tmp_path / "vectors"))
    monkeypatch.setattr(ingest, "get_embedding_client", lambda **kw: embedding)
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: store)
    try:
        yield store
    finally:
        store.close()


# --------------------------------------------------------------------------
# a. 正常 MD 上传：状态机 / 落档 / chunks 元数据
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_md_upload(db, _upload_dir):
    content = (
        "# 第一章\n\n这是第一段正文，用于验证切块与落库。\n\n"
        "# 第二章\n\n这是第二段正文，内容足够长以切出多个块用于断言。\n"
    ) * 3
    result = await ingest.ingest_document(db, "doc.md", content.encode("utf-8"))

    await db.rollback()
    doc = await db.get(Document, result.document_id)
    assert doc.index_status == "ready"
    assert doc.status == "ready"
    assert doc.chunk_count > 0
    assert doc.vectorized is False
    # 留档：路径在上传目录、且磁盘文件真实存在
    assert doc.file_path and Path(doc.file_path).exists()
    assert doc.file_path.startswith(str(_upload_dir))

    chunks = (
        await db.execute(select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.ordinal))
    ).scalars().all()
    # ordinal 连续
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    # start_offset < end_offset
    assert all(c.start_offset < c.end_offset for c in chunks)
    # MD 无页信息 → page 恒 None
    assert all(c.page is None for c in chunks)


# --------------------------------------------------------------------------
# b. 状态机：写 chunks 失败 → failed 行；重传同内容 → 就地重试（id 不变）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_machine_failed_then_inplace_retry(db, _upload_dir, monkeypatch):
    content = "# 标题\n\n正文内容用于状态机入库测试。\n" * 4
    orig = ingest._persist_chunks

    async def _boom(session, doc, prepared):
        raise RuntimeError("模拟写 chunks 失败")

    monkeypatch.setattr(ingest, "_persist_chunks", _boom)
    with pytest.raises(RuntimeError):
        await ingest.ingest_document(db, "fail.md", content.encode("utf-8"))

    # 1) 失败留行：index_status/status 都 failed，index_error 中文非空
    rows = (await db.execute(select(Document))).scalars().all()
    assert len(rows) == 1
    failed = rows[0]
    assert failed.index_status == "failed"
    assert failed.status == "failed"
    assert failed.index_error and "模拟写 chunks 失败" in failed.index_error

    # 2) 重传同内容 → 就地重试：document id 不变、最终 ready
    monkeypatch.setattr(ingest, "_persist_chunks", orig)
    result = await ingest.ingest_document(db, "fail.md", content.encode("utf-8"))
    assert result.document_id == failed.id, "就地重试必须复用同一 document id"

    await db.rollback()
    fresh = await db.get(Document, result.document_id)
    assert fresh.index_status == "ready"
    assert fresh.status == "ready"
    assert fresh.chunk_count > 0


@pytest.mark.asyncio
async def test_retry_document_from_stored(db, _upload_dir, monkeypatch):
    """retry_document 从留档重读字节重跑（失败可重试的第二条入口）。"""
    content = "# 重试\n\n从留档重读字节重跑入库。\n" * 3
    orig = ingest._persist_chunks

    async def _boom(session, doc, prepared):
        raise RuntimeError("模拟写 chunks 失败")

    monkeypatch.setattr(ingest, "_persist_chunks", _boom)
    with pytest.raises(RuntimeError):
        await ingest.ingest_document(db, "retry.md", content.encode("utf-8"))
    monkeypatch.setattr(ingest, "_persist_chunks", orig)

    rows = (await db.execute(select(Document))).scalars().all()
    failed_id = rows[0].id
    # 留档丢失的情况另行测；这里留档在，retry_document 应成功
    result = await ingest.retry_document(db, failed_id)
    assert result.document_id == failed_id
    await db.rollback()
    fresh = await db.get(Document, failed_id)
    assert fresh.index_status == "ready"


# --------------------------------------------------------------------------
# c. 重复内容（正常状态）→ DuplicateDocument，document_id 指向既有行
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_content_raises(db, _upload_dir):
    content = "# 重复\n\n同一份内容两次上传应被拦截。\n" * 3
    r1 = await ingest.ingest_document(db, "a.md", content.encode("utf-8"))
    with pytest.raises(DuplicateDocument) as exc_info:
        await ingest.ingest_document(db, "b.md", content.encode("utf-8"))
    assert exc_info.value.document_id == r1.document_id
    # 不应留下第二个文档行（无垃圾行）
    rows = (await db.execute(select(Document))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_overwrite_rebuilds_same_id_and_preserves_history(db, _upload_dir, _working_vector):
    """覆盖成功切换同一文档的启用构建，当前块不重复，历史块留作旧引用证据。"""
    content = "# 覆盖\n\noverwrite 应重建同一行，旧引用与旧正文仍然保留。\n" * 3
    first = await ingest.ingest_document(db, "o.md", content.encode("utf-8"))
    old_build = (await db.get(Document, first.document_id)).active_build_id
    old_ids = [chunk.id for chunk in await current_chunks(db, first.document_id)]
    second = await ingest.ingest_document(db, "o.md", content.encode("utf-8"), overwrite=True)

    assert second.document_id == first.document_id
    rows = (await db.execute(select(Document))).scalars().all()
    assert len(rows) == 1, "覆盖不应产生第二个文档"
    assert rows[0].active_build_id != old_build
    chunks = await current_chunks(db, second.document_id)
    assert len(chunks) == second.chunk_count, "当前版本不应混入历史分块"
    assert set(old_ids).isdisjoint(chunk.id for chunk in chunks)
    assert all([(await db.get(Chunk, chunk_id)) is not None for chunk_id in old_ids])


# --------------------------------------------------------------------------
# d. 坏 PDF / 扫描版 PDF → DocumentParseError，且留下 failed 行（选定语义）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_pdf_leaves_failed_row(db, _upload_dir, monkeypatch):
    def _bad_parse(filename, data):
        raise DocumentParseError("该 PDF 未提取到文字，请上传文字版或 txt/md。")

    monkeypatch.setattr(ingest, "parse_document_pages", _bad_parse)
    with pytest.raises(DocumentParseError):
        await ingest.ingest_document(db, "scan.pdf", b"%PDF-1.4 fake scanned")

    rows = (await db.execute(select(Document))).scalars().all()
    # 选定语义：失败「留行」（pending 行置 failed），而非不留行——这样前端能展示
    # 失败原因、且用户可重传触发就地重试。
    assert len(rows) == 1
    doc = rows[0]
    assert doc.index_status == "failed"
    assert doc.status == "failed"
    assert doc.index_error and "PDF" in doc.index_error


# --------------------------------------------------------------------------
# e. purge_document 后 chunks 表无残留
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_removes_chunks(db, _upload_dir):
    content = "# 待删除\n\npurge 后 chunks 不应残留。\n" * 3
    r = await ingest.ingest_document(db, "p.md", content.encode("utf-8"))
    await ingest.purge_document(db, r.document_id)

    docs = (
        await db.execute(select(Document).where(Document.id == r.document_id))
    ).scalars().all()
    assert docs == []
    chunks = (
        await db.execute(select(Chunk).where(Chunk.document_id == r.document_id))
    ).scalars().all()
    assert chunks == []


# --------------------------------------------------------------------------
# f. 解析阶段失败的行按「留档路径」匹配：坏文件连传两次不留垃圾行（文档 id 不变）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_file_reupload_matches_failed_row_by_path(db, _upload_dir):
    """解析就失败的行走不到 content_hash（那列为 NULL），重传必须按 ``file_path``
    命中同一行 —— 否则同一个坏文件每传一次就多一行 failed 垃圾行。

    ``save_upload`` 的落档名是内容寻址的（``<sha256[:8]>-<安全名>``），
    所以「同内容重复上传 → 同一个 file_path」，file_path 是可靠匹配键。
    """
    bad = b"%PDF-1.4 not really a pdf"

    with pytest.raises(DocumentParseError):
        await ingest.ingest_document(db, "broken.pdf", bad)
    first = (await db.execute(select(Document))).scalars().all()
    assert len(first) == 1
    assert first[0].index_status == "failed"
    assert first[0].content_hash is None, "解析失败的行没有 content_hash，只能按 file_path 匹配"

    with pytest.raises(DocumentParseError):
        await ingest.ingest_document(db, "broken.pdf", bad)
    rows = (await db.execute(select(Document))).scalars().all()
    assert len(rows) == 1, f"重传坏文件不该多留垃圾行，实际 {len(rows)} 行"
    assert rows[0].id == first[0].id, "应当复用同一个 document id（就地重试）"


# --------------------------------------------------------------------------
# g. 真 DB 错误（flush 阶段）也必须落 failed，不能卡在 processing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_error_marks_failed_not_stuck_processing(db, _upload_dir, monkeypatch):
    """失败来自 DB 层时，session 会进入 pending-rollback；不先 rollback 就 commit，
    「置 failed」这一步自己会抛 PendingRollbackError，行永久卡在 processing。

    这里用真唯一约束冲突（``uk_chunks_document_build_ordinal``：同构建同 ordinal
    两行）逼出 flush 阶段的 DB 错误。
    """

    async def _dup_ordinal(session, doc, prepared):
        rows = [
            Chunk(document_id=doc.id, content="a", ordinal=0),
            Chunk(document_id=doc.id, content="b", ordinal=0),
        ]
        session.add_all(rows)
        return rows

    monkeypatch.setattr(ingest, "_persist_chunks", _dup_ordinal)
    with pytest.raises(Exception):
        await ingest.ingest_document(db, "flushfail.md", ("# 标题\n\n正文\n" * 4).encode("utf-8"))

    rows = (await db.execute(select(Document))).scalars().all()
    assert len(rows) == 1
    doc = rows[0]
    assert doc.index_status == "failed", f"必须落 failed，实际 {doc.index_status}"
    assert doc.status == "failed"
    assert doc.index_error, "失败原因必须可读非空"
    # session 必须已被 rollback 拉回可用状态（否则这里会抛 PendingRollbackError）
    assert (await db.execute(select(Chunk))).scalars().all() == []


# --------------------------------------------------------------------------
# h. retry_document：新构建替换当前残留分块，历史分块不进入当前正文
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_document_replaces_stale_current_chunks(db, _upload_dir, _working_vector):
    """重试复用同一文档，新构建使用独立 ordinal 空间且当前正文不含残留块。"""
    content = "# 残留\n\n重试前先清旧块。\n" * 3
    r = await ingest.ingest_document(db, "stale.md", content.encode("utf-8"))

    # 人为把这一行改造成「上一轮写了一半」的状态：只剩一个 ordinal=0 的残留块。
    # 新块也从 ordinal=0 开始；只有当前版本的块能进入正文查询。
    await db.execute(text("DELETE FROM chunks WHERE document_id = :d"), {"d": r.document_id})
    db.add(Chunk(document_id=r.document_id, content="残留块", ordinal=0))
    await db.commit()

    result = await ingest.retry_document(db, r.document_id)
    assert result.document_id == r.document_id
    await db.rollback()
    fresh = await db.get(Document, r.document_id)
    assert fresh.index_status == "ready"
    chunks = await current_chunks(db, r.document_id)
    assert [c.ordinal for c in chunks] == list(range(len(chunks))), "当前分块 ordinal 必须连续"
    assert all(c.content != "残留块" for c in chunks)


# --------------------------------------------------------------------------
# i. frontmatter 只对 markdown 解析（非 md 的 `---` 是正文，不许被当 YAML 剥掉）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frontmatter_only_stripped_for_markdown(db, _upload_dir):
    """老仓库只在后缀是 md/markdown 时解析 frontmatter（``_prepare`` L119-124）；
    对 .txt 无条件解析会把正文里的 YAML 风格段落当元数据剥掉——真丢内容。

    注：断言用「键行文本」而不是 `---` 本身——清洗的重复段落去重会把同块内
    重复出现的 `---` 折叠成一条（那是清洗的既有行为，不是 frontmatter 造成的）。
    """
    body = "---\ncategory: 探针\n---\n\n这是正文。\n"

    await ingest.ingest_document(db, "a.txt", body.encode("utf-8"))
    txt_doc = (await db.execute(select(Document).order_by(Document.id.desc()))).scalars().first()
    assert txt_doc.category == "", ".txt 不解析 frontmatter，category 应为空"
    txt_chunks = (
        await db.execute(select(Chunk.content).where(Chunk.document_id == txt_doc.id))
    ).scalars().all()
    assert any("category: 探针" in piece for piece in txt_chunks), ".txt 里的元数据风格段落必须作为正文保留"

    await ingest.ingest_document(db, "b.md", body.encode("utf-8"))
    md_doc = (await db.execute(select(Document).order_by(Document.id.desc()))).scalars().first()
    assert md_doc.category == "探针", ".md 的 frontmatter category 必须解析出来"
    md_chunks = (
        await db.execute(select(Chunk.content).where(Chunk.document_id == md_doc.id))
    ).scalars().all()
    assert all("category: 探针" not in piece for piece in md_chunks), ".md 的 YAML 块必须在清洗前剥离"


# --------------------------------------------------------------------------
# j. 回归修复：解析段 / 向量写段必须丢线程（慢解析 / 慢写不再冻结事件循环）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_segment_runs_off_the_event_loop(db, _upload_dir, monkeypatch):
    """**回归修复 · 解析段**：``prepare_document → parse_document_pages``
    （MinerU OCR / nvidia-smi 探测 / 解码全在这棵调用树里，纯 CPU/IO、不碰 session）
    必须整体丢线程执行 —— 扫描件一次慢解析不再冻结整个事件循环。

    判别法：解析桩记「解析窗口」（进/出各打一次 ``perf_counter``）+ 执行线程；
    主协程在入库任务运行期间以 20ms 心跳自旋。若解析仍跑在事件循环上，
    解析窗口内**零心跳**（循环被同步段占死）⇒ 红。
    """
    loop_tid = threading.get_ident()
    parse_tids: list[int] = []
    window: list[float] = []

    def _slow_parse(filename, data):
        parse_tids.append(threading.get_ident())
        window.append(time.perf_counter())
        time.sleep(0.3)  # 模拟扫描件 OCR 的同步耗时
        window.append(time.perf_counter())
        return "慢解析正文，用于事件循环占用判据。\n" * 3, []

    monkeypatch.setattr(ingest, "parse_document_pages", _slow_parse)

    content = "# 慢解析\n\n解析必须在线程里跑。\n" * 3
    task = asyncio.ensure_future(
        ingest.ingest_document(db, "slow.md", content.encode("utf-8"))
    )
    ticks_in_window = 0
    while not task.done():
        await asyncio.sleep(0.02)
        now = time.perf_counter()
        # 窗口已开（记了进入时刻）且未关（还没记返回时刻）即计一次心跳
        if window and window[0] <= now and (len(window) < 2 or now <= window[1]):
            ticks_in_window += 1
    result = await task

    assert result.chunk_count > 0
    assert parse_tids, "解析桩没被调到 ⇒ 判据无效"
    assert all(t != loop_tid for t in parse_tids), (
        "解析跑在事件循环线程上 ⇒（回归修复 未修）"
    )
    assert ticks_in_window > 0, (
        "解析窗口内事件循环零心跳 ⇒ 一次慢解析冻结整个后端（回归修复 未修）"
    )


@pytest.mark.asyncio
async def test_vector_upsert_runs_off_the_event_loop(db, _upload_dir, monkeypatch):
    """**回归修复 · 向量写段**：``ensure_collection + upsert(wait=True)``（同步
    Qdrant 客户端）必须丢线程 —— 大批量点写入不再冻结事件循环。

    桩 embed 给定维向量、桩 store 的 ``ensure_collection`` 记窗口 + 睡 0.3s（模拟慢写）；
    判别法与解析段同款（窗口内心跳必须推进 + 执行线程必须异于循环线程）。
    """
    loop_tid = threading.get_ident()
    store_tids: list[int] = []
    window: list[float] = []

    class _FakeEmbed:
        dim = 4

        async def embed_texts(self, texts):
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    class _SlowStore:
        def for_space(self, space_id):
            return self

        def ensure_collection(self, dim):
            store_tids.append(threading.get_ident())
            window.append(time.perf_counter())
            time.sleep(0.3)
            window.append(time.perf_counter())

        def upsert(self, points):
            assert points, "应有待写点"
            self.points = list(points)

        def get_by_ids(self, ids):
            return [
                VectorHit(id=point.id, score=0.0, payload=point.payload)
                for point in self.points if point.id in ids
            ]

    # 测试级 monkeypatch 晚于 autouse 的 _isolate_vector 生效 ⇒ 用桩件顶掉「快速失败」隔离件
    monkeypatch.setattr(ingest, "get_embedding_client", lambda *, fake=False: _FakeEmbed())
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: _SlowStore())

    content = "# 向量段\n\n慢写也必须离循环。\n" * 3
    task = asyncio.ensure_future(
        ingest.ingest_document(db, "vec.md", content.encode("utf-8"))
    )
    ticks_in_window = 0
    while not task.done():
        await asyncio.sleep(0.02)
        now = time.perf_counter()
        # 窗口已开（记了进入时刻）且未关（还没记返回时刻）即计一次心跳
        if window and window[0] <= now and (len(window) < 2 or now <= window[1]):
            ticks_in_window += 1
    result = await task

    assert result.vectorized is True
    assert store_tids, "桩 store 没被调到 ⇒ 判据无效"
    assert all(t != loop_tid for t in store_tids), (
        "向量写跑在事件循环线程上 ⇒（回归修复 未修）"
    )
    assert ticks_in_window > 0, "向量写窗口内事件循环零心跳 ⇒（回归修复 未修）"


@pytest.mark.asyncio
async def test_retry_document_parse_runs_off_the_event_loop(
    db, _upload_dir, _working_vector, monkeypatch
):
    """**回归修复 · 重试路径**：``retry_document`` 的 ``prepare_document`` 同款丢线程。"""
    loop_tid = threading.get_ident()
    parse_tids: list[int] = []

    def _tid_parse(filename, data):
        parse_tids.append(threading.get_ident())
        return "重试正文。\n" * 3, []

    content = "# 重试\n\n先正常入库一次，再从留档重试。\n" * 3
    first = await ingest.ingest_document(db, "retry-tid.md", content.encode("utf-8"))
    monkeypatch.setattr(ingest, "parse_document_pages", _tid_parse)
    result = await ingest.retry_document(db, first.document_id)

    assert result.document_id == first.document_id
    assert parse_tids, "解析桩没被调到 ⇒ 判据无效"
    assert all(t != loop_tid for t in parse_tids), (
        "retry_document 的解析跑在事件循环线程上 ⇒（回归修复 未修）"
    )
