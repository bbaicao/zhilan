"""U2 空间隔离、历史点先过滤、payload 与已发布构建双重核对。"""

from __future__ import annotations

import pytest

from app.models import Chunk, Document, IndexBuild
from app.retrieval.vector import search_vectors
from app.vector.base import VectorFilter, VectorHit, VectorPoint
from app.vector.embed import FakeEmbedding
from app.vector.identity import embedding_identity, point_id, space_id
from app.vector.qdrant_store import QdrantVectorStore, VectorStoreUnavailable


def test_same_dimension_models_use_distinct_shared_client_spaces(tmp_path):
    first_identity = embedding_identity(FakeEmbedding(dim=3))
    second_identity = {**first_identity, "model": "another-model"}
    first_space, second_space = space_id(first_identity), space_id(second_identity)
    assert first_space != second_space
    assert point_id("first-build", 1) != point_id("second-build", 1)
    assert point_id("first-build", 1) == point_id("first-build", 1)

    store = QdrantVectorStore(path=str(tmp_path / "vectors"))
    try:
        first, second = store.for_space(first_space), store.for_space(second_space)
        first.ensure_collection(3)
        second.ensure_collection(3)
        first.upsert([VectorPoint(point_id("first-build", 1), [1.0, 0.0, 0.0], {})])
        assert first.count() == 1 and second.count() == 0
        first.close()
        second.upsert([VectorPoint(point_id("second-build", 1), [0.0, 1.0, 0.0], {})])
        assert second.count() == 1
        assert store.for_space(first_space).count() == 1
        store.close()
        with pytest.raises(VectorStoreUnavailable):
            second.count()
    finally:
        store.close()


async def _seed_build(db, embedding):
    current_space = space_id(embedding_identity(embedding))
    document = Document(
        filename="版本.md", file_type="md", status="ready", index_status="ready", kb_id=1
    )
    db.add(document)
    await db.flush()
    current = IndexBuild(
        id="current-build",
        document_id=document.id,
        content_build_id="current-build",
        state="published",
        fingerprint={},
        space_id=current_space,
        vectorized=True,
    )
    db.add(current)
    document.active_build_id = document.content_build_id = current.id
    old_chunk = Chunk(
        document_id=document.id, build_id="old-build", ordinal=0, content="历史正文"
    )
    current_chunk = Chunk(
        document_id=document.id, build_id=current.id, ordinal=0, content="当前正文"
    )
    db.add_all([old_chunk, current_chunk])
    await db.commit()
    return document, old_chunk, current_chunk, current_space


@pytest.mark.asyncio
async def test_historical_points_are_filtered_before_top_k(db, tmp_path):
    embedding = FakeEmbedding(dim=3)
    document, old, current, current_space = await _seed_build(db, embedding)
    document.category, document.tags = "变更分类", ["当前标签"]
    await db.commit()
    vector = (await embedding.embed_texts(["版本"]))[0]
    store = QdrantVectorStore(path=str(tmp_path / "vectors"))
    try:
        scoped = store.for_space(current_space)
        scoped.ensure_collection(3)
        scoped.upsert(
            [
                VectorPoint(
                    point_id(build, chunk.id),
                    values,
                    {
                        "document_id": document.id,
                        "chunk_id": chunk.id,
                        "build_id": build,
                        "space_id": current_space,
                    },
                )
                for chunk, build, values in [
                    (old, "old-build", vector),
                    (current, "current-build", [-value for value in vector]),
                ]
            ]
        )
        assert scoped.query(vector, limit=1)[0].payload["build_id"] == "old-build"
        materials, warning = await search_vectors(
            db, "版本",
            filters={"kb_ids": [document.kb_id], "categories": ["变更分类"], "tags": ["当前标签"]},
            limit=1, embedding=embedding, store=store,
        )
        assert warning is None
        assert [item.chunk_id for item in materials] == [current.id]
        assert scoped.count() == 2  # 历史向量仍存在，不能依靠先删除来通过。
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_key", ["build_id", "space_id"])
async def test_mismatched_payload_never_becomes_current_material(db, wrong_key):
    embedding = FakeEmbedding(dim=3)
    _document, _old, current, current_space = await _seed_build(db, embedding)
    payload = {
        "chunk_id": current.id,
        "build_id": "current-build",
        "space_id": current_space,
        wrong_key: "wrong",
    }

    class CorruptStore:
        def for_space(self, requested):
            assert requested == current_space
            return self

        def query(self, vector, *, limit, filters):
            assert filters == VectorFilter(
                any_of={"build_id": ["current-build"], "space_id": [current_space]}
            )
            return [VectorHit(point_id("current-build", current.id), 1.0, payload)]

    materials, warning = await search_vectors(
        db, "版本", embedding=embedding, store=CorruptStore()
    )
    assert warning is None and materials == []


@pytest.mark.asyncio
async def test_production_does_not_guess_legacy_point_model(db, monkeypatch):
    from app.retrieval import vector as module

    document = Document(
        filename="旧版.md", file_type="md", status="ready", index_status="ready"
    )
    db.add(document)
    await db.flush()
    db.add(Chunk(document_id=document.id, ordinal=0, content="没有模型身份的旧点"))
    await db.commit()

    class LegacyStore:
        def query(self, *args, **kwargs):
            pytest.fail("未标模型的旧点不能进入生产向量查询")

    monkeypatch.setattr(module, "get_embedding_client", lambda: FakeEmbedding(dim=3))
    monkeypatch.setattr(module, "get_vector_store", lambda: LegacyStore())
    assert await module.search_vectors(db, "旧版") == ([], None)
