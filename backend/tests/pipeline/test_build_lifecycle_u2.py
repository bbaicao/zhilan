"""U2：真实临时 SQL/Qdrant 上的发布故障、重启、取消与回滚判据。"""

import asyncio
import hashlib
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import vector as vector_module
from app.cache.store import current_generation
from app.core.errors import RagError
from app.models import Chat, Chunk, Document, IndexBuild, Message, MessageAttribution, WikiLink
from app.pipeline.builds import (
    current_chunks,
    document_snapshot,
    fail_build,
    publish_build,
    recover_build,
    rollback_build,
    stage_build,
    validate_build,
)
from app.pipeline.ingest import _upsert_points
from app.retrieval.keyword import search_keywords
from app.vector.embed import FakeEmbedding
from app.vector.qdrant_store import QdrantVectorStore


@pytest.fixture
def store(tmp_path):
    vector_store = QdrantVectorStore(path=tmp_path / "vectors")
    try:
        yield vector_store
    finally:
        vector_store.close()


@pytest_asyncio.fixture
async def published(db, store, tmp_path):
    raw = "oldsignal 原版正文\noldsecondary 原版第二块"
    source = tmp_path / "original.md"
    source.write_text(raw, encoding="utf-8")
    doc = Document(
        filename="original.md", file_type="md", file_path=str(source),
        status="ready", index_status="ready", chunk_count=2,
        raw_content=raw, content_hash=hashlib.sha256(raw.encode()).hexdigest(),
        clean_report={"canonical_text": raw, "rules": {"version": "old"}},
        source_encoding="utf-8", kb_id=1, category="原分类", tags=["原标签"],
        created_at=datetime(2026, 9, 1, 12, 0),
    )
    db.add(doc)
    await db.flush()
    rows = [
        Chunk(document_id=doc.id, ordinal=i, content=text)
        for i, text in enumerate(raw.splitlines())
    ]
    db.add_all(rows)
    await db.commit()
    embedding = FakeEmbedding(dim=8)
    build = await stage_build(db, doc, embedding=embedding)
    await validate_build(db, build.id, embedding=embedding, store=store, upsert=_upsert_points)
    doc = await publish_build(db, build.id)
    return SimpleNamespace(
        doc_id=doc.id, old_id=build.id, old_space=build.space_id,
        old_points=list(build.point_ids), old_chunk_ids=[row.id for row in rows],
        snapshot=document_snapshot(doc), embedding=embedding, source=source, tmp=tmp_path,
    )


async def candidate(db, published, *, count=2, label="newsignal", embedding=None):
    doc = await db.get(Document, published.doc_id, populate_existing=True)
    texts = [f"{label} 新版正文 {i}" for i in range(count)]
    raw = "\n".join(texts)
    source = published.tmp / f"{label}.md"
    source.write_text(raw, encoding="utf-8")
    snapshot = document_snapshot(doc)
    snapshot.update(
        filename=f"{label}.md", file_path=str(source), raw_content=raw,
        content_hash=hashlib.sha256(raw.encode()).hexdigest(),
        clean_report={"canonical_text": raw, "rules": {"version": label}},
        chunk_count=count, category="新分类", tags=["新标签"],
        created_at=datetime(2026, 10, 1, 14, 30),
    )
    return await stage_build(
        db, doc, embedding=embedding or published.embedding, snapshot=snapshot,
        chunks=[
            Chunk(document_id=doc.id, ordinal=i, content=text)
            for i, text in enumerate(texts)
        ],
    )


async def assert_old_readable(db, published, store):
    doc = await db.get(Document, published.doc_id, populate_existing=True)
    assert doc.active_build_id == published.old_id
    assert document_snapshot(doc) == published.snapshot
    assert [row.id for row in await current_chunks(db, doc.id)] == published.old_chunk_ids
    assert {hit.id for hit in store.for_space(published.old_space).get_by_ids(
        published.old_points
    )} == set(published.old_points)
    assert {row.chunk_id for row in await search_keywords(db, "oldsignal")} == {
        published.old_chunk_ids[0]
    }
    assert not await search_keywords(db, "newsignal")


@pytest.mark.asyncio
async def test_sql_publish_before_commit_failure_keeps_old(db, store, published, monkeypatch):
    build = await candidate(db, published)
    build_id, space, points = build.id, build.space_id, list(build.point_ids)
    await validate_build(db, build_id, embedding=published.embedding, store=store, upsert=_upsert_points)
    generation = current_generation()
    commit = db.commit
    failed = False

    async def commit_failure():
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("publish-before-commit")
        await commit()

    monkeypatch.setattr(db, "commit", commit_failure)
    with pytest.raises(RuntimeError, match="publish-before-commit"):
        await publish_build(db, build_id)
    await fail_build(db, build_id, "publish-before-commit", store=store)
    await assert_old_readable(db, published, store)
    assert not store.for_space(space).get_by_ids(points)
    assert (await db.get(IndexBuild, build_id, populate_existing=True)).state == "failed"
    assert current_generation() == generation


@pytest.mark.asyncio
async def test_sql_commit_succeeded_then_error_never_cleans_active(db, store, published, monkeypatch):
    build = await candidate(db, published)
    build_id, space, points = build.id, build.space_id, list(build.point_ids)
    await validate_build(db, build_id, embedding=published.embedding, store=store, upsert=_upsert_points)
    generation = current_generation()
    commit = db.commit
    failed = False

    async def committed_error():
        nonlocal failed
        await commit()
        if not failed:
            failed = True
            raise RuntimeError("publish-after-commit")

    monkeypatch.setattr(db, "commit", committed_error)
    with pytest.raises(RuntimeError, match="publish-after-commit"):
        await publish_build(db, build_id)
    await fail_build(db, build_id, "publish-after-commit", store=store)
    assert (await db.get(Document, published.doc_id)).active_build_id == build_id
    assert (await db.get(IndexBuild, build_id)).state == "published"
    assert {hit.id for hit in store.for_space(space).get_by_ids(points)} == set(points)
    assert current_generation() > generation


@pytest.mark.asyncio
@pytest.mark.parametrize("validated", [True, False])
async def test_restart_recovers_validated_or_withdraws_preparing(
    db, engine, store, published, validated
):
    build = await candidate(db, published)
    build_id, points, space = build.id, list(build.point_ids), build.space_id
    if validated:
        await validate_build(
            db, build_id, embedding=published.embedding, store=store, upsert=_upsert_points
        )
    await db.close()
    store.close()
    reopened = QdrantVectorStore(path=published.tmp / "vectors")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            state = await recover_build(
                session, build_id, store=reopened, embedding=published.embedding
            )
            doc = await session.get(Document, published.doc_id, populate_existing=True)
            assert state == ("published" if validated else "failed")
            assert doc.active_build_id == (build_id if validated else published.old_id)
            if validated:
                assert {hit.id for hit in reopened.for_space(space).get_by_ids(points)} == set(points)
                assert [row.build_id for row in await current_chunks(session, doc.id)] == [build_id] * 2
            else:
                await assert_old_readable(session, published, reopened)
                assert not reopened.for_space(space).get_by_ids(points)
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_same_base_concurrent_build_cas_and_cleanup_only_self(db, store, published):
    first = await candidate(db, published, label="newsignal")
    second = await candidate(db, published, label="siblingsignal")
    first_id, first_points, space = first.id, list(first.point_ids), first.space_id
    second_id, second_points = second.id, list(second.point_ids)
    assert first.base_build_id == second.base_build_id == published.old_id
    for build_id in (first_id, second_id):
        await validate_build(db, build_id, embedding=published.embedding, store=store, upsert=_upsert_points)
    await publish_build(db, first_id)
    with pytest.raises(RagError, match="另一轮"):
        await publish_build(db, second_id)
    await fail_build(db, second_id, "another build won", store=store)
    assert (await db.get(Document, published.doc_id)).active_build_id == first_id
    assert (await db.get(IndexBuild, second_id)).state == "conflict"
    scoped = store.for_space(space)
    assert not scoped.get_by_ids(second_points)
    assert {hit.id for hit in scoped.get_by_ids(first_points)} == set(first_points)
    assert {hit.id for hit in scoped.get_by_ids(published.old_points)} == set(published.old_points)
    assert scoped.count() == len(first_points) + len(published.old_points)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["late_embedding", "partial_embeddings", "partial_upsert", "missing_point", "payload_identity"]
)
async def test_prepare_faults_keep_old_and_clean_own_points(db, store, published, fault):
    class FaultEmbedding(FakeEmbedding):
        async def embed_texts(self, texts):
            vectors = await super().embed_texts(texts)
            if fault == "late_embedding":
                assert len(texts) > 16
                raise RuntimeError("last embedding batch failed")
            return vectors[:-1] if fault == "partial_embeddings" else vectors

    embedding = FaultEmbedding(dim=8)
    build = await candidate(db, published, count=17 if fault == "late_embedding" else 2,
                            embedding=embedding)
    build_id, space, points = build.id, build.space_id, list(build.point_ids)
    generation = current_generation()

    def faulty_upsert(scoped, rows, dim):
        if fault in ("partial_upsert", "missing_point"):
            _upsert_points(scoped, rows[:1], dim)
            if fault == "partial_upsert":
                raise RuntimeError("Qdrant stopped after first point")
            return
        _upsert_points(scoped, rows, dim)
        if fault == "payload_identity":
            scoped.set_payload({"build_id": "another-build"}, ids=[rows[0].id])

    with pytest.raises((RuntimeError, RagError)):
        await validate_build(db, build_id, embedding=embedding, store=store, upsert=faulty_upsert)
    await fail_build(db, build_id, fault, store=store)
    await assert_old_readable(db, published, store)
    assert not store.for_space(space).get_by_ids(points)
    assert current_generation() == generation


@pytest.mark.asyncio
async def test_cancel_waits_for_vector_thread_then_cleans(db, store, published):
    build = await candidate(db, published)
    build_id, space, points = build.id, build.space_id, list(build.point_ids)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def paused_upsert(scoped, rows, dim):
        started.set()
        assert release.wait(2), "test must release the write thread"
        _upsert_points(scoped, rows, dim)
        finished.set()

    task = asyncio.create_task(
        validate_build(db, build_id, embedding=published.embedding, store=store, upsert=paused_upsert)
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cleanup cannot overtake the live write thread"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    await fail_build(db, build_id, "cancelled", state="cancelled", store=store)
    await assert_old_readable(db, published, store)
    assert (await db.get(IndexBuild, build_id)).state == "cancelled"
    assert not store.for_space(space).get_by_ids(points)


@pytest.mark.asyncio
async def test_publish_and_rollback_preserve_reference_and_source_snapshot(db, store, published):
    chat = Chat(title="保留历史引用")
    db.add(chat)
    await db.flush()
    message = Message(chat_id=chat.id, role="assistant", content="旧版答案")
    db.add(message)
    await db.flush()
    attribution = MessageAttribution(
        message_id=message.id, chunk_id=published.old_chunk_ids[0],
        ordinal=1, title="original.md", snippet="oldsignal 原版正文",
    )
    db.add(attribution)
    await db.commit()
    attribution_id = attribution.id
    old_source = published.source.read_bytes()
    build = await candidate(db, published)
    build_id = build.id
    new_snapshot = dict(build.snapshot)
    generation = current_generation()
    await validate_build(db, build_id, embedding=published.embedding, store=store, upsert=_upsert_points)
    assert current_generation() == generation
    await publish_build(db, build_id)
    assert current_generation() > generation
    doc = await db.get(Document, published.doc_id, populate_existing=True)
    assert document_snapshot(doc) == new_snapshot
    assert not await search_keywords(db, "oldsignal")
    assert len(await search_keywords(db, "newsignal")) == 2
    assert (await db.get(MessageAttribution, attribution_id)).chunk_id == published.old_chunk_ids[0]
    assert (await db.get(Chunk, published.old_chunk_ids[0])).content == "oldsignal 原版正文"
    generation = current_generation()
    restored = await rollback_build(db, published.doc_id, published.old_id, store=store)
    assert document_snapshot(restored) == published.snapshot
    assert current_generation() > generation
    await assert_old_readable(db, published, store)
    assert (await db.get(MessageAttribution, attribution_id)).chunk_id == published.old_chunk_ids[0]
    assert published.source.read_bytes() == old_source
    assert (published.tmp / "newsignal.md").exists()


@pytest.mark.asyncio
async def test_model_only_rebuild_reuses_body_and_isolates_same_dim_model(db, store, published):
    class OtherModel(FakeEmbedding):
        def identity(self):
            return {**super().identity(), "model": "other-model-same-dim"}

    embedding = OtherModel(dim=8)
    doc = await db.get(Document, published.doc_id, populate_existing=True)
    build = await stage_build(db, doc, embedding=embedding)
    build_id, space, points = build.id, build.space_id, list(build.point_ids)
    old_build = await db.get(IndexBuild, published.old_id)
    assert build.content_build_id == old_build.content_build_id
    assert build.fingerprint["processing"] == old_build.fingerprint["processing"]
    assert build.space_id != published.old_space
    await validate_build(db, build_id, embedding=embedding, store=store, upsert=_upsert_points)
    await publish_build(db, build_id)
    doc = await db.get(Document, published.doc_id, populate_existing=True)
    assert document_snapshot(doc) == published.snapshot
    assert [row.id for row in await current_chunks(db, doc.id)] == published.old_chunk_ids
    assert {hit.id for hit in store.for_space(space).get_by_ids(points)} == set(points)
    assert {hit.id for hit in store.for_space(published.old_space).get_by_ids(
        published.old_points
    )} == set(published.old_points)
    await rollback_build(db, published.doc_id, published.old_id, store=store)
    await assert_old_readable(db, published, store)


@pytest.mark.asyncio
async def test_validated_recovery_rejects_current_same_dim_different_model(
    db, store, published, monkeypatch
):
    build = await candidate(db, published)
    build_id, space, points = build.id, build.space_id, list(build.point_ids)
    await validate_build(
        db, build_id, embedding=published.embedding, store=store, upsert=_upsert_points
    )

    class ChangedModel(FakeEmbedding):
        def identity(self):
            return {**super().identity(), "model": "changed-after-validation"}

    changed = ChangedModel(dim=8)
    assert changed.dim == published.embedding.dim
    monkeypatch.setattr(vector_module, "get_embedding_client", lambda **kw: changed)
    assert await recover_build(db, build_id, store=store) == "failed"
    assert (await db.get(IndexBuild, build_id, populate_existing=True)).state == "failed"
    assert not store.for_space(space).get_by_ids(points)
    await assert_old_readable(db, published, store)


@pytest.mark.asyncio
async def test_recover_and_rollback_refresh_links_from_current_canonical_body(db, store, published):
    targets = [
        Document(filename=f"{title}.md", file_type="md", kb_id=1,
                 status="ready", index_status="ready")
        for title in ("OldTarget", "NewTarget")
    ]
    db.add_all(targets)
    await db.commit()
    old_target_id, new_target_id = [target.id for target in targets]

    async def linked_target():
        rows = list((await db.execute(select(WikiLink).where(
            WikiLink.source_type == "document", WikiLink.source_id == published.doc_id
        ))).scalars().all())
        assert len(rows) == 1 and rows[0].resolved is True
        return rows[0].target_title, rows[0].target_document_id

    old = await candidate(db, published, label="oldsignal [[OldTarget]]")
    old_id = old.id
    await validate_build(
        db, old_id, embedding=published.embedding, store=store, upsert=_upsert_points
    )
    await publish_build(db, old_id, embedding=published.embedding)
    assert await linked_target() == ("OldTarget", old_target_id)

    new = await candidate(db, published, label="newsignal [[NewTarget]]")
    new_id = new.id
    await validate_build(
        db, new_id, embedding=published.embedding, store=store, upsert=_upsert_points
    )
    assert await linked_target() == ("OldTarget", old_target_id), "准备未发布不应改变正文关系"
    assert await recover_build(
        db, new_id, store=store, embedding=published.embedding
    ) == "published"
    assert await linked_target() == ("NewTarget", new_target_id)
    await rollback_build(db, published.doc_id, old_id, store=store)
    assert await linked_target() == ("OldTarget", old_target_id)
