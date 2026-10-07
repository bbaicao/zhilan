"""索引构建：SQL 启用指针决定可读版本；向量先写入独立空间，再条件发布。"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
from datetime import datetime
from uuid import uuid4

from sqlalchemy import case, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.app_logging import redact
from app.core.config import settings
from app.core.errors import RagError
from app.models import Chunk, Document, IndexBuild, WikiLink, active_chunks
from app.pipeline.cleaning import rule_fingerprint
from app.pipeline import lexical_index  # noqa: F401 — ORM flush 同事务维护版本分块词项。
from app.vector.base import VectorPoint
from app.vector.identity import embedding_identity, point_id, space_id
from app.vector.payload import scope_payload

log = logging.getLogger(__name__)

_DOCUMENT_FIELDS = (
    "filename", "file_type", "file_path", "kb_id", "category", "tags",
    "created_at", "content_hash", "raw_content", "clean_report", "source_encoding",
    "chunk_count",
)


def processing_fingerprint() -> dict:
    """算法版本与参数单独记录，换 embedding 不触发重新解析。"""
    from app.pipeline.chunking import CODE_WINDOW_LINES, CODE_WINDOW_CHARS, CODE_WINDOW_OVERLAP

    return {
        "parse": "opt3-text-v1",
        "clean": rule_fingerprint(),
        "body": "u1-body-sha256-v1",
        "chunk": "u1-markdown-code-v1",
        "position": "u1-source-map-v1",
        "size": settings.RAG_CHUNK_SIZE,
        "overlap": settings.RAG_CHUNK_OVERLAP,
        "code_lines": CODE_WINDOW_LINES,
        "code_chars": CODE_WINDOW_CHARS,
        "code_overlap": CODE_WINDOW_OVERLAP,
        "ocr": {
            key: getattr(settings, key, None)
            for key in (
                "MINERU_ENABLE", "MINERU_BACKEND", "MINERU_VLM_ENABLE",
                "OCR_SCAN_THRESHOLD", "OCR_IMAGE_ENABLE",
            )
        },
    }


def document_snapshot(document: Document) -> dict:
    """只读已加载标量；同步函数不承担数据库加载。"""
    state = inspect(document)
    if state.has_identity and set(_DOCUMENT_FIELDS) & state.unloaded:
        raise RagError("文档快照字段尚未加载，请在异步入口先加载。")
    snapshot = {key: copy.deepcopy(state.dict.get(key)) for key in _DOCUMENT_FIELDS}
    date = snapshot["created_at"]
    if isinstance(date, datetime):
        snapshot["created_at"] = date.isoformat()
    return snapshot


def _values(snapshot: dict) -> dict:
    values = {key: copy.deepcopy(snapshot[key]) for key in _DOCUMENT_FIELDS if key in snapshot}
    date = values.get("created_at")
    if isinstance(date, str):
        values["created_at"] = datetime.fromisoformat(date)
    return values


async def _fresh_build(session: AsyncSession, build_id: str) -> IndexBuild:
    build = await session.get(IndexBuild, build_id, populate_existing=True)
    if build is None:
        raise RagError("索引构建不存在。")
    return build


async def stage_build(
    session: AsyncSession,
    document: Document,
    *,
    embedding,
    snapshot: dict | None = None,
    chunks: list[Chunk] | None = None,
) -> IndexBuild:
    """持久化准备产物，旧文档与启用块保持不变；chunks=None 表示纯重向量。"""
    state = inspect(document)
    required = set(_DOCUMENT_FIELDS) | {"id", "content_build_id", "index_status", "active_build_id"}
    missing = sorted(required & state.unloaded)
    if state.has_identity and missing:
        # 仅加载缺失字段；已加载的未提交编辑不能被 refresh 覆盖或提前 autoflush。
        with session.no_autoflush:
            await session.refresh(document, attribute_names=missing)
    build_id = str(uuid4())
    content_build_id = build_id if chunks is not None else document.content_build_id
    identity = embedding_identity(embedding)
    legacy_link_titles = []
    report = document.clean_report if isinstance(document.clean_report, dict) else {}
    if document.index_status == "ready" and not isinstance(report.get("canonical_text"), str):
        with session.no_autoflush:
            legacy_link_titles = list((await session.execute(
                select(WikiLink.target_title).where(
                    WikiLink.source_type == "document", WikiLink.source_id == document.id
                ).order_by(WikiLink.id)
            )).scalars().all())
    # 旧资料没有模型身份；保存完整旧正文供关键词回滚，不能替旧数字点猜模型。
    if document.index_status == "ready" and document.active_build_id is None:
        with session.no_autoflush:
            legacy = await session.scalar(select(IndexBuild).where(
                IndexBuild.document_id == document.id, IndexBuild.space_id.is_(None)
            ).limit(1))
        if legacy is None:
            legacy_snapshot = document_snapshot(document)
            if legacy_link_titles:
                legacy_snapshot["link_titles"] = legacy_link_titles
            session.add(IndexBuild(
                id=str(uuid4()), document_id=document.id, state="retired",
                content_build_id=document.content_build_id, fingerprint={"legacy": True},
                snapshot=legacy_snapshot, point_ids=[], vectorized=False,
                error="历史数字向量未记录模型身份；回滚保留关键词正文，向量须显式重建。",
            ))
    build = IndexBuild(
        id=build_id,
        document_id=document.id,
        base_build_id=document.active_build_id,
        content_build_id=content_build_id,
        state="preparing",
        fingerprint={
            "processing": processing_fingerprint(), "embedding": identity,
            "configuration": processing_fingerprint(),
        },
        space_id=space_id(identity),
        snapshot=copy.deepcopy(snapshot if snapshot is not None else document_snapshot(document)),
        point_ids=[],
        vectorized=False,
    )
    if isinstance(build.snapshot.get("created_at"), datetime):
        build.snapshot["created_at"] = build.snapshot["created_at"].isoformat()
    if legacy_link_titles:
        build.snapshot["link_titles"] = legacy_link_titles
    build.fingerprint["previous_link_title"] = (document.filename or "").rsplit(".", 1)[0]
    build.fingerprint["previous_link_kb_id"] = int(document.kb_id or 1)
    build.fingerprint["base_kb_id"] = document.kb_id
    if document.active_build_id:
        with session.no_autoflush:
            previous = await session.get(IndexBuild, document.active_build_id)
        if previous and chunks is None:
            build.fingerprint["processing"] = copy.deepcopy(
                previous.fingerprint.get("processing", {"legacy": True})
            )
        if snapshot is None and previous and previous.snapshot.get("source_archive_path"):
            build.snapshot["source_archive_path"] = previous.snapshot["source_archive_path"]
    elif chunks is None:
        build.fingerprint["processing"] = copy.deepcopy(
            (document.clean_report or {}).get("processing_fingerprint") or {"legacy": True}
        )
    if chunks is not None:
        actual = (build.snapshot.get("clean_report") or {}).get("processing_fingerprint")
        if actual:
            build.fingerprint["processing"] = copy.deepcopy(actual)
    if chunks is not None:
        for chunk in chunks:
            chunk.build_id = build_id
        session.add_all(chunks)
    session.add(build)
    await session.flush()
    rows = await build_chunks(session, build)
    if not rows:
        raise RagError("没有分块内容，无法构建索引。")
    build.point_ids = [point_id(build.id, chunk.id) for chunk in rows]
    await session.commit()
    return build


async def build_chunks(session: AsyncSession, build: IndexBuild) -> list[Chunk]:
    condition = (
        Chunk.build_id.is_(None) if build.content_build_id is None
        else Chunk.build_id == build.content_build_id
    )
    return list((await session.execute(
        select(Chunk).where(Chunk.document_id == build.document_id, condition)
        .order_by(Chunk.ordinal, Chunk.id)
    )).scalars().all())


async def _write_complete(function, *args):
    """取消时等同步写线程结束，防止清理后它又把本轮点写回来。"""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


def _check_vectors(vectors, rows: list[Chunk], dim: int) -> None:
    if not isinstance(vectors, list) or len(vectors) != len(rows):
        raise RagError("Embedding 返回条数与分块数不一致。")
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dim:
            raise RagError("Embedding 向量维度与本轮模型配置不一致。")
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in vector
        ):
            raise RagError("Embedding 向量包含无效数值。")


async def validate_build(session: AsyncSession, build_id: str, *, embedding, store, upsert) -> None:
    build = await _fresh_build(session, build_id)
    if build.state != "preparing":
        raise RagError("该索引构建已结束准备，不能重复写入。")
    if embedding_identity(embedding) != build.fingerprint["embedding"]:
        raise RagError("构建模型配置已经变化，请重新构建。")
    rows = await build_chunks(session, build)
    vectors = await embedding.embed_texts([row.content for row in rows])
    _check_vectors(vectors, rows, int(build.fingerprint["embedding"]["dim"]))
    snapshot = build.snapshot
    metadata = scope_payload(
        kb_id=snapshot.get("kb_id"), category=snapshot.get("category"),
        tags=snapshot.get("tags"), created_at=snapshot.get("created_at"),
    )
    points = [
        VectorPoint(
            id=point_id(build.id, row.id),
            vector=vector,
            payload={
                "chunk_id": row.id, "build_id": build.id, "space_id": build.space_id,
                "document_id": build.document_id, "ordinal": row.ordinal,
                "filename": snapshot.get("filename"), **metadata,
            },
        )
        for row, vector in zip(rows, vectors)
    ]
    scoped = store.for_space(build.space_id)
    await _write_complete(upsert, scoped, points, int(build.fingerprint["embedding"]["dim"]))
    await check_points(session, build_id, store=store)
    build.state = "validated"
    build.vectorized = True
    build.error = None
    build.fingerprint = {**build.fingerprint, "payload_scope": metadata["scope_hash"]}
    await session.commit()


async def check_points(session: AsyncSession, build_id: str, *, store) -> None:
    """验证完整点集与身份，SQL 发布失败/重启恢复也用同一判据。"""
    build = await _fresh_build(session, build_id)
    rows = await build_chunks(session, build)
    expected = {point_id(build.id, row.id): row.id for row in rows}
    if set(build.point_ids) != set(expected):
        raise RagError("构建点清单与分块不一致，拒绝发布。")
    hits = await asyncio.to_thread(store.for_space(build.space_id).get_by_ids, build.point_ids)
    if len(hits) != len(expected) or {hit.id for hit in hits} != set(expected):
        raise RagError("向量写入不完整，拒绝切换索引版本。")
    for hit in hits:
        payload = hit.payload
        if (
            payload.get("build_id") != build.id or payload.get("space_id") != build.space_id
            or payload.get("chunk_id") != expected[hit.id]
            or payload.get("document_id") != build.document_id
        ):
            raise RagError("向量构建身份校验失败，拒绝切换索引版本。")


async def backfill_scope_payloads(
    session: AsyncSession, *, store, after_document_id: int = 0, batch_size: int = 200,
) -> dict:
    """显式有界、可重跑回填；只更新已知构建的现存点，不动向量或旧数字点。

    先核对已提交的资料范围，拒绝把尚未提交的修改写入外部向量库。
    调用方提交回填记录；中断时重复本批安全。读路径未回填也先用当前 SQL 构建白名单，
    所以回填失败不会扩大范围或丢掉旧资料。
    """
    size = max(1, min(int(batch_size), 500))
    rows = (await session.execute(
        select(Document, IndexBuild)
        .join(IndexBuild, IndexBuild.id == Document.active_build_id)
        .where(
            Document.id > int(after_document_id),
            Document.deleted_at.is_(None), Document.index_status == "ready",
            IndexBuild.document_id == Document.id, IndexBuild.vectorized.is_(True),
            IndexBuild.space_id.is_not(None),
        ).order_by(Document.id).limit(size)
    )).all()
    if rows:
        bind = session.bind
        engine = bind.engine if isinstance(bind, AsyncConnection) else bind
        async with AsyncSession(engine) as committed:
            snapshot = (await committed.execute(
                select(
                    Document.id, Document.active_build_id, Document.kb_id,
                    Document.category, Document.tags, Document.created_at,
                    Document.deleted_at, Document.index_status,
                ).where(Document.id.in_([doc.id for doc, _ in rows]))
            )).all()
        committed_scope = {
            row[0]: (
                row[1], scope_payload(
                    kb_id=row[2], category=row[3], tags=row[4], created_at=row[5],
                )["scope_hash"], row[6], row[7],
            ) for row in snapshot
        }
        for doc, build in rows:
            current_hash = scope_payload(
                kb_id=doc.kb_id, category=doc.category, tags=doc.tags, created_at=doc.created_at,
            )["scope_hash"]
            if committed_scope.get(doc.id) != (build.id, current_hash, None, "ready"):
                raise RagError("范围回填前请先提交资料范围的修改；本批未写入向量。")
    updated = 0
    for doc, build in rows:
        metadata = scope_payload(
            kb_id=doc.kb_id, category=doc.category, tags=doc.tags, created_at=doc.created_at,
        )
        await check_points(session, build.id, store=store)
        scoped = store.for_space(build.space_id)
        count = await asyncio.to_thread(
            scoped.set_payload, metadata, ids=build.point_ids
        )
        if count != len(build.point_ids):
            raise RagError("向量范围回填不完整，已停止本批回填。")
        updated += count
        build.fingerprint = {**build.fingerprint, "payload_scope": metadata["scope_hash"]}
    return {
        "documents": len(rows), "points": updated,
        "next_document_id": int(rows[-1][0].id) if rows else int(after_document_id),
        "has_more": len(rows) == size,
    }


async def publish_build(session: AsyncSession, build_id: str, *, embedding=None) -> Document:
    build = await _fresh_build(session, build_id)
    if build.state != "validated":
        raise RagError("索引构建尚未通过校验。")
    doc = await session.get(Document, build.document_id, populate_existing=True)
    if doc is None:
        raise RagError("文档已删除，无法发布索引。")
    document_id = int(doc.id)
    old_title = (doc.filename or "").rsplit(".", 1)[0]
    old_kb_id = int(doc.kb_id or 1)
    if doc.index_status == "ready" and not build.vectorized:
        raise RagError("新版向量构建失败，已保留原文档与索引。")
    if build.fingerprint.get("configuration") != processing_fingerprint() or (
        build.content_build_id == build.id
        and build.fingerprint["processing"] != processing_fingerprint()
    ):
        raise RagError("处理规则已变化，拒绝发布过期构建。")
    if embedding is not None and embedding_identity(embedding) != build.fingerprint["embedding"]:
        raise RagError("向量模型已变化，拒绝发布过期构建。")
    values = _values(build.snapshot)
    # 归库是独立可变事实；发布不撤销构建期间已经完成的归库/知识库合并。
    if "kb_id" in values:
        values["kb_id"] = case(
            (Document.kb_id == build.fingerprint.get("base_kb_id"), values["kb_id"]),
            else_=Document.kb_id,
        )
    values.update(
        active_build_id=build.id, content_build_id=build.content_build_id,
        status="ready", index_status="ready", index_error=None,
        vectorized=build.vectorized, vector_error=build.error,
    )
    guard = (
        Document.active_build_id.is_(None) if build.base_build_id is None
        else Document.active_build_id == build.base_build_id
    )
    result = await session.execute(
        update(Document).where(
            Document.id == build.document_id, Document.deleted_at.is_(None), guard
        ).values(**values).execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        build = await _fresh_build(session, build_id)
        build.state = "conflict"
        build.error = "启用版本已被另一轮构建改变，本轮未发布。"
        await session.commit()
        raise RagError(build.error)
    if build.base_build_id:
        await session.execute(
            update(IndexBuild).where(IndexBuild.id == build.base_build_id).values(state="retired")
        )
    build.state = "published"
    await session.commit()
    # 世代在共享发布入口推进，内部重建与恢复也不会继续使用旧缓存。
    from app.cache import invalidate_all

    invalidate_all("index_publish")
    await _maintain_current_links(
        session, document_id, old_title=old_title, old_kb_id=old_kb_id
    )
    return await session.get(Document, document_id, populate_existing=True)


async def _maintain_current_links(
    session: AsyncSession,
    document_id: int,
    *,
    old_title: str | None = None,
    old_kb_id: int | None = None,
) -> None:
    """发布已提交后维护当前正文关系；派生失败不撤销已启用版本。"""
    from app.core import derived_counters
    from app.pipeline.wikilink import maintain_source_links

    try:
        doc = await session.get(Document, document_id, populate_existing=True)
        report = doc.clean_report if doc and isinstance(doc.clean_report, dict) else {}
        text = report.get("canonical_text")
        if not isinstance(text, str):
            text = None
            active = await session.get(IndexBuild, doc.active_build_id) if doc and doc.active_build_id else None
            titles = active.snapshot.get("link_titles") if active else None
            if isinstance(titles, list):
                # 只重放已经解析并留档的标题，不从旧原文或重叠分块猜正文。
                text = "\n\n".join(f"[[{title}]]" for title in titles if isinstance(title, str))
        await maintain_source_links(
            session, "document", document_id, text=text,
            old_title=old_title, old_kb_id=old_kb_id,
        )
    except Exception as exc:
        try:
            await session.rollback()
        except Exception:
            pass
        log.warning("文档 %s 已发布，链接维护待重试：%s", document_id, redact(str(exc)))
        derived_counters.bump("doc_links")


async def clean_build_points(session: AsyncSession, build_id: str, *, store) -> bool:
    """只能清本轮 UUID 点；启用版本以及保留用于回滚的历史版本不能清。"""
    build = await _fresh_build(session, build_id)
    active = await session.scalar(
        select(Document.active_build_id).where(Document.id == build.document_id)
    )
    if active == build.id or build.state in ("published", "retired"):
        return False
    if not build.point_ids:
        return True
    try:
        await _write_complete(store.for_space(build.space_id).delete, build.point_ids)
        return True
    except Exception:
        from app.core import derived_counters

        derived_counters.bump("stale_vector_points")
        return False


async def fail_build(session: AsyncSession, build_id: str, error: str, *, store=None, state="failed") -> None:
    await session.rollback()
    build = await _fresh_build(session, build_id)
    doc = await session.get(Document, build.document_id, populate_existing=True)
    # commit 可能已成功而响应失败；SQL 指针是事实，不能误删已启用版本。
    if doc is not None and doc.active_build_id == build.id:
        from app.cache import invalidate_all

        invalidate_all("index_publish_committed")
        await _maintain_current_links(
            session, int(doc.id),
            old_title=build.fingerprint.get("previous_link_title"),
            old_kb_id=build.fingerprint.get("previous_link_kb_id"),
        )
        return
    if build.state != "conflict":
        build.state = state
    build.error = redact(str(error))[:255]
    await session.commit()
    if store is not None:
        await clean_build_points(session, build_id, store=store)


async def recover_build(session: AsyncSession, build_id: str, *, store, embedding=None) -> str:
    """显式恢复一个中断构建；不扫描/重建真实资料，也不清理其他正在进行的轮次。"""
    build = await _fresh_build(session, build_id)
    document_id = int(build.document_id)
    doc = await session.get(Document, build.document_id, populate_existing=True)
    if doc is not None and doc.active_build_id == build.id:
        from app.cache import invalidate_all

        invalidate_all("index_recover")
        await _maintain_current_links(
            session, document_id,
            old_title=build.fingerprint.get("previous_link_title"),
            old_kb_id=build.fingerprint.get("previous_link_kb_id"),
        )
        return "published"
    if build.state in ("published", "retired"):
        return build.state
    if build.state == "validated":
        try:
            if build.vectorized:
                await check_points(session, build_id, store=store)
            if embedding is None:
                from app.vector import get_embedding_client

                embedding = get_embedding_client()
            await publish_build(session, build_id, embedding=embedding)
            return "published"
        except Exception as exc:
            await fail_build(session, build_id, str(exc), store=store)
            active = await session.scalar(
                select(Document.active_build_id).where(Document.id == document_id)
            )
            return "published" if active == build_id else "failed"
    await fail_build(session, build_id, "未完成校验的中断构建已撤下，可重新尝试。", store=store)
    return "failed"


async def rollback_build(session: AsyncSession, document_id: int, build_id: str, *, store) -> Document:
    """显式回滚至保留的完整版本；保留每轮归因块、原文件与向量。"""
    target = await _fresh_build(session, build_id)
    doc = await session.get(Document, document_id, populate_existing=True)
    if target.document_id != document_id or target.state not in ("published", "retired"):
        raise RagError("只能回滚到该文档已经发布并保留的版本。")
    if doc is None or doc.deleted_at is not None:
        raise RagError("文档不存在或在回收站中。")
    old_title = (doc.filename or "").rsplit(".", 1)[0]
    old_kb_id = int(doc.kb_id or 1)
    if target.vectorized:
        await check_points(session, target.id, store=store)
    previous = doc.active_build_id
    guard = Document.active_build_id == previous if previous else Document.active_build_id.is_(None)
    values = _values(target.snapshot)
    values.pop("kb_id", None)
    if target.snapshot.get("source_archive_path"):
        values["file_path"] = target.snapshot["source_archive_path"]
    values.update(
        active_build_id=target.id, content_build_id=target.content_build_id,
        status="ready", index_status="ready", index_error=None,
        vectorized=target.vectorized, vector_error=target.error,
    )
    result = await session.execute(
        update(Document).where(Document.id == document_id, guard).values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise RagError("另一轮构建已经发布，本次回滚未执行。")
    if previous and previous != target.id:
        await session.execute(update(IndexBuild).where(IndexBuild.id == previous).values(state="retired"))
    target.state = "published"
    await session.commit()
    from app.cache import invalidate_all

    invalidate_all("index_rollback")
    await _maintain_current_links(
        session, document_id, old_title=old_title, old_kb_id=old_kb_id
    )
    return await session.get(Document, document_id, populate_existing=True)


async def current_chunks(session: AsyncSession, document_id: int) -> list[Chunk]:
    return list((await session.execute(
        select(Chunk).join(Document, Document.id == Chunk.document_id)
        .where(Document.id == document_id, active_chunks()).order_by(Chunk.ordinal, Chunk.id)
    )).scalars().all())
