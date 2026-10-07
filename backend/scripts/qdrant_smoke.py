"""阶段 0 Qdrant 连通冒烟：真容器 URL，写证据日志。"""

from __future__ import annotations

import asyncio
import uuid

from app.vector import FakeEmbedding, get_vector_store
from app.vector.base import VectorPoint


async def main() -> None:
    s = get_vector_store()
    print("before", s.collection_info())
    s.ensure_collection(16)
    emb = FakeEmbedding(dim=16)
    vecs = await emb.embed_texts(["知澜阶段0连通", "第二条"])
    pid = str(uuid.uuid4())
    s.upsert([VectorPoint(id=pid, vector=vecs[0], payload={"smoke": "s0", "chunk_id": 1})])
    hits = s.query(vecs[0], limit=3, filters={"smoke": "s0"})
    print("hits", hits)
    print("count", s.count())
    by = s.get_by_ids([pid])
    print("by_ids", by)
    s.delete([pid])
    print("after_delete", s.count())
    print("QDRANT_CONNECT_OK")


if __name__ == "__main__":
    asyncio.run(main())
