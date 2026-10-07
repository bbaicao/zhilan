"""U3：真实本地范围下推、元数据回填与坏点补取边界。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.errors import RagError
from app.models import IndexBuild
from app.pipeline.builds import backfill_scope_payloads
from app.retrieval.vector import search_vectors
from app.vector.base import PayloadRange, VectorFilter, VectorHit, VectorPoint
from app.vector.embed import FakeEmbedding
from app.vector.identity import point_id
from app.vector.payload import scope_payload
from app.vector.qdrant_store import QdrantVectorStore
from tests.vector.test_u2_spaces import _seed_build


class CountingEmbedding(FakeEmbedding):
    def __init__(self):
        super().__init__(dim=3)
        self.calls = []

    async def embed_texts(self, texts):
        self.calls.append(list(texts))
        return await super().embed_texts(texts)


def payload(document, chunk, current_space):
    return {
        "document_id": document.id, "chunk_id": chunk.id,
        "build_id": chunk.build_id, "space_id": current_space,
    }


def stored_vector(scoped, identifier):
    return scoped._require_client().retrieve(
        collection_name=scoped.collection, ids=[identifier], with_vectors=True,
    )[0].vector


@pytest.mark.asyncio
async def test_real_local_full_scope_is_applied_before_top_k_and_uses_current_fingerprint(
    db, tmp_path, monkeypatch,
):
    embedding = CountingEmbedding()
    document, _old, current, current_space = await _seed_build(db, embedding)
    document.kb_id, document.category = 5, "合同"
    document.tags = ["共同", "已审"]
    document.created_at = datetime(2026, 10, 4)
    metadata = scope_payload(
        kb_id=document.kb_id, category=document.category,
        tags=document.tags, created_at=document.created_at,
    )
    build = await db.get(IndexBuild, document.active_build_id)
    build.fingerprint = {**build.fingerprint, "payload_scope": metadata["scope_hash"]}
    build.point_ids = [point_id(current.build_id, current.id)]
    await db.commit()
    vector = embedding._one("版本")
    target_id = point_id(current.build_id, current.id)
    target_payload = {**payload(document, current, current_space), **metadata}
    store = QdrantVectorStore(path=str(tmp_path / "vectors"))
    try:
        scoped = store.for_space(current_space)
        scoped.ensure_collection(3)
        wrong_dimensions = [
            {"kb_id": 99}, {"category": "无关"}, {"tags": ["共同"]},
            {"tags": ["已审"]}, {"created_ts": metadata["created_ts"] - 1},
            {"created_ts": metadata["created_ts"] + 1},
        ]
        scoped.upsert([
            VectorPoint(
                point_id(current.build_id, 900_000 + index), vector,
                {**target_payload, "chunk_id": 900_000 + index,
                 **wrong_dimensions[index % len(wrong_dimensions)]},
            )
            for index in range(31)
        ] + [VectorPoint(target_id, [-value for value in vector], target_payload)])
        # 31 个高分错误范围点仍在库里；仅后过滤 TOP1 会漏掉目标。
        assert scoped.query(vector, limit=1)[0].id != target_id
        timestamp = document.created_at.replace(tzinfo=timezone.utc).timestamp()
        expected = VectorFilter(
            any_of={
                "build_id": [current.build_id], "space_id": [current_space],
                "kb_id": [4, 5], "category": ["政策", "合同"],
            },
            all_of={"tags": ["共同", "已审"]},
            ranges={"created_ts": PayloadRange(gte=timestamp, lte=timestamp)},
        )
        assert [hit.id for hit in scoped.query(vector, limit=1, filters=expected)] == [target_id]
        seen = []
        original_query = QdrantVectorStore.query

        def record_query(self, query_vector, *, limit, filters):
            seen.append((limit, filters))
            return original_query(self, query_vector, limit=limit, filters=filters)

        monkeypatch.setattr(QdrantVectorStore, "query", record_query)
        filters = {
            "kb_ids": [4, 5], "categories": ["政策", "合同"], "tags": ["共同", "已审"],
            "created_after": "2026-10-04T08:00:00+08:00",
            "created_before": "2026-10-04T08:00:00+08:00",
        }
        materials, warning = await search_vectors(
            db, "版本", filters, limit=1, embedding=embedding, store=store,
        )

        assert warning is None and [item.chunk_id for item in materials] == [current.id]
        assert seen == [(1, expected)]
        assert embedding.calls == [["版本"]] and scoped.count() == 32
    finally:
        store.close()


@pytest.mark.asyncio
async def test_scope_backfill_is_repeatable_keeps_vectors_and_metadata_change_uses_sql_fallback(
    db, tmp_path, monkeypatch,
):
    embedding = CountingEmbedding()
    document, old, current, current_space = await _seed_build(db, embedding)
    build = await db.get(IndexBuild, document.active_build_id)
    target_id = point_id(current.build_id, current.id)
    old_id = point_id(old.build_id, old.id)
    build.point_ids = [target_id]
    document.created_at = datetime(2026, 10, 4)
    document.category, document.tags = "原分类", ["原标签"]
    await db.commit()
    vector = embedding._one("版本")
    store = QdrantVectorStore(path=str(tmp_path / "vectors"))
    try:
        scoped = store.for_space(current_space)
        scoped.ensure_collection(3)
        scoped.upsert([
            VectorPoint(target_id, vector, payload(document, current, current_space)),
            VectorPoint(old_id, [-value for value in vector], payload(document, old, current_space)),
        ])
        before = stored_vector(scoped, target_id)
        first = await backfill_scope_payloads(db, store=store, batch_size=1)
        await db.commit()
        second = await backfill_scope_payloads(db, store=store, batch_size=1)
        await db.commit()
        assert first == second and first["points"] == first["documents"] == 1
        assert stored_vector(scoped, target_id) == before
        assert "scope_hash" not in scoped.get_by_ids([old_id])[0].payload
        # 当前 SQL 元数据变更，旧 payload 不得挡住目标，也不应冒充已同步。
        document.kb_id, document.category, document.tags = 4, "新分类", ["共同", "新标签"]
        await db.commit()
        seen = []
        original_query = QdrantVectorStore.query

        def record_query(self, query_vector, *, limit, filters):
            seen.append(filters)
            return original_query(self, query_vector, limit=limit, filters=filters)

        monkeypatch.setattr(QdrantVectorStore, "query", record_query)
        filters = {"kb_ids": [4], "categories": ["新分类"], "tags": ["共同", "新标签"]}
        result, warning = await search_vectors(
            db, "版本", filters, limit=1, embedding=embedding, store=store,
        )
        assert warning is None and [item.chunk_id for item in result] == [current.id]
        assert seen == [VectorFilter(
            any_of={"build_id": [current.build_id], "space_id": [current_space]},
        )]
        assert scoped.get_by_ids([target_id])[0].payload["kb_id"] == 1
        third = await backfill_scope_payloads(db, store=store, batch_size=1)
        await db.commit()
        assert third["points"] == 1
        seen.clear()
        result, warning = await search_vectors(
            db, "版本", filters, limit=1, embedding=embedding, store=store,
        )
        assert warning is None and [item.chunk_id for item in result] == [current.id]
        assert seen[0].any_of["kb_id"] == [4]
        assert seen[0].any_of["category"] == ["新分类"]
        assert seen[0].all_of["tags"] == ["共同", "新标签"]
        assert stored_vector(scoped, target_id) == before and scoped.count() == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_scope_backfill_rejects_empty_manifest_for_nonempty_current_build(db, tmp_path):
    embedding = FakeEmbedding(dim=3)
    document, _old, current, current_space = await _seed_build(db, embedding)
    build = await db.get(IndexBuild, document.active_build_id)
    build.point_ids = []
    await db.commit()
    target_id = point_id(current.build_id, current.id)
    original_payload = payload(document, current, current_space)
    store = QdrantVectorStore(path=str(tmp_path / "vectors"))
    try:
        scoped = store.for_space(current_space)
        scoped.ensure_collection(3)
        scoped.upsert([VectorPoint(target_id, embedding._one("版本"), original_payload)])

        with pytest.raises(RagError, match="清单|身份|完整|分块"):
            await backfill_scope_payloads(db, store=store)

        assert "payload_scope" not in build.fingerprint
        assert scoped.get_by_ids([target_id])[0].payload == original_payload
    finally:
        store.close()


@pytest.mark.asyncio
async def test_scope_backfill_rejects_flushed_but_uncommitted_metadata(db, tmp_path):
    embedding = FakeEmbedding(dim=3)
    document, _old, current, current_space = await _seed_build(db, embedding)
    target_id = point_id(current.build_id, current.id)
    build = await db.get(IndexBuild, document.active_build_id)
    build.point_ids = [target_id]
    await db.commit()
    store = QdrantVectorStore(path=str(tmp_path / "vectors"))
    try:
        scoped = store.for_space(current_space)
        scoped.ensure_collection(3)
        scoped.upsert([VectorPoint(
            target_id, embedding._one("版本"), payload(document, current, current_space),
        )])
        await backfill_scope_payloads(db, store=store)
        await db.commit()
        before = scoped.get_by_ids([target_id])[0].payload
        document.category = "尚未提交的新分类"
        await db.flush()

        with pytest.raises(RagError, match="先提交"):
            await backfill_scope_payloads(db, store=store)

        await db.rollback()
        assert scoped.get_by_ids([target_id])[0].payload == before
        found, warning = await search_vectors(
            db, "版本", {"kb_ids": [1]}, limit=1, embedding=embedding, store=store,
        )
        assert warning is None and len(found) == 1
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_limits", "has_result"),
    [("filled", [1, 2], True), ("exhausted", [1, 2], False), ("bounded", [1, 2, 4], False)],
)
async def test_bad_payload_is_checked_in_sql_and_refill_uses_one_embedding(
    db, case, expected_limits, has_result, caplog,
):
    caplog.set_level("INFO", logger="app.retrieval.vector")
    embedding = CountingEmbedding()
    document, old, current, current_space = await _seed_build(db, embedding)
    # 看似当前build的历史分块在 SQL active_chunks 二次核对时必须被剔除。
    bad = VectorHit(
        point_id(old.build_id, old.id), 1.0,
        {**payload(document, old, current_space), "build_id": current.build_id},
    )
    good = VectorHit(
        point_id(current.build_id, current.id), 0.2,
        payload(document, current, current_space),
    )
    hits = [bad, good] if case == "filled" else [bad]
    if case == "bounded":
        hits += [
            VectorHit(
                point_id(current.build_id, identifier), 0.8,
                {**payload(document, current, current_space), "chunk_id": identifier},
            )
            for identifier in (900_001, 900_002, 900_003)
        ] + [good]
    limits, vectors = [], []

    class CorruptStore:
        def for_space(self, requested):
            assert requested == current_space
            return self

        def query(self, vector, *, limit, filters):
            assert filters == VectorFilter(
                any_of={"build_id": [current.build_id], "space_id": [current_space]},
            )
            limits.append(limit)
            vectors.append(list(vector))
            return hits[:limit]

    materials, warning = await search_vectors(
        db, "版本", limit=1, embedding=embedding, store=CorruptStore(),
    )

    assert warning is None
    assert [item.chunk_id for item in materials] == ([current.id] if has_result else [])
    assert limits == expected_limits and embedding.calls == [["版本"]]
    assert all(vector == vectors[0] for vector in vectors)
    reason = "budget" if case == "bounded" else case
    assert (
        f"reason={reason} rounds={len(expected_limits)} requested={expected_limits[-1]}"
        in caplog.text
    )
