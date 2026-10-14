"""条目 2.4 · 重建索引服务（``POST /api/documents/reindex`` 的业务层）。

阶段 2 范围（有意偏差，已在 messages 与本 docstring 登记）：

- **notes=0**：笔记向量属检索阶段，本阶段不写、不假实现；
- **orphans_removed=0**：走「按 document 重写覆盖」路径（`index_chunks` 以 document
  为粒度覆盖点），不做跨库孤儿点扫描；不为凑数伪造正数；
- **recleaned**：`clean_report.rule_fingerprint` 与当前规则不一致且有留档时，
  走 `retry_document` 重跑 prepare → 重切块 → 重写向量（真实 reclean）；
  无留档时跳过 reclean 并在 messages 说明，只做向量重写。

服务层不 import `api` / FastAPI；端点层只做 HTTP 映射。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import RagError
from app.models import Document, IndexBuild
from app.pipeline import graphlink
from app.pipeline.builds import processing_fingerprint
from app.pipeline.cleaning import rule_fingerprint
from app.pipeline.ingest import (
    CONTENT_HASH_VERSION, index_chunks, retry_document, rechunk_document,
)

log = logging.getLogger(__name__)


@dataclass
class ReindexResult:
    """重建索引统计（对齐 02 §7.4 ReindexOut）。"""

    documents: int = 0
    notes: int = 0
    vectors: int = 0
    failed: int = 0
    orphans_removed: int = 0
    recleaned: int = 0
    messages: list[str] = field(default_factory=list)


def _needs_reclean(report: object, current_fp: str) -> bool:
    """清洗规则或正文身份过期时，显式重建须重解析同一条文档。"""
    if not isinstance(report, dict):
        return True
    identity = report.get("content_identity")
    return (
        report.get("rule_fingerprint") != current_fp
        or not isinstance(identity, dict)
        or identity.get("version") != CONTENT_HASH_VERSION
    )


async def _load_targets(
    session: AsyncSession, document_id: int | None
) -> list[Document]:
    """取本次重建目标：指定 id 或全部未删文档。"""
    if document_id is not None:
        doc = await session.get(Document, document_id)
        if doc is None or doc.deleted_at is not None:
            raise RagError("文档不存在，无法重建索引。")
        return [doc]

    rows = (
        await session.execute(
            select(Document)
            .where(Document.deleted_at.is_(None))
            .order_by(Document.id.asc())
        )
    ).scalars().all()
    return list(rows)


async def reindex_documents(
    session: AsyncSession, document_id: int | None = None
) -> ReindexResult:
    """重建索引：对每个目标文档重写向量；指纹过期且有留档时连带 reclean。

    - 指定 ``document_id`` 不存在或在回收站 → ``RagError``（端点层转 400）；
    - 空库全量 → 全 0 的 ``ReindexResult``（仍是 200）；
    - 单文档失败不中断整批：记 failed + messages，继续下一个。
    """
    # P3②（批量重建只在收尾算一次）：范围内逐篇发布钩子**暂缓**，收尾对每个受影响库重算一次。
    # `batch_scope` 走任务局部 ContextVar + finally 恢复 ⇒ 并发普通导入照常触发，批次抛错/取消
    # 都不泄漏暂缓态；这里只多包一层范围，下面的循环（含单篇失败继续的语义）一字未动。
    with graphlink.batch_scope():
        return await _reindex_documents(session, document_id)


async def _reindex_documents(
    session: AsyncSession, document_id: int | None = None,
) -> ReindexResult:
    """`reindex_documents` 的实现体（包在 `graphlink.batch_scope()` 里，单独成函数便于引用）。"""
    current_fp = rule_fingerprint()
    processing = processing_fingerprint()
    targets = [(doc.id, doc.filename) for doc in await _load_targets(session, document_id)]

    result = ReindexResult()
    kb_ids: set[int] = set()
    # 笔记向量属检索阶段（阶段 3），本阶段不实现——不写 notes，也不假报数字
    result.messages.append("本阶段不重建笔记向量（notes=0，归属检索阶段）。")

    for doc_id, filename in targets:
        try:
            doc = await session.get(Document, doc_id, populate_existing=True)
            if doc is None or doc.deleted_at is not None:
                raise RagError("文档已删除，无法重建索引。")
            kb_ids.add(int(doc.kb_id or 1))
            report = doc.clean_report if isinstance(doc.clean_report, dict) else None
            stale = _needs_reclean(report, current_fp)
            stored = bool((doc.file_path or "").strip())
            build = await session.get(IndexBuild, doc.active_build_id) if doc.active_build_id else None
            old_processing = build.fingerprint.get("processing", {}) if build else {}
            if build and any(
                old_processing.get(key) != processing.get(key)
                for key in ("parse", "clean", "body", "ocr")
            ):
                stale = True

            if stale and stored:
                ingested = await retry_document(session, doc_id)
                refreshed = await session.get(Document, doc_id, populate_existing=True)
                if not ingested.vectorized:
                    result.failed += 1
                    result.messages.append(
                        f"id={doc_id} {filename}：重清洗向量失败（{ingested.message}）"
                    )
                    continue
                result.recleaned += 1
                result.documents += 1
                if refreshed is not None and refreshed.vectorized:
                    result.vectors += int(refreshed.chunk_count or ingested.chunk_count or 0)
                continue

            if stale and not stored:
                result.messages.append(
                    f"id={doc_id} {filename}：清洗规则已变更但缺少留档，"
                    "跳过 reclean，仅重写向量。"
                )

            rechunk = build and old_processing != processing and not stale
            if rechunk:
                ingested = await rechunk_document(session, doc_id)
                ok, err = ingested.vectorized, ingested.message if not ingested.vectorized else None
            else:
                ok, err = await index_chunks(session, doc)
            if ok:
                refreshed = await session.get(Document, doc_id, populate_existing=True)
                result.documents += 1
                result.vectors += int(refreshed.chunk_count or 0)
            else:
                result.failed += 1
                result.messages.append(f"id={doc_id} {filename}：向量写入失败（{err}）")
        except Exception as exc:  # noqa: BLE001 —— 单文档失败不炸整批
            await session.rollback()
            log.warning("reindex id=%s 失败: %s", doc_id, exc)
            result.failed += 1
            result.messages.append(f"id={doc_id} {filename}：{exc}")

    # 导入资料自动连线（专项 · 2026-10-08）：按**涉及的库**全量重算「内容接近」边。
    # 关态下 `recompute_for_kb_ids` 直接返回 []（连新表都不 SELECT）；结果只写既有
    # `messages` 字段 —— `ReindexResult` 形状冻结，不给它加字段。
    if graphlink.enabled() and kb_ids:
        try:
            for outcome in await graphlink.recompute_for_kb_ids(session, sorted(kb_ids)):
                result.messages.append(
                    f"连线 kb_id={outcome['kb_id']}：{outcome['status']}，"
                    f"写入 {outcome['edges']} 条（参与文档 {outcome['documents']} 篇）。"
                )
        except Exception as exc:  # noqa: BLE001 —— 派生边失败不改本次重建的统计
            await session.rollback()
            log.warning("重建索引收尾连线重算失败：%s", exc)

    # orphans_removed：覆盖写路径下不做跨库孤儿扫描，如实报 0
    return result


__all__ = ["ReindexResult", "reindex_documents"]
