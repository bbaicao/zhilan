"""OPT3：解析拒绝不动旧构建，修正源文件后可重试并留档。"""

import pytest
from pathlib import Path
from sqlalchemy import select

from app.core.errors import DocumentParseError
from app.models import Chunk, Document, IndexBuild
from app.pipeline import ingest
from app.pipeline.builds import current_chunks, document_snapshot, processing_fingerprint
from tests.pipeline.test_reindex_u2 import wire  # noqa: F401


@pytest.mark.asyncio
async def test_parse_rejection_keeps_old_then_corrected_source_can_retry(db, wire):
    initial = await ingest.ingest_document(db, "retry.txt", b"oldsignal original body")
    doc_id = initial.document_id
    doc = await db.get(Document, doc_id)
    old_pointer, old_snapshot = doc.active_build_id, document_snapshot(doc)
    old_chunk_ids = [row.id for row in await current_chunks(db, doc_id)]
    old_build = await db.get(IndexBuild, old_pointer)
    old_space, old_points = old_build.space_id, list(old_build.point_ids)
    with pytest.raises(DocumentParseError):
        await ingest.ingest_document(db, "retry.txt", b"GIF89a disguised image", overwrite=True)
    doc = await db.get(Document, doc_id, populate_existing=True)
    assert doc.active_build_id == old_pointer and document_snapshot(doc) == old_snapshot
    assert [row.id for row in await current_chunks(db, doc_id)] == old_chunk_ids
    assert {point.id for point in wire.store.for_space(old_space).get_by_ids(old_points)} == set(old_points)
    failed = await db.scalar(select(Document).where(
        Document.filename == "retry.txt", Document.index_status == "failed"
    ))
    assert failed is not None
    failed_id = failed.id
    Path(failed.file_path).write_bytes("newsignal 修正后正文".encode("utf-16"))
    corrected = await ingest.retry_document(db, failed_id)
    doc = await db.get(Document, corrected.document_id, populate_existing=True)
    assert corrected.document_id == failed_id and corrected.vectorized is True
    assert doc.raw_content == "newsignal 修正后正文" and doc.active_build_id != old_pointer
    active = await db.get(IndexBuild, doc.active_build_id)
    assert active.snapshot["raw_content"] == doc.raw_content
    assert active.snapshot.get("source_archive_path")
    assert active.fingerprint["processing"] == processing_fingerprint()
    assert processing_fingerprint()["parse"] != "u1-pages-v1"
    original = await db.get(Document, doc_id, populate_existing=True)
    assert original.active_build_id == old_pointer and document_snapshot(original) == old_snapshot


@pytest.mark.asyncio
async def test_ready_retry_parse_failure_keeps_old_then_publishes_corrected(db, wire):
    initial = await ingest.ingest_document(db, "ready-retry.txt", b"oldsignal original")
    doc_id = initial.document_id
    doc = await db.get(Document, doc_id)
    old_pointer, old_snapshot = doc.active_build_id, document_snapshot(doc)
    old_chunk_ids = [row.id for row in await current_chunks(db, doc_id)]
    old = await db.get(IndexBuild, old_pointer)
    old_space, old_points = old.space_id, list(old.point_ids)
    source = Path(doc.file_path)
    source.write_bytes(b"GIF89a renamed binary")
    with pytest.raises(DocumentParseError):
        await ingest.retry_document(db, doc_id)
    doc = await db.get(Document, doc_id, populate_existing=True)
    assert doc.index_status == "ready" and doc.active_build_id == old_pointer
    assert document_snapshot(doc) == old_snapshot
    assert [row.id for row in await current_chunks(db, doc_id)] == old_chunk_ids
    assert {point.id for point in wire.store.for_space(old_space).get_by_ids(old_points)} == set(old_points)
    source.write_bytes("newsignal 原位重试正文".encode("utf-32"))
    corrected = await ingest.retry_document(db, doc_id)
    doc = await db.get(Document, doc_id, populate_existing=True)
    assert corrected.document_id == doc_id and corrected.vectorized is True
    assert doc.index_status == "ready" and doc.active_build_id != old_pointer
    assert doc.raw_content == "newsignal 原位重试正文"
    for chunk_id in old_chunk_ids:
        assert await db.get(Chunk, chunk_id) is not None
