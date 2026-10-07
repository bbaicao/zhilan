"""条目 2.5 冒烟：上传后 ready + vectorized=true + MySQL/Qdrant 双边一致。

用 FakeEmbedding + 本地 path Qdrant（不连真 embedding API / 不依赖容器 Qdrant），
在真 MySQL 测试库跑通：ingest → index_chunks → 双边对账 → purge 级联 → reindex。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models import Base, Document, IndexBuild
from app.pipeline.builds import current_chunks
from app.pipeline import ingest, reindex as reindex_mod
from app.vector.embed import FakeEmbedding
from app.vector.qdrant_store import QdrantVectorStore

# 证据捕获口径（`回归修复`，2026-09-18 补的**代码侧**修法）：中文控制台默认 GBK 码页，
# 本脚本会 print 含 ✅ 的中文；重定向 stdout 时按 GBK 编解码 ⇒ 中断脚本 + 日志乱码。
# 故在 import 之后立刻把两个流钉成 UTF-8，**不再依赖调用方记得设 PYTHONIOENCODING**。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 —— 非 TextIOWrapper（例如被 pytest 捕获）时没有这个方法
        pass

TEST_DB = "zhilan_v2_test"
DSN = settings.mysql_dsn.replace("+aiomysql", "+aiomysql").replace(
    f"/{settings.MYSQL_DATABASE}", f"/{TEST_DB}"
)
DIM = 16


def _engine():
    return create_async_engine(DSN, pool_pre_ping=True)


async def _clean(session):
    await session.execute(text("DELETE FROM chunks"))
    await session.execute(text("DELETE FROM document_builds"))
    await session.execute(text("DELETE FROM documents"))
    await session.commit()


async def main() -> None:
    eng = _engine()
    async with eng.begin() as bc:
        await bc.run_sync(lambda sync: Base.metadata.create_all(sync))
    factory = async_sessionmaker(eng, expire_on_commit=False)

    tmp = Path(tempfile.mkdtemp(prefix="zhilan_s25_"))
    type(settings).upload_path = property(lambda self: tmp)
    qdir = tmp / "qdrant"
    store = QdrantVectorStore(path=str(qdir), collection=f"s25_{uuid.uuid4().hex[:8]}")
    store.ensure_collection(dim=DIM)
    emb = FakeEmbedding(dim=DIM)

    # 注入：不碰真 API
    ingest.get_embedding_client = lambda **kwargs: emb
    ingest.get_vector_store = lambda **kwargs: store

    md = (
        "---\ncategory: 冒烟\ntags: [阶段2, 2.5]\n---\n\n"
        "# 知澜阶段2冒烟\n\n## 向量化\n\n上传后应 ready 且 vectorized=true，"
        "Qdrant 构建点与启用 chunks 一一对应。\n\n## 级联\n\n硬删时向量与 MySQL 同步清理。\n"
    )
    results: list[tuple[bool, str]] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        results.append((ok, label))
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" —— {detail}" if detail else ""), flush=True)

    async with factory() as session:
        await _clean(session)

        # 1. 上传 → ready + vectorized
        r = await ingest.ingest_document(session, "冒烟.md", md.encode("utf-8"))
        doc = await session.get(Document, r.document_id)
        check(doc.status == "ready", "上传后 status=ready", f"status={doc.status}")
        check(doc.vectorized is True, "上传后 vectorized=true", f"vector_error={doc.vector_error!r}")
        check(doc.chunk_count > 0, "chunk_count>0", f"chunk_count={doc.chunk_count}")

        chunk_ids = [chunk.id for chunk in await current_chunks(session, doc.id)]
        build = await session.get(IndexBuild, doc.active_build_id)
        scoped = store.for_space(build.space_id)
        hits = scoped.get_by_ids(build.point_ids)
        point_ids = {str(h.id) for h in hits}
        check(
            point_ids == set(build.point_ids)
            and {h.payload.get("chunk_id") for h in hits} == set(chunk_ids)
            and all(h.payload.get("build_id") == build.id for h in hits),
            "MySQL 启用块与 Qdrant 构建点双边一致",
            f"mysql={len(chunk_ids)} qdrant={len(point_ids)}",
        )
        hits = scoped.query(
            await _first_vec(emb, chunk_ids, session), limit=5,
            filters={"build_id": build.id, "space_id": build.space_id},
        )
        check(any(h.payload.get("document_id") == doc.id for h in hits), "Qdrant 可按向量命中该文档", f"hits={len(hits)}")

        # 2. reindex 覆盖重写
        rr = await reindex_mod.reindex_documents(session, document_id=doc.id)
        check(rr.documents >= 1 and rr.failed == 0, "reindex 单文档成功", f"docs={rr.documents} vecs={rr.vectors} failed={rr.failed}")
        doc2 = await session.get(Document, doc.id)
        check(doc2.vectorized is True, "reindex 后仍 vectorized=true", f"vector_error={doc2.vector_error!r}")

        # 3. 硬删级联
        builds = [
            build, await session.get(IndexBuild, doc2.active_build_id)
        ]
        n_before = scoped.count()
        await ingest.purge_document(session, doc.id)
        remaining = (
            await session.execute(
                text("SELECT COUNT(*) FROM chunks WHERE document_id=:d").bindparams(d=doc.id)
            )
        ).scalar()
        doc_gone = await session.get(Document, doc.id)
        check(remaining == 0 and doc_gone is None, "purge 后 MySQL 行消失", f"chunks_left={remaining}")
        check(
            all(store.for_space(b.space_id).get_by_ids(b.point_ids) == [] for b in builds),
            "purge 后新旧构建的 Qdrant 点消失",
            f"count_before={n_before} count_after={scoped.count()}",
        )

        await _clean(session)

    await eng.dispose()
    store.close()
    failed = [label for ok, label in results if not ok]
    print(f"\n== 结果 {len(results) - len(failed)}/{len(results)} PASS ==")
    if failed:
        raise SystemExit(1)
    print("条目 2.5 向量冒烟通过 ✅")


async def _first_vec(emb: FakeEmbedding, chunk_ids, session) -> list[float]:
    content = (
        await session.execute(
            text("SELECT content FROM chunks WHERE id=:i").bindparams(i=chunk_ids[0])
        )
    ).scalar()
    return (await emb.embed_texts([content or ""]))[0]


if __name__ == "__main__":
    asyncio.run(main())
