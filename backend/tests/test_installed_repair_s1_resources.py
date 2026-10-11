"""S1-R1: short structured windows and unnecessary large-body reloads."""

import pytest
from sqlalchemy import event, update

from app.core.errors import RagError
from app.models import Document, IndexBuild
from app.pipeline import builds, ingest
from app.pipeline.chunking import _iter_document_splits, _iter_splits
from tests.pipeline.test_build_lifecycle_u2 import candidate, published, store  # noqa: F401


@pytest.mark.parametrize("structure", [
    "```python\n" + "    value = 1\n" * 100 + "```\n",
    "| name | value |\n| --- | --- |\n" + "| item | data |\n" * 100,
    "    " + "indented body " * 100 + "\n",
])
def test_short_ordinary_preamble_advances_without_suffix_repetition(structure):
    preamble = "intro-" + "x" * 68 + "\n"
    text = preamble + ("following ordinary body " * 80) + "\n" + structure
    windows = list(_iter_document_splits(text, 800, 100))
    assert sum(end == len(preamble) for _, _, end in windows) == 1
    assert all(right[2] > left[2] for left, right in zip(windows, windows[1:])), [
        (start, end) for _, start, end in windows
    ]
    covered = set()
    for _, start, end in windows:
        assert 0 <= start < end <= len(text)
        covered.update(range(start, end))
    assert all(index in covered for index, char in enumerate(text) if not char.isspace())


def test_long_ordinary_window_keeps_overlap_and_frozen_plain_text():
    text = ("ordinary words " * 120) + "\n```\ncode\n```\n"
    windows = list(_iter_document_splits(text, 800, 100))
    assert windows[0][2] - windows[1][1] == 100
    ordinary = "plain words " * 200
    assert list(_iter_document_splits(ordinary, 800, 100)) == list(_iter_splits(ordinary, 800, 100))


def record_queries(db):
    queries = []
    engine = db.bind.sync_engine

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return queries, engine, record


@pytest.mark.asyncio
async def test_point_validation_reads_fresh_manifest_without_snapshot(db, store, published):
    queries, engine, record = record_queries(db)
    try:
        await builds.check_points(db, published.old_id, store=store)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    build_queries = [query for query in queries if "FROM document_builds" in query]
    assert build_queries and all("document_builds.snapshot" not in query for query in build_queries)
    await db.execute(update(IndexBuild).where(IndexBuild.id == published.old_id)
                     .values(point_ids=[]).execution_options(synchronize_session=False))
    await db.commit()
    with pytest.raises(RagError, match="构建点清单"):
        await builds.check_points(db, published.old_id, store=store)


@pytest.mark.asyncio
async def test_active_point_cleanup_reads_no_snapshot_and_keeps_active_points(db, store, published):
    queries, engine, record = record_queries(db)
    try:
        assert await builds.clean_build_points(db, published.old_id, store=store) is False
    finally:
        event.remove(engine, "before_cursor_execute", record)
    build_queries = [query for query in queries if "FROM document_builds" in query]
    assert build_queries and all("document_builds.snapshot" not in query for query in build_queries)
    assert {hit.id for hit in store.for_space(published.old_space).get_by_ids(published.old_points)} == set(published.old_points)


@pytest.mark.asyncio
async def test_build_result_reads_database_flags_without_snapshot(db, store, published, monkeypatch):
    doc = await db.get(Document, published.doc_id, populate_existing=True)
    monkeypatch.setattr(ingest, "get_embedding_client", lambda: published.embedding)
    monkeypatch.setattr(ingest, "get_vector_store", lambda: store)
    real_publish = builds.publish_build
    queries, engine, record = record_queries(db)

    async def publish(*args, **kwargs):
        result = await real_publish(*args, **kwargs)
        queries.clear()
        return result

    monkeypatch.setattr(builds, "publish_build", publish)
    try:
        assert await ingest._build_document(db, doc) == (True, None, True)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    build_queries = [query for query in queries if "FROM document_builds" in query]
    assert build_queries and all("document_builds.snapshot" not in query for query in build_queries)


@pytest.mark.asyncio
@pytest.mark.parametrize("link_failure", [False, True])
async def test_publish_returns_complete_body_without_repeating_successful_link_read(
    db, store, published, monkeypatch, link_failure,
):
    from app.pipeline import wikilink

    build = await candidate(db, published, label="new body")
    build_id = build.id
    await builds.validate_build(
        db, build.id, embedding=published.embedding, store=store, upsert=ingest._upsert_points
    )
    expected = build.snapshot
    if link_failure:
        async def fail_links(*args, **kwargs):
            raise RuntimeError("injected-links-failure")
        monkeypatch.setattr(wikilink, "maintain_source_links", fail_links)
    real_links = builds._maintain_current_links
    queries, engine, record = record_queries(db)

    async def links(*args, **kwargs):
        await real_links(*args, **kwargs)
        queries.clear()

    monkeypatch.setattr(builds, "_maintain_current_links", links)
    try:
        doc = await builds.publish_build(db, build_id, embedding=published.embedding)
        assert builds.document_snapshot(doc) == {
            field: expected[field] for field in builds._DOCUMENT_FIELDS
        }
        assert doc.active_build_id == build_id
    finally:
        event.remove(engine, "before_cursor_execute", record)
    if not link_failure:
        assert not any("documents.raw_content" in query or "documents.clean_report" in query
                       for query in queries)


@pytest.mark.asyncio
async def test_current_digest_hit_skips_body_predicate_and_raw_reload(db, store, published):
    doc = await db.get(Document, published.doc_id, populate_existing=True)
    prepared = ingest.prepare_document("original.md", doc.raw_content.encode("utf-8"))
    doc.clean_report = {
        **doc.clean_report,
        "content_identity": {"version": ingest.CONTENT_HASH_VERSION},
    }
    await db.commit()
    queries, engine, record = record_queries(db)
    try:
        result = await ingest._find_by_hash(db, prepared.content_hash, prepared=prepared)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert result.id == published.doc_id and ingest._current_identity(result)
    assert len(queries) == 1 and "documents.raw_content" not in queries[0]
