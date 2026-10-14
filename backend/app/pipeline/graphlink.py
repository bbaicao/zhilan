"""导入资料自动连线（专项 · 2026-10-08）：文档**内容接近**边的维护链。

语义（务必与「事实关系」区分开）：这里算的边 = 两篇同库文档**代表向量**的余弦。
代表向量 = 该文档**当前有效构建**分块的等距均匀抽样均值 + L2 归一 ⇒ 边只表示
「内容接近（向量相似）」，不是事实关系，**也不参与召回**（检索/回答链路零改动）。

三条硬约束：
1. **关态逐字零影响**：`enabled()` 为假时本模块所有读写入口在**碰表之前**返回，
   `GET /api/graph` 输出与改动前逐字一致（连 `document_similarity_edges` 都不 SELECT）。
2. **零模型调用**：只读既有向量（`VectorStore.fetch_vectors`），不调 embedding / chat / rerank
   —— 禁网矩阵（`backend/tests/verify_v2.py`）对本链的要求与 GRAPHRAG 同款。
3. **失败保旧**：按库全量算 → 输入水印 → 事务内复核未变才「删旧 + 插新」；水印不符、
   **任一分块读不到向量**或任何异常一律**保留上一次完整结果**（绝不出现半套边）。
   不做单篇增量更新。

读向量的口径 = 「当前有效构建 + 对应模型空间」：`IndexBuild.id == Document.active_build_id`
（与 `app/retrieval/vector.py` 的当前点白名单同源 —— 复用构建行自己的 `space_id`/`point_ids`，
而 `content_build_id` 为空的历史资料直接跳过：既不猜模型也不重嵌入）。

为什么必须锁 `active_build_id` 而不是 `content_build_id` 那一行：复用旧正文的构建（reindex
不重切块）会把点按**新构建 id** 重写，旧内容构建行的点可能已被 `clean_build_points` 删掉
⇒ 按 `content_build_id` 去读会读到不存在的点。`content_build_id` 仍用作「有没有模型身份」的闸门。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import logging
import math
from typing import Any, Iterable, Iterator, Sequence

import anyio
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import derived_counters
from app.core.config import settings
from app.models import Document, DocumentSimilarityEdge, IndexBuild

log = logging.getLogger(__name__)


class _VectorsUnreadable(RuntimeError):
    """本轮有分块读不到向量（P1 闸门）：整库不提交 —— 半套边比旧边更坏，旧边逐字段原样保留。"""

__all__ = [
    "batch_scope",
    "enabled",
    "sample_indices",
    "mean_normalized",
    "compute_edges",
    "kb_watermark",
    "known_kb_ids",
    "recompute_kb_edges",
    "recompute_after_publish",
    "recompute_for_kb_ids",
    "delete_edges_for_document",
]


#: 批量重建深度（`ContextVar` ⇒ **任务局部**，不是进程全局开关；`batch_scope` 在 finally 恢复）。
_BATCH_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "graphlink_batch_depth", default=0,
)

#: 全库 O(n²) 计算的**有界并发**：同一事件循环里一次只跑一份（见 `_compute_guard`）。
_COMPUTE_GUARD: Any = None
_COMPUTE_GUARD_LOOP: Any = None


@contextlib.contextmanager
def batch_scope() -> Iterator[None]:
    """批量重建范围：范围内的逐篇发布钩子**暂缓**，收尾对每个受影响库重算一次。

    N 篇文档的批量重建若是每篇发布都触发一次全库 O(n²)，就成了 N+1 次计算（计划 P3 根因二）。
    范围用 `ContextVar`（任务局部）而不是进程全局开关：同一进程里并发的普通导入、批量之外的
    单篇发布照常触发；`finally` 里一定恢复 ⇒ 批次抛错或取消都不会把「暂缓」泄漏给下一次导入。
    """
    token = _BATCH_DEPTH.set(_BATCH_DEPTH.get() + 1)
    try:
        yield
    finally:
        _BATCH_DEPTH.reset(token)


def _compute_guard() -> anyio.Lock:
    """取本事件循环的全库计算锁（懒建 + 换循环重建：anyio 原语不能跨 loop 复用）。"""
    global _COMPUTE_GUARD, _COMPUTE_GUARD_LOOP
    loop = asyncio.get_running_loop()
    if _COMPUTE_GUARD is None or _COMPUTE_GUARD_LOOP is not loop:
        _COMPUTE_GUARD = anyio.Lock()
        _COMPUTE_GUARD_LOOP = loop
    return _COMPUTE_GUARD


def _probe_session(session: AsyncSession) -> AsyncSession:
    """水印复核用的**独立会话**（同绑定、自己的事务与快照）。

    为什么不复用调用方会话：在调用方会话里要拿到新快照就得 `commit()` + `begin()`，而
    `commit()`/`rollback()` 会**过期调用方的 ORM 实例**，调用方随后读属性就在 async 上下文里抛
    `MissingGreenlet`（发布钩子 `builds.py`、reindex 收尾都还持有 ORM 对象）。独立会话只读、
    即用即关：调用方事务状态与实例都原样可用，复核读到的仍是**当下**提交的构建身份。
    """
    bind = getattr(session, "bind", None)
    if bind is None:
        bind = getattr(getattr(session, "sync_session", None), "bind", None)
    if bind is None:
        raise RuntimeError("连线重算无法为水印复核取得独立会话绑定。")
    return AsyncSession(bind=bind, expire_on_commit=False)


def enabled() -> bool:
    """总开关的**唯一**读取点（配置只走 .env，不加设置页字段）。"""
    return bool(getattr(settings, "GRAPHLINK_ENABLE", False))


def sample_indices(total: int, limit: int) -> list[int]:
    """在 `range(total)` 上按序号**等距均匀抽样**至多 `limit` 个下标（升序、去重、确定性）。

    为什么是等距而不是随机：同数据两次重算必须逐条一致（验收 4.2），随机抽样做不到。
    等距的代价是可能漏掉被夹在抽样缝隙里的「恰好相似」分块 —— 代表向量只求量级正确。
    """
    total = int(total)
    limit = int(limit)
    if total <= 0 or limit <= 0:
        return []
    if total <= limit:
        return list(range(total))
    return sorted({int(index * total // limit) for index in range(limit)})


def mean_normalized(vectors: Sequence[Sequence[float]]) -> list[float] | None:
    """分块向量取均值再 L2 归一；维度不一致 / 空 / 零范数一律返回 None（不猜）。"""
    usable = [list(item) for item in vectors if item]
    if not usable:
        return None
    dims = {len(item) for item in usable}
    if len(dims) != 1 or 0 in dims:
        return None
    dim = dims.pop()
    acc = [0.0] * dim
    for item in usable:
        for index, value in enumerate(item):
            acc[index] += float(value)
    norm = math.sqrt(sum(value * value for value in acc))
    if norm <= 0:
        return None
    return [value / norm for value in acc]


def _cosine(left: Sequence[float], right: Sequence[float]) -> float | None:
    """余弦；维度不同返回 None（跨模型空间的点不是同一个空间的坐标，不许对拍）。"""
    if len(left) != len(right):
        return None
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    return float(dot)


def compute_edges(
    vectors: dict[int, list[float]],
    *,
    top_k: int,
    min_score: float,
    max_degree: int,
) -> list[tuple[int, int, float]]:
    """按「两端的 top-K 并集 + 分数阈值 + 每篇度数上限」挑边（**确定性**）。

    三步（顺序即是 §3.3 的口径）：
    1. 过滤：分数 ≥ `min_score` 的候选对（端点规范化成 `(min, max)`）。
    2. 并集：对每篇文档取它自己的分数前 `top_k` 名，取所有并集（单向相似也算）。
    3. 剪枝：候选按（分数降序, 左端点, 右端点）排序后贪心，任一端度数满 `max_degree` 就丢；
       同分按 document_id 升序 ⇒ 同一份输入任意次重算得到逐条相同的结果。
    输入向量必须已 L2 归一（见 `mean_normalized`）⇒ 点积即余弦。
    """
    ids = sorted(int(key) for key in vectors)
    top_k = max(0, int(top_k))
    max_degree = max(0, int(max_degree))
    candidates: list[tuple[float, int, int]] = []
    for index, left in enumerate(ids):
        for right in ids[index + 1:]:
            score = _cosine(vectors[left], vectors[right])
            if score is None or score < float(min_score):
                continue
            candidates.append((score, left, right))
    if not candidates or top_k <= 0 or max_degree <= 0:
        return []
    ordered = sorted(candidates, key=lambda item: (-item[0], item[1], item[2]))
    candidates = ordered
    per_anchor: dict[int, list[tuple[float, int, int]]] = {key: [] for key in ids}
    for item in candidates:
        _, left, right = item
        per_anchor[left].append(item)
        per_anchor[right].append(item)
    keep: set[tuple[float, int, int]] = set()
    for anchor in ids:
        keep.update(per_anchor[anchor][:top_k])
    degree: dict[int, int] = {key: 0 for key in ids}
    picked: list[tuple[int, int, float]] = []
    for score, left, right in sorted(keep, key=lambda item: (-item[0], item[1], item[2])):
        if degree[left] >= max_degree or degree[right] >= max_degree:
            continue
        degree[left] += 1
        degree[right] += 1
        picked.append((left, right, score))
    return picked


def kb_watermark(rows: Iterable[Any]) -> str:
    """输入水印 = sha256(排序后的 `(document_id, active_build_id, space_id)`，限该库)。

    它回答的是「这一轮读到的输入，跟我准备提交时看到的输入，是同一份吗」。
    按 `document_id` 升序归一 ⇒ 行序不影响结果。

    身份里必须有 `active_build_id`（P2-A）：同一模型空间内重建（复用旧正文、换分块）时
    `content_build_id` 与 `space_id` 都可能**逐字不变**，只有 `active_build_id` 会变；
    不带它 ⇒ 水印相等、旧边被当成新结果提交（旧边残留）。行 = `_kb_rows` 的
    `(id, content_build_id, space_id, point_ids, active_build_id)`。
    """
    parts = []
    for row in rows:
        doc_id = int(row[0])
        active_build_id = row[4] or ""
        space = row[2] or ""
        parts.append(f"{doc_id}\t{active_build_id}\t{space}")
    return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()


async def _kb_rows(session: AsyncSession, kb_id: int) -> list[Any]:
    """该库**未软删**文档的 `(id, content_build_id, space_id, point_ids, active_build_id)`
    （按 document_id 升序）；后三列来自该文档当前有效构建的 JOIN。

    ⚠️ 列序是外部契约：`kb_watermark` / `_read_groups` / 验收用例都按下标读，新列只能追加。
    """
    return list((await session.execute(
        select(
            Document.id,
            Document.content_build_id,
            IndexBuild.space_id,
            IndexBuild.point_ids,
            Document.active_build_id,
        )
        .outerjoin(IndexBuild, IndexBuild.id == Document.active_build_id)
        .where(Document.kb_id == int(kb_id), Document.deleted_at.is_(None))
        .order_by(Document.id)
    )).all())


def _read_groups(
    rows: Sequence[Any], store: Any, limit: int,
) -> tuple[dict[str, dict[int, list[float]]], int]:
    """同步慢路径：读代表向量、按 space 分组；返回分组与「读不到的分块数」。

    **在 worker 线程里跑**（见 `_read_representatives`）：逐篇 fetch_vectors 的等待不该占住
    导入期的事件循环。这里只碰向量库与纯函数，不碰 SQL/事务。
    """
    groups: dict[str, dict[int, list[float]]] = {}
    missing = 0
    for row in rows:
        points = list(row[3] or [])
        picked = [points[index] for index in sample_indices(len(points), limit)]
        if not picked:
            missing += 1
            continue
        scoped = store
        if callable(getattr(store, "for_space", None)):
            scoped = store.for_space(row[2])
        try:
            got = scoped.fetch_vectors(picked)
        except Exception as exc:  # noqa: BLE001 —— 读向量失败只丢这一篇，不炸整库
            log.warning("连线读向量失败 document_id=%s: %s", row[0], exc)
            missing += len(picked)
            continue
        missing += len(picked) - len(got)
        representative = mean_normalized(list(got.values()))
        if representative is None:
            continue
        groups.setdefault(str(row[2]), {})[int(row[0])] = representative
    return groups, missing


async def _read_representatives(
    session: AsyncSession, rows: Sequence[Any], store: Any,
) -> tuple[dict[str, dict[int, list[float]]], int]:
    """慢路径（读向量）：返回 `{space_id: {document_id: 单位向量}}` 与「读不到的分块数」。

    读向量一律经 `anyio.to_thread.run_sync` 丢进 worker 线程（P3：事件循环不被 Qdrant 读取
    占住；SQL 仍在 async 侧）。单独成函数是为了给验收用例留**注入点**：水印拦截用例在这里
    插入一篇新文档，模拟「计算期间有人发布」（第二次水印读到它 ⇒ 本次不提交）。
    """
    limit = int(getattr(settings, "GRAPHLINK_MAX_CHUNKS_PER_DOC", 64) or 0)
    return await anyio.to_thread.run_sync(_read_groups, rows, store, limit)


async def known_kb_ids(session: AsyncSession) -> list[int]:
    """缺省库清单 = 有**未软删**文档的库 ∪ 表里已有旧边的库（按 id 升序）。

    两个来源都要：前者是待算的输入，后者覆盖「文档已全被删、旧边还留着」的库
    —— 那种库也得重算一遍（结果是合法的 0 条边，等于把陈旧边清掉）。
    **会读新表**，调用方必须先过 `enabled()`。
    """
    live = select(Document.kb_id).where(Document.deleted_at.is_(None)).distinct()
    linked = select(DocumentSimilarityEdge.kb_id).distinct()
    found = {
        int(row[0])
        for statement in (live, linked)
        for row in (await session.execute(statement)).all()
        if row[0] is not None
    }
    return sorted(found)


def _score_spaces(
    groups: dict[str, dict[int, list[float]]],
    top_k: int,
    min_score: float,
    max_degree: int,
) -> list[tuple[str, int, int, float, int]]:
    """同步纯计算：逐空间算边，返回 `(space_id, left, right, score, dim)`。

    **在 worker 线程里跑**（`recompute_kb_edges` 经 `anyio.to_thread.run_sync` 调它）：
    O(n²) 余弦是导入期最大的一段纯 CPU，不能占住事件循环（P3）。`compute_edges` 仍按模块
    全局名解析 ⇒ 既有用例的 monkeypatch 注入点不受影响。维度不一致的空间不拍对。
    """
    scored: list[tuple[str, int, int, float, int]] = []
    for space in sorted(groups):
        bucket = groups[space]
        length = {len(item) for item in bucket.values()}
        if len(length) != 1:
            continue
        dim = length.pop()
        for left, right, score in compute_edges(
            bucket, top_k=top_k, min_score=min_score, max_degree=max_degree,
        ):
            scored.append((space, int(left), int(right), score, int(dim)))
    return scored


async def recompute_kb_edges(
    session: AsyncSession, kb_id: int, *, store: Any = None, dry_run: bool = False,
) -> dict:
    """按库**全量**重算相似边并原子替换；返回状态字典（供日志 / 脚本 / 用例断言）。

    status：`disabled`（门未开，未碰表）/ `written`（已提交，含合法的 0 条边）/
    `dry`（试算：算完就回，一行不删不插，给维护脚本 `--dry-run` 用）/
    `oversize`（超出 MAX_DOCS_PER_KB，跳过且旧边保留）/ `stale`（水印不符 ⇒ 本次不提交）/
    `failed`（读不到向量或异常 ⇒ 回滚/不提交，旧边逐字段保留）。后三种都会 bump `similar_links`。
    """
    kb = int(kb_id)
    if not enabled():
        return {"status": "disabled", "kb_id": kb, "edges": 0, "documents": 0, "missing": 0}
    rows = await _kb_rows(session, kb)
    watermark = kb_watermark(rows)
    eligible = [row for row in rows if row[1] and row[2] and row[3]]
    limit_docs = int(getattr(settings, "GRAPHLINK_MAX_DOCS_PER_KB", 2000) or 0)
    if len(eligible) > limit_docs > 0:
        log.warning(
            "知识库 %s 文档数 %d 超出连线上限 %d，本次跳过（旧边保留）。",
            kb, len(eligible), limit_docs,
        )
        derived_counters.bump("similar_links")
        return {"status": "oversize", "kb_id": kb, "edges": 0, "documents": len(eligible), "missing": 0}
    if store is None:
        from app.vector import get_vector_store

        store = get_vector_store()
    wrote = False  # 只有进过写阶段才允许动调用方事务状态（见 except 分支）
    try:
        groups, missing = await _read_representatives(session, eligible, store)
        measured = sum(len(bucket) for bucket in groups.values())
        if missing and not dry_run:
            # P1（失败保旧）：只要有一个分块读不到向量，本轮**整库不提交** —— 旧边的行数、
            # 两端、score、space_id、dim 一个字段都不许动（半套边比旧边更坏）。
            # 空库（missing=0，合法地 0 条边）不走这条路；试算本来一行不写 ⇒ 不进本闸门。
            raise _VectorsUnreadable(
                f"知识库 {kb} 有 {missing} 个分块读不到向量，本次不提交（旧边保留）。"
            )
        async with _compute_guard():  # 有界并发：一次只让一份全库 O(n²) 占线程
            scored = await anyio.to_thread.run_sync(
                _score_spaces,
                groups,
                int(getattr(settings, "GRAPHLINK_TOP_K", 3) or 0),
                float(getattr(settings, "GRAPHLINK_MIN_SCORE", 0.65) or 0.0),
                int(getattr(settings, "GRAPHLINK_MAX_DEGREE", 3) or 0),
            )
        if dry_run:
            # 试算：连「水印不符」的告警都不发（那是一次真提交才会遇到的判定），一行不写。
            return {
                "status": "dry",
                "kb_id": kb,
                "edges": len(scored),
                "documents": measured,
                "missing": missing,
            }
        # P2-A：身份复核用的是**独立会话的新快照**（同绑定的第二个连接）—— 复核读到的构建身份
        # 就是随后被删写的那一份；`active_build_id` 也在身份里，所以同一 space 内重建（正文没变、
        # 分块全换）同样判 stale。复核**不碰调用方 session**：不 commit/begin/rollback（那会过期
        # 调用方的 ORM 实例 ⇒ MissingGreenlet），只有真的进写阶段才可能回滚调用方事务。
        probe = _probe_session(session)
        try:
            fresh = kb_watermark(await _kb_rows(probe, kb))
        finally:
            await probe.close()
        if fresh != watermark:
            log.info("知识库 %s 在连线计算期间发生变化，本次结果不提交（旧边保留）。", kb)
            derived_counters.bump("similar_links")
            return {"status": "stale", "kb_id": kb, "edges": 0, "documents": measured, "missing": missing}
        wrote = True
        await session.execute(
            delete(DocumentSimilarityEdge).where(DocumentSimilarityEdge.kb_id == kb)
        )
        session.add_all([
            DocumentSimilarityEdge(
                kb_id=kb,
                source_document_id=left,
                target_document_id=right,
                score=score,
                space_id=space,
                dim=dim,
            )
            for space, left, right, score, dim in scored
        ])
        await session.commit()
    except Exception as exc:  # noqa: BLE001 —— 派生链失败不炸上游；旧边原样保留
        if wrote:
            # 真的进过写阶段（含删旧之前的那一刻）：必须丢掉半套写入。这只发生在写/提交路径上，
            # 读向量失败、复核失败、计算异常都不再碰调用方事务状态。
            try:
                await session.rollback()
            except Exception:
                pass
        log.warning("知识库 %s 连线重算失败，保留上一次结果：%s", kb, exc)
        derived_counters.bump("similar_links")
        return {"status": "failed", "kb_id": kb, "edges": 0, "documents": 0, "missing": 0}
    if missing:
        derived_counters.bump("similar_links", missing)
    return {
        "status": "written",
        "kb_id": kb,
        "edges": len(scored),
        "documents": measured,
        "missing": missing,
    }


async def recompute_for_kb_ids(
    session: AsyncSession, kb_ids: Iterable[int], *, store: Any = None,
) -> list[dict]:
    """对若干库逐个全量重算（reindex 收尾用；逐个独立，单个库失败不影响后面的库）。"""
    if not enabled():
        return []
    results = []
    for kb_id in sorted({int(item) for item in kb_ids}):
        results.append(await recompute_kb_edges(session, kb_id, store=store))
    return results


async def recompute_after_publish(
    session: AsyncSession, document_id: int, *, store: Any = None,
) -> dict:
    """发布后钩子：重算该文档所属库的相似边；**失败不回滚已发布的索引**。

    `app/pipeline/builds.py` 的发布收尾在提交之后调它 ⇒ 这里再脏也不能把发布带崩
    （与 `_maintain_current_links` 同款隔离：不抛、记日志、失败计数）。
    """
    if not enabled():
        return {"status": "disabled", "kb_id": 0, "edges": 0, "documents": 0, "missing": 0}
    if _BATCH_DEPTH.get() > 0:
        # 批量重建范围内（P3②）：逐篇钩子暂缓，收尾由 `recompute_for_kb_ids` 每库算一次。
        # 连 doc 都不读 —— 批次内 N 次调用一条 SQL 都不产生。
        return {"status": "deferred", "kb_id": 0, "edges": 0, "documents": 0, "missing": 0}
    try:
        doc = await session.get(Document, int(document_id), populate_existing=True)
        if doc is None:
            return {"status": "disabled", "kb_id": 0, "edges": 0, "documents": 0, "missing": 0}
        return await recompute_kb_edges(session, int(doc.kb_id or 1), store=store)
    except Exception as exc:  # noqa: BLE001 —— 派生边维护绝不取消已发布的构建
        try:
            await session.rollback()
        except Exception:
            pass
        log.warning("文档 %s 发布后连线维护失败：%s", document_id, exc)
        derived_counters.bump("similar_links")
        return {"status": "failed", "kb_id": 0, "edges": 0, "documents": 0, "missing": 0}


async def delete_edges_for_document(session: AsyncSession, document_id: int) -> int:
    """硬删文档时清掉**两端**边；返回删除行数。

    门未开直接返回 0（**连表都不碰** —— 关态逐字零影响优先于孤儿边）：
    关态下这张表既不会被写、也不会被读，残留行对 `GET /api/graph` 无影响；
    开态时 ingest 的 purge 在提交后调它（同 wiki 边清理的位置）。
    """
    if not enabled():
        return 0
    doc_id = int(document_id)
    try:
        result = await session.execute(
            delete(DocumentSimilarityEdge).where(
                or_(
                    DocumentSimilarityEdge.source_document_id == doc_id,
                    DocumentSimilarityEdge.target_document_id == doc_id,
                )
            )
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 —— 清边失败不挡硬删本身
        try:
            await session.rollback()
        except Exception:
            pass
        log.warning("清除文档 %s 的相似边失败：%s", doc_id, exc)
        derived_counters.bump("similar_links")
        return 0
    return int(getattr(result, "rowcount", 0) or 0)
