"""条目 2.3：``index_chunks`` / ``purge_document`` 向量级联单测。

依赖注入：monkeypatch ``ingest.get_embedding_client`` / ``ingest.get_vector_store``，
注入 FakeEmbedding(dim=16) + 本地 path Qdrant，不连真 embedding API / 真 Qdrant 服务。
DB 走 conftest 的 ``db`` 夹具（真 MySQL 测试库）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.models import Chunk, Document, IndexBuild
from app.pipeline import ingest
from app.pipeline.builds import current_chunks
from app.vector.embed import FakeEmbedding, OpenAICompatEmbedding
from app.vector.qdrant_store import QdrantVectorStore


@pytest_asyncio.fixture(autouse=True)
async def _clean_documents(db):
    """每个用例前清 documents/chunks，保证隔离。"""
    await db.execute(text("DELETE FROM chunks"))
    await db.execute(text("DELETE FROM documents"))
    await db.commit()
    yield


@pytest.fixture
def store(tmp_path) -> QdrantVectorStore:
    s = QdrantVectorStore(path=str(tmp_path / "qdrant"), collection="idx_unit_chunks")
    s.ensure_collection(dim=16)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def _vector_deps(monkeypatch, store):
    """注入 FakeEmbedding(dim=16) + 本地 Qdrant store。"""
    emb = FakeEmbedding(dim=16)
    monkeypatch.setattr(ingest, "get_embedding_client", lambda *, fake=False: emb)
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: store)
    return emb, store


async def _make_doc_with_chunks(db, n: int = 3) -> tuple[Document, list[Chunk]]:
    doc = Document(
        filename="向量测试.md",
        file_type="md",
        status="ready",
        index_status="ready",
        chunk_count=n,
        kb_id=1,
        category="测试分类",
    )
    db.add(doc)
    await db.flush()
    chunks = [
        Chunk(document_id=doc.id, content=f"第 {i} 块正文，用于向量化验证。", ordinal=i)
        for i in range(n)
    ]
    db.add_all(chunks)
    await db.flush()
    return doc, chunks


# --------------------------------------------------------------------------
# a. 成功路径：启用构建的 UUID 清单完整，payload 含 chunk_id / document_id / ordinal
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_chunks_success(db, store, _vector_deps):
    doc, chunks = await _make_doc_with_chunks(db, 3)
    ok, err = await ingest.index_chunks(db, doc)

    assert ok is True
    assert err is None

    build = await db.get(IndexBuild, doc.active_build_id)
    ids = list(build.point_ids)
    hits = store.for_space(build.space_id).get_by_ids(ids)
    assert {h.id for h in hits} == set(ids), "全部 chunk 都必须写入 Qdrant"

    by_id = {h.payload["chunk_id"]: h for h in hits}
    assert set(by_id) == {c.id for c in chunks}
    for c in chunks:
        payload = by_id[c.id].payload
        assert payload["build_id"] == build.id
        assert payload["space_id"] == build.space_id
        assert payload["document_id"] == doc.id
        assert payload["ordinal"] == c.ordinal
        assert payload["kb_id"] == 1
        assert payload["filename"] == "向量测试.md"
        assert payload["category"] == "测试分类"


@pytest.mark.asyncio
async def test_index_chunks_via_finish_ingest_marks_vectorized(db, store, _vector_deps, tmp_path, monkeypatch):
    """走完整 ``ingest_document``：成功后 ``vectorized=true``。"""
    from app.core.config import settings

    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path))
    content = "# 标题\n\n这是用于验证向量写入路径的正文内容。\n" * 4
    result = await ingest.ingest_document(db, "vec.md", content.encode("utf-8"))

    await db.rollback()
    doc = await db.get(Document, result.document_id)
    assert doc.index_status == "ready"
    assert doc.vectorized is True
    assert doc.vector_error is None

    chunk_rows = await current_chunks(db, doc.id)
    build = await db.get(IndexBuild, doc.active_build_id)
    ids = list(build.point_ids)
    assert len(store.for_space(build.space_id).get_by_ids(ids)) == len(chunk_rows)


# --------------------------------------------------------------------------
# b. embed 失败 → (False, 中文错误)，不抛
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_chunks_embed_failure_returns_error(db, store, monkeypatch):
    class Boom(FakeEmbedding):
        async def embed_texts(self, texts):
            raise RuntimeError("模拟 embedding 失败")

    monkeypatch.setattr(ingest, "get_embedding_client", lambda *, fake=False: Boom(dim=16))
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: store)

    doc, _ = await _make_doc_with_chunks(db, 2)
    ok, err = await ingest.index_chunks(db, doc)

    assert ok is False
    assert err is not None
    assert "模拟 embedding 失败" in err
    assert len(err) <= 255


@pytest.mark.asyncio
async def test_index_chunks_empty_vectors_returns_error(db, store, monkeypatch):
    class Empty(FakeEmbedding):
        async def embed_texts(self, texts):
            return []

    monkeypatch.setattr(ingest, "get_embedding_client", lambda *, fake=False: Empty(dim=16))
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: store)

    doc, _ = await _make_doc_with_chunks(db, 2)
    ok, err = await ingest.index_chunks(db, doc)
    assert ok is False
    assert err and "向量" in err


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["count", "dimension", "boolean", "nonfinite"])
async def test_invalid_vectors_preserve_published_build(db, store, _vector_deps, monkeypatch, bad):
    """合法旧版先发布；非法输出不得写入候选点，也不得替换旧版与原块。"""
    doc, chunks = await _make_doc_with_chunks(db, 2)
    assert (await ingest.index_chunks(db, doc)) == (True, None)
    doc_id, old_pointer = doc.id, doc.active_build_id
    old_chunk_ids = [chunk.id for chunk in chunks]
    old_build = await db.get(IndexBuild, old_pointer)
    old_points, old_space = list(old_build.point_ids), old_build.space_id

    class Invalid(FakeEmbedding):
        async def embed_texts(self, texts):
            vectors = await super().embed_texts(texts)
            if bad == "count":
                return vectors[:-1]
            if bad == "dimension":
                vectors[0] = vectors[0][:-1]
            else:
                vectors[0][0] = True if bad == "boolean" else float("nan")
            return vectors

    monkeypatch.setattr(ingest, "get_embedding_client", lambda **kw: Invalid(dim=16))
    ok, err = await ingest.index_chunks(db, doc)
    assert ok is False and err
    current = await db.get(Document, doc_id, populate_existing=True)
    assert current.active_build_id == old_pointer and current.vectorized is True
    assert [row.id for row in await current_chunks(db, doc_id)] == old_chunk_ids
    assert {hit.id for hit in store.for_space(old_space).get_by_ids(old_points)} == set(old_points)
    failed = (await db.scalars(select(IndexBuild).where(
        IndexBuild.document_id == doc_id, IndexBuild.state == "failed"
    ))).one()
    assert failed.id != old_pointer and failed.point_ids
    assert store.for_space(failed.space_id).get_by_ids(failed.point_ids) == []


# --------------------------------------------------------------------------
# c. 无 chunk → (False, ...)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_chunks_no_chunks(db, store, _vector_deps):
    doc = Document(
        filename="空.md",
        file_type="md",
        status="ready",
        index_status="ready",
    )
    db.add(doc)
    await db.flush()

    ok, err = await ingest.index_chunks(db, doc)
    assert ok is False
    assert err and "没有分块" in err


# --------------------------------------------------------------------------
# d. purge_document：Qdrant 点消失 + MySQL 行消失
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_document_cascades_vectors(db, store, _vector_deps):
    doc, chunks = await _make_doc_with_chunks(db, 2)
    ok, _ = await ingest.index_chunks(db, doc)
    assert ok is True

    doc_id = doc.id
    build = await db.get(IndexBuild, doc.active_build_id)
    ids = list(build.point_ids)
    scoped = store.for_space(build.space_id)
    assert len(scoped.get_by_ids(ids)) == 2

    await ingest.purge_document(db, doc_id)

    assert scoped.get_by_ids(ids) == [], "向量点必须被级联删除"
    docs = (
        await db.execute(select(Document).where(Document.id == doc_id))
    ).scalars().all()
    assert docs == []
    rows = (
        await db.execute(select(Chunk).where(Chunk.document_id == doc_id))
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_purge_vector_failure_still_deletes_mysql(db, store, _vector_deps, monkeypatch):
    """向量删除失败只 warning，不挡 MySQL 硬删（Qdrant 不可用时文档不能卡死）。"""
    doc, _ = await _make_doc_with_chunks(db, 2)
    assert (await ingest.index_chunks(db, doc)) == (True, None)
    doc_id = doc.id
    build = await db.get(IndexBuild, doc.active_build_id)
    point_ids, space = list(build.point_ids), build.space_id
    scoped = store.for_space(space)
    calls = []

    def boom_delete(ids):
        calls.append(list(ids))
        raise RuntimeError("Qdrant 不可用")

    monkeypatch.setattr(scoped, "delete", boom_delete)
    monkeypatch.setattr(store, "for_space", lambda actual: scoped if actual == space else None)

    # 不抛
    await ingest.purge_document(db, doc_id)
    assert calls == [point_ids], "必须确实进入该构建空间的向量删除失败分支"

    docs = (
        await db.execute(select(Document).where(Document.id == doc_id))
    ).scalars().all()
    assert docs == []
    rows = (
        await db.execute(select(Chunk).where(Chunk.document_id == doc_id))
    ).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# 阶段 5 · 条目 5.3 真机抽查抓到的缺陷 回归修复：embedding 请求必须分批
# ---------------------------------------------------------------------------
def _stub_embedding_client(monkeypatch, *, status: int = 200, body_text: str = "ok"):
    """把 `OpenAICompatEmbedding` 用的 httpx 客户端换成记录批次的假实现。"""
    from app.core.config import settings as app_settings
    from app.vector import embed as embed_mod

    calls: list[int] = []

    class _Resp:
        def __init__(self, size: int) -> None:
            self.status_code = status
            self._size = size

        def json(self):
            return {"data": [{"index": i, "embedding": [0.5] * 8} for i in range(self._size)]}

        @property
        def text(self) -> str:
            return body_text

    class _Client:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, _url, headers=None, json=None):  # noqa: A002 —— 对齐 httpx 签名
            size = len(json["input"])
            calls.append(size)
            return _Resp(size)

    fake_cfg = SimpleNamespace(model="m", base_url="http://embed.test/v1", api_key="k")
    monkeypatch.setattr(type(app_settings), "embed_config", property(lambda self: fake_cfg))
    monkeypatch.setattr(embed_mod.httpx, "AsyncClient", _Client)
    return calls


@pytest.mark.asyncio
async def test_openai_compat_embedding_splits_requests_by_batch(monkeypatch):
    """97 块必须拆成多批（每批 ≤ `_MAX_EMBED_BATCH`），而不是一次全塞进去。

    实测依据（`回归修复`）：DashScope 兼容端点单次上限 20 条，超限回
    `<400> InvalidParameter: Value error, batch size is invalid, it should not be larger than 20.`
    ——不切分批时，**任何 >20 块的真实文档都无法向量化**（真机抽查：16 页论文 = 97 块全失败）。
    """
    calls = _stub_embedding_client(monkeypatch)

    vectors = await OpenAICompatEmbedding(dim=8).embed_texts([f"第 {i} 块" for i in range(97)])

    assert len(vectors) == 97, "分批后必须按原顺序拼回，条数与输入一致"
    assert calls == [16, 16, 16, 16, 16, 16, 1], f"批次切分不符：{calls}"
    # 对**服务端实测上限**断言，不是对 `_MAX_EMBED_BATCH` 自己断言——后者是**自指弱断言**：
    # 把常量改成 10**6 它照样成立（独立核验 2026-09-18 实测：cap→10⁶ 时只有上一行的硬编码清单会红）。
    assert max(calls) <= 20, "每批必须低于服务端实测上限 20（原文见上方 docstring）"


@pytest.mark.asyncio
async def test_embed_batches_respect_server_limit_for_unlisted_split(monkeypatch):
    """上限语义要有**独立**守卫：本条**不写死**切分形状，只断言「每批 ≤ 服务端实测上限 20」。

    为什么另开一条（独立核验代理 2026-09-18 的变异提醒）：上一条的硬编码清单
    `[16,16,16,16,16,16,1]` 在「cap 改大」时**总是先红**，于是那句 `max(calls) <= 20` 被抢先、
    **没有独立杀伤力**。本条用「45 块 → 16+16+13」这种不被清单覆盖的形状，把上限语义单独钉住：
    把 `_MAX_EMBED_BATCH` 改成 `10**6` 时，本条会因 `max(calls) == 45 > 20` 自己变红。
    """
    calls = _stub_embedding_client(monkeypatch)

    vectors = await OpenAICompatEmbedding(dim=8).embed_texts([f"第 {i} 块" for i in range(45)])

    assert len(vectors) == 45
    assert sum(calls) == 45, f"分批后条数必须守恒：{calls}"
    assert len(calls) > 1, "45 块必须真的拆成多批，否则下面那句会退化成恒真"
    assert max(calls) <= 20, f"每批必须低于服务端实测上限 20，实测 {calls}"


@pytest.mark.asyncio
async def test_openai_compat_embedding_surfaces_server_error_body(monkeypatch):
    """服务端 4xx 的**响应体原文**必须带进错误信息——批量上限这类真因只在响应体里。

    这条是 `回归修复` 的排障教训：原实现只报 `Client error '400 Bad Request'`，
    逼得排障者从「量批量」重新开始。
    """
    hint = "batch size is invalid, it should not be larger than 20."
    _stub_embedding_client(monkeypatch, status=400, body_text=hint)

    with pytest.raises(RuntimeError) as excinfo:
        await OpenAICompatEmbedding().embed_texts(["一段正文"])

    message = str(excinfo.value)
    assert hint in message, "必须把服务端原文带出来"
    assert message.startswith("embedding 调用失败"), "保留既有前缀，便于上层文案拼接"


# ---------------------------------------------------------------------------
# 阶段 5 · 对抗性复核 F8：embed 客户端的两处边角（只重试该重试的、按 index 归位）
# ---------------------------------------------------------------------------
def _stub_embedding_rows(monkeypatch, rows: list[dict], *, status: int = 200):
    """返回 `call_log`（每次请求的入参条数），响应体由调用方给定（可带 index / 乱序）。"""
    from app.core.config import settings as app_settings
    from app.vector import embed as embed_mod

    calls: list[int] = []

    class _Resp:
        status_code = status
        text = "stub"

        @staticmethod
        def json():
            return {"data": rows}

    class _Client:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, _url, headers=None, json=None):  # noqa: A002
            calls.append(len(json["input"]))
            return _Resp()

    fake_cfg = SimpleNamespace(model="m", base_url="http://embed.test/v1", api_key="k")
    monkeypatch.setattr(type(app_settings), "embed_config", property(lambda self: fake_cfg))
    monkeypatch.setattr(embed_mod.httpx, "AsyncClient", _Client)
    return calls


@pytest.mark.asyncio
async def test_openai_compat_embedding_orders_by_server_index(monkeypatch):
    """服务端乱序返回时必须按 `index` 归位——错位的向量不报错，只会让检索悄悄变差（F8）。"""
    rows = [
        {"index": 3, "embedding": [3.0]},
        {"index": 2, "embedding": [2.0]},
        {"index": 1, "embedding": [1.0]},
        {"index": 0, "embedding": [0.0]},
    ]
    _stub_embedding_rows(monkeypatch, rows)

    vectors = await OpenAICompatEmbedding(dim=1).embed_texts(["a", "b", "c", "d"])

    assert [v[0] for v in vectors] == [0.0, 1.0, 2.0, 3.0], f"未按 index 归位：{vectors}"


@pytest.mark.asyncio
async def test_openai_compat_embedding_does_not_retry_4xx(monkeypatch):
    """4xx 是确定性错误：只打一次接口就该失败（旧实现会白重试一次）。"""
    calls = _stub_embedding_client(monkeypatch, status=400, body_text="bad request")

    with pytest.raises(RuntimeError):
        await OpenAICompatEmbedding().embed_texts(["一段正文"])

    assert len(calls) == 1, f"4xx 被重试了 {len(calls)} 次（F8）"
