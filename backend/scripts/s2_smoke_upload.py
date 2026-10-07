"""条目 2.1 上传冒烟：在真 MySQL（zhilan_v2_test）上跑通入库全链路。

流程：建 MD → ingest_document → 打印 doc + 首块元数据 → 同内容重传触发
DuplicateDocument → 强制写块失败留 failed 行 → retry_document 成功 → 清理。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.errors import DuplicateDocument
from app.models import Base, Chunk, Document
from app.pipeline import ingest

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


def _engine():
    return create_async_engine(DSN, pool_pre_ping=True)


async def _clean(session):
    await session.execute(text("DELETE FROM chunks"))
    await session.execute(text("DELETE FROM documents"))
    await session.commit()


async def main():
    eng = _engine()
    # 保证表存在（不破坏既有表）
    async with eng.begin() as bc:
        await bc.run_sync(lambda sync: Base.metadata.create_all(sync))
    factory = async_sessionmaker(eng, expire_on_commit=False)

    # 留档目录重定向到本机临时目录（真实写盘）
    tmp = Path(tempfile.mkdtemp(prefix="zhilan_smoke_"))
    type(settings).upload_path = property(lambda self: tmp)

    async with factory() as session:
        await _clean(session)

        # —— 步骤 1：正常上传 MD ——
        md = "# 知澜项目周报\n\n本周完成了文档接入与状态机，状态机覆盖 pending→ready/failed。\n" * 4
        r = await ingest.ingest_document(session, "周报.md", md.encode("utf-8"))
        doc = await session.get(Document, r.document_id)
        # 先把 id 取出来：下面步骤 3 的失败分支里 `_finish_ingest` 会 rollback，
        # 那会把本 session 里所有对象置为 expired；之后**任何**属性访问（连 `.id`
        # 也是）都会触发惰性刷新，而 async 会话下这必须发生在 await 上下文里，
        # 否则抛 MissingGreenlet。这是 async SQLAlchemy 的既有脾气，不是产品缺陷。
        doc_id = doc.id
        print("== 步骤1 上传成功 ==")
        print(f"  document_id={doc.id} filename={doc.filename}")
        print(f"  status={doc.status} index_status={doc.index_status} chunk_count={doc.chunk_count} vectorized={doc.vectorized}")
        print(f"  留档存在={Path(doc.file_path).exists()} 路径前缀={str(doc.file_path)[:60]}...")
        chunks = (await session.execute(
            text("SELECT ordinal,page,start_offset,end_offset FROM chunks WHERE document_id=:d ORDER BY ordinal").bindparams(d=doc.id)
        )).all()
        print(f"  首块 ordinal={chunks[0].ordinal} page={chunks[0].page} "
              f"start_offset={chunks[0].start_offset} end_offset={chunks[0].end_offset} "
              f"连续={[c.ordinal for c in chunks]==list(range(len(chunks)))}")

        # —— 步骤 2：同内容重传 → DuplicateDocument ——
        print("== 步骤2 同内容重传 ==")
        try:
            await ingest.ingest_document(session, "周报副本.md", md.encode("utf-8"))
            print("  ERROR: 未触发重复拦截")
        except DuplicateDocument as e:
            print(f"  触发 DuplicateDocument: existing_id={e.document_id} filename={e.filename}")

        # —— 步骤 3：强制写块失败 → failed 行 → retry_document 成功 ——
        print("== 步骤3 失败可重试 ==")
        orig = ingest._persist_chunks

        async def _boom(s, d, p):
            raise RuntimeError("模拟写 chunks 失败")

        ingest._persist_chunks = _boom
        try:
            await ingest.ingest_document(session, "fail.md", ("# 会失败\n\n内容。\n" * 3).encode("utf-8"))
            print("  ERROR: 未触发写块失败")
        except RuntimeError:
            # 取刚建的失败行
            rows = (await session.execute(text("SELECT id FROM documents ORDER BY id DESC LIMIT 1"))).all()
            failed_id = rows[0][0]
            failed = await session.get(Document, failed_id)
            print(f"  失败留行: id={failed.id} status={failed.status} index_status={failed.index_status} index_error={failed.index_error!r}")
        ingest._persist_chunks = orig
        retried = await ingest.retry_document(session, failed.id)
        fresh = await session.get(Document, retried.document_id)
        print(f"  retry_document 后: status={fresh.status} index_status={fresh.index_status} chunk_count={fresh.chunk_count}")

        # —— 步骤 4：清理 ——
        fresh_id = fresh.id
        await ingest.purge_document(session, doc_id)
        await ingest.purge_document(session, fresh_id)
        await _clean(session)
        print("== 步骤4 清理完成 ==")

    await eng.dispose()
    print("冒烟全链路通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
