"""U2：从真实入库/重建入口验证失败保旧、按指纹重切块与历史空间清理。"""

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import ModelConfig, settings
from app.core.errors import RagError
from app.models import Chat, Chunk, Document, IndexBuild, Message, MessageAttribution
from app.pipeline import ingest, reindex
from app.pipeline.builds import current_chunks, document_snapshot, rollback_build
from app.retrieval.keyword import search_keywords
from app.vector import get_embedding_client
from app.vector import embed as embed_module
from app.vector.base import VectorPoint
from app.vector.embed import FakeEmbedding, OpenAICompatEmbedding
from app.vector.qdrant_store import QdrantVectorStore


@pytest.fixture
def wire(monkeypatch, tmp_path):
    store = QdrantVectorStore(path=str(tmp_path / "vectors"))
    dependencies = SimpleNamespace(store=store, embedding=FakeEmbedding(dim=8))
    monkeypatch.setattr(ingest, "get_embedding_client", lambda **kw: dependencies.embedding)
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: store)
    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path / "uploads"))
    monkeypatch.setattr(settings, "RAG_CHUNK_SIZE", 320)
    monkeypatch.setattr(settings, "RAG_CHUNK_OVERLAP", 32)
    try:
        yield dependencies
    finally:
        store.close()


def long_body():
    return "\n\n".join(
        f"section{i} 这是第 {i} 段独立正文，记录实际索引重建的来源位置和正文身份。"
        f"本段的编号是 {i}，参数变化只需要重新切块，旧来源仍然能够查询和回滚。"
        for i in range(30)
    ).encode("utf-8")


@pytest.mark.asyncio
async def test_reindex_last_embedding_batch_failure_counts_failure_and_keeps_old(
    db, wire, monkeypatch
):
    monkeypatch.setattr(settings, "RAG_CHUNK_SIZE", 64)
    monkeypatch.setattr(settings, "RAG_CHUNK_OVERLAP", 0)
    result = await ingest.ingest_document(db, "many-blocks.txt", long_body())
    doc_id = result.document_id
    doc = await db.get(Document, doc_id, populate_existing=True)
    old_pointer, old_snapshot = doc.active_build_id, document_snapshot(doc)
    old_rows = await current_chunks(db, doc_id)
    old_ids = [row.id for row in old_rows]
    assert len(old_ids) > 16
    old_build = await db.get(IndexBuild, old_pointer)
    old_points, old_space = list(old_build.point_ids), old_build.space_id
    calls = []

    async def respond(request):
        inputs = json.loads(request.content)["input"]
        calls.append(inputs)
        return httpx.Response(200, json={
            "data": [
                {"index": i, "embedding": [0.1] * 8} for i in range(len(inputs))
            ] if len(calls) == 1 else []
        })

    client_factory = httpx.AsyncClient
    monkeypatch.setattr(
        embed_module.httpx, "AsyncClient",
        lambda *args, **kwargs: client_factory(
            *args, **kwargs, transport=httpx.MockTransport(respond)
        ),
    )
    monkeypatch.setattr(embed_module, "_record_embed_usage", lambda *args: None)
    wire.embedding = OpenAICompatEmbedding(dim=8, config=ModelConfig(
        kind="embed", provider="offline", model="new-embedding",
        base_url="https://embedding.invalid/v1", api_key="offline-test-placeholder",
    ))

    def forbidden_parse(*args, **kwargs):
        raise AssertionError("model-only reindex must reuse the persisted body")

    monkeypatch.setattr(ingest, "parse_document_pages", forbidden_parse)
    rebuilt = await reindex.reindex_documents(db, document_id=doc_id)
    assert len(calls) == 2 and len(calls[0]) == 16
    assert (rebuilt.documents, rebuilt.recleaned, rebuilt.vectors, rebuilt.failed) == (0, 0, 0, 1)
    doc = await db.get(Document, doc_id, populate_existing=True)
    assert doc.active_build_id == old_pointer and doc.vectorized is True
    assert document_snapshot(doc) == old_snapshot
    assert [row.id for row in await current_chunks(db, doc_id)] == old_ids
    assert {hit.id for hit in wire.store.for_space(old_space).get_by_ids(old_points)} == set(old_points)


@pytest.mark.asyncio
async def test_reindex_chunk_parameter_change_reuses_body_without_parse(db, wire, monkeypatch):
    result = await ingest.ingest_document(db, "rechunk.txt", long_body())
    doc_id = result.document_id
    doc = await db.get(Document, doc_id, populate_existing=True)
    old_pointer = doc.active_build_id
    old_raw, old_hash = doc.raw_content, doc.content_hash
    old_identity = dict(doc.clean_report["content_identity"])
    old_ids = [row.id for row in await current_chunks(db, doc_id)]
    monkeypatch.setattr(settings, "RAG_CHUNK_SIZE", 80)
    monkeypatch.setattr(settings, "RAG_CHUNK_OVERLAP", 8)

    def forbidden_parse(*args, **kwargs):
        raise AssertionError("rechunk must use canonical text and source_map without parsing")

    monkeypatch.setattr(ingest, "parse_document_pages", forbidden_parse)
    rebuilt = await reindex.reindex_documents(db, document_id=doc_id)
    doc = await db.get(Document, doc_id, populate_existing=True)
    rows = await current_chunks(db, doc_id)
    assert (rebuilt.documents, rebuilt.recleaned, rebuilt.failed) == (1, 0, 0)
    assert rebuilt.vectors == len(rows)
    assert doc.active_build_id != old_pointer
    assert len(rows) > len(old_ids)
    assert set(row.id for row in rows).isdisjoint(old_ids)
    assert (doc.raw_content, doc.content_hash) == (old_raw, old_hash)
    assert doc.clean_report["content_identity"] == old_identity
    assert all([(await db.get(Chunk, chunk_id)) is not None for chunk_id in old_ids])


@pytest.mark.asyncio
async def test_first_ingest_vector_failure_still_publishes_keyword_body(db, wire):
    class Unavailable(FakeEmbedding):
        async def embed_texts(self, texts):
            raise RuntimeError("offline embedding unavailable")

    wire.embedding = Unavailable(dim=8)
    result = await ingest.ingest_document(
        db, "keyword-only.txt", "fallbacksignal 首次模型失败时仍保留完整关键词正文。".encode("utf-8")
    )
    doc = await db.get(Document, result.document_id, populate_existing=True)
    assert result.vectorized is False
    assert doc.index_status == doc.status == "ready" and doc.vectorized is False
    assert "unavailable" in doc.vector_error
    assert doc.active_build_id is not None
    active = await db.get(IndexBuild, doc.active_build_id)
    assert active.state == "published" and active.vectorized is False
    assert await search_keywords(db, "fallbacksignal")
    assert await current_chunks(db, doc.id)


@pytest.mark.asyncio
async def test_overwrite_failure_and_success_keep_document_id_and_old_reference(db, wire):
    data = "samebody 历史引用需要保留，覆盖重建在同一文档身份上切换版本。".encode("utf-8")
    initial = await ingest.ingest_document(db, "original.txt", data)
    doc_id = initial.document_id
    doc = await db.get(Document, doc_id, populate_existing=True)
    old_pointer, old_snapshot = doc.active_build_id, document_snapshot(doc)
    old_chunk_id = (await current_chunks(db, doc_id))[0].id
    chat = Chat(title="覆盖后保留引用")
    db.add(chat)
    await db.flush()
    message = Message(chat_id=chat.id, role="assistant", content="历史回答")
    db.add(message)
    await db.flush()
    attribution = MessageAttribution(
        message_id=message.id, chunk_id=old_chunk_id, ordinal=1, title="original.txt"
    )
    db.add(attribution)
    await db.commit()
    attribution_id = attribution.id

    class Unavailable(FakeEmbedding):
        async def embed_texts(self, texts):
            raise RuntimeError("overwrite embedding unavailable")

    wire.embedding = Unavailable(dim=8)
    with pytest.raises(RagError, match="unavailable"):
        await ingest.ingest_document(db, "renamed.txt", data, overwrite=True)
    doc = await db.get(Document, doc_id, populate_existing=True)
    assert doc.active_build_id == old_pointer
    assert document_snapshot(doc) == old_snapshot
    assert (await db.get(MessageAttribution, attribution_id)).chunk_id == old_chunk_id
    assert await db.scalar(select(func.count()).select_from(Document)) == 1
    wire.embedding = FakeEmbedding(dim=8)
    replaced = await ingest.ingest_document(db, "renamed.txt", data, overwrite=True)
    assert replaced.document_id == doc_id and replaced.vectorized is True
    doc = await db.get(Document, doc_id, populate_existing=True)
    assert doc.filename == "renamed.txt" and doc.active_build_id != old_pointer
    assert (await db.get(MessageAttribution, attribution_id)).chunk_id == old_chunk_id
    assert (await db.get(Chunk, old_chunk_id)).content
    assert old_chunk_id not in [row.id for row in await current_chunks(db, doc_id)]
    assert await db.scalar(select(func.count()).select_from(Document)) == 1


@pytest.mark.asyncio
async def test_purge_cleans_every_history_space_and_preserves_other_document(db, wire):
    first = await ingest.ingest_document(db, "purge.txt", b"purgesignal document with two model versions")
    second = await ingest.ingest_document(db, "keep.txt", b"keepsignal separate document must survive cleanup")
    doc = await db.get(Document, first.document_id, populate_existing=True)
    old = await db.get(IndexBuild, doc.active_build_id)
    old_points, old_space = list(old.point_ids), old.space_id
    kept_doc = await db.get(Document, second.document_id, populate_existing=True)
    kept = await db.get(IndexBuild, kept_doc.active_build_id)
    kept_points = list(kept.point_ids)

    class OtherModel(FakeEmbedding):
        def identity(self):
            return {**super().identity(), "model": "second-model"}

    wire.embedding = OtherModel(dim=8)
    ok, error = await ingest.index_chunks(db, doc)
    assert ok is True and error is None
    doc = await db.get(Document, first.document_id, populate_existing=True)
    current = await db.get(IndexBuild, doc.active_build_id)
    new_points, new_space = list(current.point_ids), current.space_id
    assert new_space != old_space
    assert wire.store.for_space(old_space).get_by_ids(old_points)
    assert wire.store.for_space(new_space).get_by_ids(new_points)
    await ingest.purge_document(db, first.document_id)
    assert await db.get(Document, first.document_id, populate_existing=True) is None
    assert not (await db.execute(
        select(IndexBuild.id).where(IndexBuild.document_id == first.document_id)
    )).scalars().all()
    assert not (await db.execute(
        select(Chunk.id).where(Chunk.document_id == first.document_id)
    )).scalars().all()
    assert not wire.store.for_space(old_space).get_by_ids(old_points)
    assert not wire.store.for_space(new_space).get_by_ids(new_points)
    assert {hit.id for hit in wire.store.for_space(old_space).get_by_ids(kept_points)} == set(kept_points)
    assert await current_chunks(db, second.document_id)


@pytest.mark.asyncio
async def test_unconfigured_production_embedding_first_ingest_uses_keyword_fallback(
    db, wire, monkeypatch
):
    monkeypatch.setattr(type(settings), "embed_config", property(lambda self: None))
    monkeypatch.setattr(settings, "EMBED_DIM", 8)
    monkeypatch.setattr(ingest, "get_embedding_client", get_embedding_client)

    def forbidden_client(*args, **kwargs):
        raise AssertionError("unconfigured embedding must not start an HTTP request")

    monkeypatch.setattr(embed_module.httpx, "AsyncClient", forbidden_client)
    result = await ingest.ingest_document(
        db, "unconfigured.txt", "unconfiguredsignal 未选择模型时首次资料仍可关键词检索。".encode("utf-8")
    )
    doc = await db.get(Document, result.document_id, populate_existing=True)
    assert result.vectorized is False
    assert doc.index_status == doc.status == "ready" and doc.vectorized is False
    assert "未配置 embed 模型" in doc.vector_error
    active = await db.get(IndexBuild, doc.active_build_id)
    assert active.state == "published" and active.vectorized is False
    assert active.fingerprint["embedding"]["model"] == ""
    assert await current_chunks(db, doc.id)
    assert await search_keywords(db, "unconfiguredsignal")


@pytest.mark.asyncio
async def test_legacy_null_body_snapshot_can_rollback_without_guessing_vector_model(db, wire):
    body = "legacysignal 历史正文，旧引用必须保留。\n" * 3
    path = settings.upload_path / "legacy.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    doc = Document(
        filename="legacy.txt", file_type="txt", file_path=str(path), kb_id=1,
        status="ready", index_status="ready", vectorized=True, chunk_count=1,
        raw_content=body, content_hash="d" * 64, category="旧分类", tags=["历史"],
        clean_report={"canonical_text": body, "legacy_note": "model identity was never recorded"},
    )
    db.add(doc)
    await db.flush()
    chunk = Chunk(
        document_id=doc.id, content=body, ordinal=0, start_offset=0, end_offset=len(body)
    )
    chat = Chat(title="历史 NULL 引用")
    db.add_all([chunk, chat])
    await db.flush()
    message = Message(chat_id=chat.id, role="assistant", content="引用旧资料")
    db.add(message)
    await db.flush()
    attribution = MessageAttribution(
        message_id=message.id, chunk_id=chunk.id, ordinal=1, title="legacy.txt", snippet=body
    )
    db.add(attribution)
    await db.commit()
    doc_id, old_chunk_id, attribution_id = doc.id, chunk.id, attribution.id
    # 同步快照只读已加载字段；新持久化对象的默认值在异步边界显式加载。
    await db.refresh(doc)
    old_snapshot = document_snapshot(doc)
    # 原数字点只有三维，未记录模型；新八维空间不能采用或重命名这些历史点。
    wire.store.ensure_collection(3)
    wire.store.upsert([VectorPoint(
        id=str(old_chunk_id), vector=[0.1, 0.2, 0.3],
        payload={"document_id": doc_id, "chunk_id": old_chunk_id},
    )])

    rebuilt = await reindex.reindex_documents(db, document_id=doc_id)
    assert (rebuilt.documents, rebuilt.recleaned, rebuilt.failed) == (1, 1, 0)
    doc = await db.get(Document, doc_id, populate_existing=True)
    new_ids = [row.id for row in await current_chunks(db, doc_id)]
    assert new_ids and old_chunk_id not in new_ids
    legacy = await db.scalar(select(IndexBuild).where(
        IndexBuild.document_id == doc_id, IndexBuild.space_id.is_(None)
    ))
    assert legacy is not None and legacy.state == "retired"
    assert legacy.content_build_id is None and legacy.vectorized is False
    assert legacy.fingerprint == {"legacy": True} and legacy.point_ids == []
    assert legacy.snapshot == old_snapshot
    assert (await db.get(MessageAttribution, attribution_id)).chunk_id == old_chunk_id
    assert (await db.get(Chunk, old_chunk_id)).build_id is None

    restored = await rollback_build(db, doc_id, legacy.id, store=wire.store)
    assert restored.content_build_id is None and restored.vectorized is False
    assert document_snapshot(restored) == old_snapshot
    assert [row.id for row in await current_chunks(db, doc_id)] == [old_chunk_id]
    hits = await search_keywords(db, "legacysignal")
    assert {hit.chunk_id for hit in hits} == {old_chunk_id}
    assert (await db.get(MessageAttribution, attribution_id)).snippet == body
    assert wire.store.get_by_ids([str(old_chunk_id)]), "未知模型的历史数字点仍保留原位"


@pytest.mark.asyncio
async def test_reindex_all_continues_after_first_document_rollback_failure(db, wire):
    first = await ingest.ingest_document(
        db, "fails-first.txt", b"firstsignal the first document must keep its old readable build"
    )
    second = await ingest.ingest_document(
        db, "succeeds-second.txt", b"secondsignal the second document must still be rebuilt"
    )
    first_id, second_id = first.document_id, second.document_id
    first_pointer = (await db.get(Document, first_id)).active_build_id
    second_pointer = (await db.get(Document, second_id)).active_build_id
    second_count = len(await current_chunks(db, second_id))
    assert first_id < second_id

    class SelectiveFailure(FakeEmbedding):
        async def embed_texts(self, texts):
            if any("firstsignal" in text for text in texts):
                raise RuntimeError("first-document embedding failure")
            return await super().embed_texts(texts)

    wire.embedding = SelectiveFailure(dim=8)
    result = await reindex.reindex_documents(db)
    assert (result.documents, result.failed, result.recleaned, result.vectors) == (
        1, 1, 0, second_count
    )
    assert any("fails-first.txt" in message and "first-document" in message
               for message in result.messages)
    assert (await db.get(Document, first_id, populate_existing=True)).active_build_id == first_pointer
    assert (await db.get(Document, second_id, populate_existing=True)).active_build_id != second_pointer
    assert await search_keywords(db, "firstsignal")
    assert await search_keywords(db, "secondsignal")
