"""向量召回：当前模型空间与已发布构建在 TOP_K 前过滤，再回表核对版本。

只经 ``app/vector`` 协议访问向量库，复用应用级客户端及其空间视图。
旧模型点不推断身份；旧资料仍可走关键词路。显式注入的旧测试桩保留数字点兼容。
向量不可用返回空材料与中文提示，不影响关键词召回（I5）。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Document, IndexBuild, active_chunks
from app.retrieval.types import (
    VECTOR_TOP_K,
    Material,
    document_where,
    fanout_filters,
    includes_documents,
    matches_document,
    payload_filter,
    filter_categories,
    filter_kb_ids,
    filter_tags,
    _parse_dt,
)
from app.retrieval.keyword import READY_STATUS
from app.vector.identity import embedding_identity, space_id
from app.vector.base import PayloadRange, VectorFilter
from app.vector.payload import scope_payload

log = logging.getLogger(__name__)


def get_embedding_client(*, fake: bool = False):
    """取 embedding 客户端（模块级薄包装，便于单测注入）。"""
    from app.vector import get_embedding_client as factory

    return factory(fake=fake)


def get_vector_store(**kwargs):
    """取向量库客户端（**无参 = 应用级单例**；模块级薄包装，便于单测注入）。"""
    from app.vector import get_vector_store as factory

    return factory(**kwargs)


def _to_chunk_id(raw) -> int | None:
    """payload 分块编号（或旧测试数字点编号）→ ``chunks.id``。"""
    text = str(raw)
    return int(text) if text.isdigit() else None


def _collect_hits(
    qdrant, vector, *, limit: int, candidates: list, legacy: bool = False
) -> tuple[list[tuple[int, float, dict]], bool]:
    """逐份查询并按分块合并，保留最高分对应的 payload 供版本核对。

    ``candidates`` 由 :func:`types.fanout_filters` + :func:`types.payload_filter` 给出：
    未给 kb / 单个 kb 时**只有一份**，多值 kb 时是 N 份单值 filters。

    - 只有一份候选（单值路径）⇒ 只发**一次**查询，且**原样保留向量库给的顺序**
      ——与改前逐条相同（``回归修复`` 的修复只动多值）；
    - 多份 ⇒ 每份各发一次，同一 ``chunk_id`` 出现多次时**保留最高分**（跨库去重口径），
      合并后按「分数降序、``chunk_id`` 升序」排（与融合的并列判定同口径，两次调用结果一致）；
    - **截断在合并之后**：每份各取 ``limit`` 条，合并去重后才截到 ``limit``——这才是「先按范围
      取 top-K」；先截断再合并则等于「全库 top-K 再剔范围」，正是缺陷的成因。
    """
    best: dict[int, float] = {}
    payloads: dict[int, dict] = {}
    order: list[int] = []
    exhausted = True
    for candidate in candidates:
        raw_hits = qdrant.query(vector, limit=limit, filters=candidate) or []
        exhausted = exhausted and len(raw_hits) < limit
        for hit in raw_hits:
            payload = getattr(hit, "payload", None) or {}
            if not isinstance(payload, dict):
                continue
            chunk_id = _to_chunk_id(
                getattr(hit, "id", None) if legacy else payload.get("chunk_id")
            )
            if chunk_id is None:
                continue
            score = float(getattr(hit, "score", 0.0) or 0.0)
            if chunk_id not in best:
                order.append(chunk_id)
                best[chunk_id] = score
                payloads[chunk_id] = payload
            elif score > best[chunk_id]:
                best[chunk_id] = score
                payloads[chunk_id] = payload

    hits = [(chunk_id, best[chunk_id], payloads[chunk_id]) for chunk_id in order]
    if len(candidates) > 1:
        hits.sort(key=lambda pair: (-pair[1], pair[0]))
    return hits[:limit], exhausted


async def search_vectors(
    session: AsyncSession,
    question: str,
    filters: dict | None = None,
    limit: int = VECTOR_TOP_K,
    *,
    embedding=None,
    store=None,
) -> tuple[list[Material], str | None]:
    """向量路召回。

    返回 ``(materials, warning)``：``warning`` 非空表示**已降级**（调用方记警告即可，
    不影响关键词路的产出）。``embedding`` / ``store`` 可注入（单测不连真容器）。

    ``limit`` 是材料上限；SQL 先筛当前范围的构建，向量库按构建白名单取 TOP_K。
    旧测试桩仍沿用按 kb 分份查询后合并的兼容路径。
    """
    text = (question or "").strip()
    if not text or not includes_documents(filters):
        return [], None

    try:
        client = embedding if embedding is not None else get_embedding_client()
        vectors = await client.embed_texts([text])
    except Exception as exc:  # noqa: BLE001 —— 向量不可用一律降级，不炸穿（I5）
        return [], f"向量检索不可用（{exc.__class__.__name__}），本次仅使用关键词检索。"
    if not vectors:
        return [], "向量检索未返回向量，本次仅使用关键词检索。"

    try:
        qdrant = store if store is not None else get_vector_store()
        current_space = space_id(embedding_identity(client))
        ready = (
            await session.execute(
                select(
                    Document.active_build_id,
                    Document.kb_id,
                    Document.category,
                    Document.tags,
                    Document.created_at,
                    IndexBuild.fingerprint,
                )
                .join(IndexBuild, IndexBuild.id == Document.active_build_id)
                .where(
                    IndexBuild.document_id == Document.id,
                    IndexBuild.space_id == current_space,
                    IndexBuild.vectorized.is_(True),
                    Document.deleted_at.is_(None),
                    Document.index_status == READY_STATUS,
                    *document_where(filters),
                )
            )
        ).all()
        ready = [
            row for row in ready if matches_document(
                filters, kb_id=row[1], category=row[2], tags=row[3], created_at=row[4]
            )
        ]
        build_ids = [row[0] for row in ready]
        # ponytail: 仅显式注入的旧测试桩兼容数字点；生产客户端不读取未知模型旧点。
        legacy = (
            not build_ids
            and embedding is not None
            and store is not None
            and not callable(getattr(store, "for_space", None))
        )
        if not build_ids and not legacy:
            return [], None
        if not legacy and callable(getattr(qdrant, "for_space", None)):
            qdrant = qdrant.for_space(current_space)
        # SQL 白名单本身已经精确表达全部当前范围。旧/已改元数据的点先用该白名单，
        # 不让旧 tags/category 误挡资料；显式回填后再直接下推每个 payload 维度。
        if legacy:
            candidates = [payload_filter(item) for item in fanout_filters(filters)]
        else:
            any_of = {"build_id": build_ids, "space_id": [current_space]}
            all_of = {}
            ranges = {}
            payload_current = all(
                (row[5] or {}).get("payload_scope") == scope_payload(
                    kb_id=row[1], category=row[2], tags=row[3], created_at=row[4]
                )["scope_hash"]
                for row in ready
            )
            if payload_current:
                if kbs := filter_kb_ids(filters):
                    any_of["kb_id"] = kbs
                if categories := filter_categories(filters):
                    any_of["category"] = categories
                if tags := filter_tags(filters):
                    all_of["tags"] = tags
                after = _parse_dt((filters or {}).get("created_after"))
                before = _parse_dt((filters or {}).get("created_before"))
                if after is not None or before is not None:
                    from datetime import timezone
                    ranges["created_ts"] = PayloadRange(
                        gte=after.replace(tzinfo=timezone.utc).timestamp() if after else None,
                        lte=before.replace(tzinfo=timezone.utc).timestamp() if before else None,
                    )
            candidates = [VectorFilter(any_of=any_of, all_of=all_of, ranges=ranges)]
    except Exception as exc:  # noqa: BLE001 —— 向量库不可用同样降级
        return [], f"向量检索失败（{exc.__class__.__name__}），本次仅使用关键词检索。"
    # 回表：拿正文 / 页码 / 文档字段，并过滤软删与非 ready 的文档。
    base_stmt = (
        select(
            Chunk.id,
            Chunk.document_id,
            Chunk.content,
            Chunk.ordinal,
            Chunk.page,
            Document.filename,
            Document.category,
            Document.kb_id,
            Document.tags,
            Document.created_at,
            Document.active_build_id,
        )
        .join(Document, Document.id == Chunk.document_id)
        .where(
            active_chunks(),
            Document.deleted_at.is_(None),
            Document.index_status == READY_STATUS,
        )
    )
    if legacy:
        base_stmt = base_stmt.where(Document.active_build_id.is_(None))
    else:
        base_stmt = base_stmt.join(IndexBuild, IndexBuild.id == Document.active_build_id).where(
            Document.active_build_id.in_(build_ids),
            IndexBuild.document_id == Document.id,
            IndexBuild.space_id == current_space,
            IndexBuild.vectorized.is_(True),
        )
    target = max(1, int(limit))
    out: list[Material] = []
    # ponytail: 同一向量最多查询三轮、上限四倍 TOP_K；坏点较多时有界结束。
    # 全部范围可由 SQL 构建白名单精确下推，补取只处理坏 payload / 并发切版。
    stop_reason = "budget"
    for rounds, requested in enumerate((target, target * 2, target * 4), start=1):
        try:
            ordered, exhausted = _collect_hits(
                qdrant, vectors[0], limit=requested, candidates=candidates, legacy=legacy,
            )
        except Exception as exc:  # noqa: BLE001
            fallback = "使用已有可用材料" if out else "仅使用关键词检索"
            return out, f"向量检索失败（{exc.__class__.__name__}），本次{fallback}。"
        if not ordered:
            if exhausted:
                stop_reason = "exhausted"
                break
            continue
        rows = (await session.execute(
            base_stmt.where(Chunk.id.in_([hit[0] for hit in ordered]))
        )).all()
        by_id = {int(row[0]): row for row in rows}
        out = []
        for chunk_id, score, payload in ordered:  # 保持向量库给的相似度顺序
            row = by_id.get(chunk_id)
            if row is None:
                continue
            if not legacy and (
                payload.get("build_id") != row[10] or payload.get("space_id") != current_space
            ):
                continue
            if not matches_document(
                filters, kb_id=row[7], category=row[6], tags=row[8], created_at=row[9]
            ):
                continue
            out.append(
                Material(
                    chunk_id=int(row[0]), document_id=int(row[1]), content=row[2] or "",
                    block_ordinal=int(row[3] or 0),
                    page=int(row[4]) if row[4] is not None else None,
                    filename=row[5] or "", category=row[6] or "", kb_id=int(row[7] or 1),
                    vector_score=score,
                )
            )
            if len(out) == target:
                break
        if len(out) == target or exhausted:
            stop_reason = "filled" if len(out) == target else "exhausted"
            break
    if rounds > 1 or len(out) < target:
        log.info(
            "向量补取结束 reason=%s rounds=%s requested=%s kept=%s target=%s",
            stop_reason, rounds, requested, len(out), target,
        )
    return out, None


__all__ = ["get_embedding_client", "get_vector_store", "search_vectors"]
