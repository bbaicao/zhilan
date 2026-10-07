"""OPT3：同步快照禁止隐式 SQL；异步构建只加载缺失字段。"""

import pytest
from sqlalchemy import event, inspect

from app.core.errors import RagError
from app.models import Document
from app.pipeline.builds import document_snapshot, stage_build
from app.pipeline import ingest
from tests.pipeline.test_build_lifecycle_u2 import published, store  # noqa: F401


@pytest.mark.asyncio
async def test_snapshot_of_expired_document_fails_without_sql(db, engine, published):
    doc = await db.get(Document, published.doc_id)
    await db.rollback()
    statements = []
    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        with pytest.raises(RagError, match="加载"):
            document_snapshot(doc)
        assert statements == []
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


@pytest.mark.asyncio
async def test_stage_loads_expired_fields_and_preserves_unsaved_edit(db, published):
    doc = await db.get(Document, published.doc_id)
    await db.rollback()
    doc.category = "本轮未提交分类"
    build = await stage_build(db, doc, embedding=published.embedding)
    assert build.document_id == published.doc_id
    assert build.snapshot["raw_content"] == published.snapshot["raw_content"]
    assert build.snapshot["category"] == "本轮未提交分类"
    assert doc.category == "本轮未提交分类"
    assert not ({"raw_content", "category", "created_at", "index_status", "active_build_id"}
                & inspect(doc).expired_attributes)


def test_transient_snapshot_keeps_unset_none_and_deep_copies():
    doc = Document(filename="pending.txt", clean_report={"nested": ["原值"]})
    snapshot = document_snapshot(doc)
    assert snapshot["raw_content"] is None
    snapshot["clean_report"]["nested"].append("外部改动")
    assert doc.clean_report == {"nested": ["原值"]}


@pytest.mark.asyncio
async def test_build_entry_loads_rollback_expired_document(db, published, store, monkeypatch):
    doc = await db.get(Document, published.doc_id)
    await db.rollback()
    monkeypatch.setattr(ingest, "get_embedding_client", lambda: published.embedding)
    monkeypatch.setattr(ingest, "get_vector_store", lambda: store)
    ok, error = await ingest.index_chunks(db, doc)
    assert ok is True and error is None
    current = await db.get(Document, published.doc_id, populate_existing=True)
    assert current.raw_content == published.snapshot["raw_content"]
    assert current.active_build_id != published.old_id


@pytest.mark.asyncio
async def test_loading_only_deferred_date_keeps_all_loaded_edits(db, published):
    doc = await db.get(Document, published.doc_id)
    db.expire(doc, ["created_at"])
    doc.filename = "本轮未提交.txt"
    doc.raw_content = "本轮未提交正文"
    doc.clean_report = {"nested": ["本轮未提交"]}
    build = await stage_build(db, doc, embedding=published.embedding)
    assert build.snapshot["filename"] == "本轮未提交.txt"
    assert build.snapshot["raw_content"] == "本轮未提交正文"
    assert build.snapshot["clean_report"] == {"nested": ["本轮未提交"]}
    assert build.snapshot["created_at"] == published.snapshot["created_at"]
