"""VectorStore 单测：FakeEmbedding + 本地 path Qdrant，不连真 embedding API。"""

from __future__ import annotations

import uuid

import pytest

from app.vector.base import VectorPoint
from app.vector.embed import FakeEmbedding
from app.vector.qdrant_store import QdrantVectorStore


@pytest.fixture
def store(tmp_path):
    s = QdrantVectorStore(path=str(tmp_path / "qdrant"), collection="unit_chunks")
    s.ensure_collection(dim=16)
    return s


@pytest.mark.asyncio
async def test_upsert_query_by_ids_delete_count(store):
    emb = FakeEmbedding(dim=16)
    # Qdrant 点 id 必须是 UUID 或 unsigned int（本地/服务端一致）
    id1, id2, id3 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    vecs = await emb.embed_texts(["苹果水果", "香蕉水果", "汽车交通"])
    store.upsert(
        [
            VectorPoint(id=id1, vector=vecs[0], payload={"document_id": 1, "tag": "fruit"}),
            VectorPoint(id=id2, vector=vecs[1], payload={"document_id": 1, "tag": "fruit"}),
            VectorPoint(id=id3, vector=vecs[2], payload={"document_id": 2, "tag": "car"}),
        ]
    )
    assert store.count() == 3

    hits = store.query(vecs[0], limit=2)
    assert hits and hits[0].id == id1

    filtered = store.query(vecs[0], limit=5, filters={"tag": "car"})
    assert [h.id for h in filtered] == [id3]

    by_ids = store.get_by_ids([id2, id3])
    assert {h.id for h in by_ids} == {id2, id3}

    store.delete([id1, id2, id3])
    assert store.count() == 0


def test_collection_info(store):
    info = store.collection_info()
    assert info["name"] == "unit_chunks"
    assert info["mode"] == "path"


def test_ensure_collection_dim_mismatch_raises(tmp_path):
    """已存在集合维度与本次 dim 不一致时必须抛错，禁止静默 upsert（回归修复）。"""
    path = str(tmp_path / "qdrant_dim")
    s1 = QdrantVectorStore(path=path, collection="dim_chunks")
    s1.ensure_collection(dim=16)
    with pytest.raises(ValueError, match="维度不匹配"):
        s1.ensure_collection(dim=1024)
    # 维度一致时幂等返回
    s1.ensure_collection(dim=16)
