"""U1.5：保真正文身份、旧版显式转换与短摘要留档冲突。"""

from __future__ import annotations

import hashlib
import re

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import DuplicateDocument
from app.models import Document
from app.pipeline import ingest, reindex, storage
from app.vector.embed import FakeEmbedding
from app.vector.qdrant_store import QdrantVectorStore


def test_semantic_whitespace_belongs_to_body_identity():
    assert ingest.content_hash("s='a b'") != ingest.content_hash("s='ab'")
    assert ingest.content_hash("x\t y") != ingest.content_hash("x y")
    assert ingest.content_hash(" ") != ingest.content_hash("")
    assert ingest.content_hash("\n") != ingest.content_hash(" ")
    assert ingest.content_hash("s='a b'") == ingest.content_hash("s='a b'")


def test_body_identity_does_not_include_overlap(monkeypatch):
    raw = "".join(f"{i:02d}xyz" for i in range(30))
    prepared = []
    for size, overlap in ((32, 8), (64, 8), (64, 0)):
        monkeypatch.setattr(settings, "RAG_CHUNK_SIZE", size)
        monkeypatch.setattr(settings, "RAG_CHUNK_OVERLAP", overlap)
        prepared.append(ingest.prepare_document("same.txt", raw.encode("utf-8")))
    assert len({len(value.pieces) for value in prepared}) > 1
    assert len({value.content_hash for value in prepared}) == 1
    assert all(value.content_hash == hashlib.sha256(value.text.encode("utf-8")).hexdigest()
               for value in prepared)
    assert all(value.source_sha256 == hashlib.sha256(raw.encode("utf-8")).hexdigest()
               for value in prepared)


def test_identity_version_alone_requires_explicit_reclean():
    assert reindex._needs_reclean({"rule_fingerprint": "same"}, "same")
    assert reindex._needs_reclean({
        "rule_fingerprint": "same", "content_identity": {"version": "old"},
    }, "same")
    assert not reindex._needs_reclean({
        "rule_fingerprint": "same",
        "content_identity": {"version": ingest.CONTENT_HASH_VERSION},
    }, "same")


def test_file_claim_race_does_not_overwrite_other_bytes(tmp_path, monkeypatch):
    publish = "rename" if storage.os.name == "nt" else "link"
    original = getattr(storage.os, publish)
    occupied = []

    def claim_before_publish(src, dst):
        if not occupied:
            dst.write_bytes(b"concurrent different source")
            occupied.append(dst)
        return original(src, dst)

    monkeypatch.setattr(storage.os, publish, claim_before_publish)
    saved = storage.save_upload("same.txt", b"current source", upload_dir=tmp_path)
    assert saved != occupied[0]
    assert occupied[0].read_bytes() == b"concurrent different source"
    assert saved.read_bytes() == b"current source"
    assert not list(tmp_path.glob(".tmp-*"))


@pytest.mark.parametrize("filename", ["same.txt", "a" * 180 + ".markdown"])
def test_short_digest_conflict_retains_both_files(tmp_path, filename):
    data = b"right source"
    digest = hashlib.sha256(data).hexdigest()
    occupied = tmp_path / f"{digest[:8]}-{storage._safe_name(filename)}"
    occupied.write_bytes(b"different source occupying the short name")

    saved = storage.save_upload(filename, data, upload_dir=tmp_path)
    assert saved != occupied
    assert saved.name.startswith(digest + "-")
    assert len(saved.name) <= 209
    assert occupied.read_bytes() == b"different source occupying the short name"
    assert saved.read_bytes() == data
    assert storage.save_upload(filename, data, upload_dir=tmp_path) == saved
    assert len(list(tmp_path.iterdir())) == 2


@pytest.fixture
def isolated_upload(monkeypatch, tmp_path):
    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path))
    embedding = FakeEmbedding(dim=4)
    store = QdrantVectorStore(path=str(tmp_path / "vectors"))
    monkeypatch.setattr(ingest, "get_embedding_client", lambda **kw: embedding)
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: store)
    try:
        yield tmp_path
    finally:
        store.close()


@pytest.mark.asyncio
async def test_legacy_raw_match_blocks_copy_and_explicit_retry_converts(
    db, isolated_upload
):
    body = "旧资料\n\n金额 1000000000 元；正文必须保留空白。\n"
    data = body.encode("utf-8")
    stored = isolated_upload / "old.txt"
    stored.write_bytes(data)
    # 旧摘要确实来自去空白文本，且模拟旧 overlap 重复参与摘要。
    old_hash = hashlib.sha256(re.sub(r"\s+", "", body + body).encode("utf-8")).hexdigest()
    old = Document(
        filename="old.txt", file_type="txt", status="ready", index_status="ready",
        raw_content=body, content_hash=old_hash, file_path=str(stored),
        clean_report={"rule_fingerprint": "legacy"},
    )
    db.add(old)
    await db.commit()
    old_id = old.id

    with pytest.raises(DuplicateDocument, match="重建索引") as error:
        await ingest.ingest_document(db, "renamed.txt", data)
    assert error.value.document_id == old_id
    rows = (await db.execute(select(Document))).scalars().all()
    assert len(rows) == 1 and rows[0].content_hash == old_hash
    assert "content_identity" not in rows[0].clean_report

    converted = await ingest.retry_document(db, old_id)
    assert converted.document_id == old_id
    fresh = await db.get(Document, old_id)
    prepared = ingest.prepare_document("old.txt", data)
    assert fresh.content_hash == prepared.content_hash != old_hash
    assert fresh.clean_report["content_identity"] == {
        "version": ingest.CONTENT_HASH_VERSION,
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "body_sha256": prepared.content_hash,
    }
    assert len((await db.execute(select(Document))).scalars().all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("overwrite", [False, True])
async def test_ambiguous_legacy_hash_cannot_discard_different_body(
    db, isolated_upload, overwrite
):
    old_body, new_body = "s='a b'", "s='ab'"
    old = Document(
        filename="old.py", file_type="py", status="ready", index_status="ready",
        raw_content=old_body, content_hash=hashlib.sha256(new_body.encode()).hexdigest(),
    )
    db.add(old)
    await db.commit()
    old_id = old.id

    with pytest.raises(DuplicateDocument, match="无法确认正文身份") as error:
        await ingest.ingest_document(db, "new.py", new_body.encode(), overwrite=overwrite)
    assert error.value.document_id == old_id
    rows = (await db.execute(select(Document))).scalars().all()
    assert len(rows) == 1 and rows[0].raw_content == old_body
    assert rows[0].index_status == "ready"


@pytest.mark.asyncio
async def test_unrelated_legacy_without_raw_does_not_block_upload(db, isolated_upload):
    old = Document(
        filename="unrelated.txt", file_type="txt", status="ready", index_status="ready",
        content_hash=hashlib.sha256(b"unrelated legacy").hexdigest(),
    )
    db.add(old)
    await db.commit()
    old_id = old.id
    result = await ingest.ingest_document(db, "new.txt", b"an unrelated new document")
    assert result.document_id != old_id
    assert len((await db.execute(select(Document))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_failed_write_keeps_version_and_renamed_retry_reuses_row(
    db, isolated_upload, monkeypatch
):
    data = "失败后的正文身份 a b。\n".encode("utf-8")
    original = ingest._persist_chunks

    async def fail_write(session, doc, prepared):
        raise RuntimeError("模拟写块失败")

    monkeypatch.setattr(ingest, "_persist_chunks", fail_write)
    with pytest.raises(RuntimeError, match="模拟写块失败"):
        await ingest.ingest_document(db, "failed.txt", data)
    failed = (await db.execute(select(Document))).scalar_one()
    failed_id = failed.id
    assert failed.index_status == "failed"
    assert failed.clean_report["content_identity"]["version"] == ingest.CONTENT_HASH_VERSION
    assert failed.clean_report["content_identity"]["source_sha256"] == hashlib.sha256(data).hexdigest()

    monkeypatch.setattr(ingest, "_persist_chunks", original)
    result = await ingest.ingest_document(db, "renamed.txt", data)
    assert result.document_id == failed_id
    rows = (await db.execute(select(Document))).scalars().all()
    assert len(rows) == 1 and rows[0].index_status == "ready"
