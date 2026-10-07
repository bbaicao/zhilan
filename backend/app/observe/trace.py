"""trace 器官（阶段 3 · 条目 3.3）：检索/生成的**关键事件**，为阶段 4 的 ``agent_steps`` 打样。

写两条事件（词汇表已在 ``session.events`` 里）：

| 事件 | 时机 | 载荷 |
|---|---|---|
| ``retrieval/start`` | 调检索**之前** | ``{question, filters, retrieval_id, turn}`` |
| ``retrieval/result`` | 检索返回**之后** | ``{retrieval_id, hits, from_vector, from_keyword, degraded, warning, top_chunk_id, top_ordinal}`` |

设计要点（**为 ``agent_steps`` 打样的那几条**）：

1. **一次检索 = 一对事件**（start / result 用同一个 ``retrieval_id`` 配对）——阶段 4 的
   ``tool/call`` ↔ ``tool/result`` 用同一个 ``tool_call_id`` 配对，形状是同一套；
2. **配对走 ``trace`` 列，不走 ``request_id``**：``session_events`` 上
   ``uk(chat_id, request_id)`` 是**唯一约束**（0002 迁移的幂等键），一轮里只允许一条事件带
   ``request_id``（现为 ``routing/decided``）。若这里也带，第二条起会撞约束、被
   ``safe_append_event`` 静默吞掉——那正是「事件看起来没写」的隐形坑。
   ``trace`` 列的存在意义就是「把多个事件串成一条链」（模型注释原文），正好用来串联一次检索；
3. **载荷只放可判定的量**（条数、子路来源、是否降级），**不塞正文**：事件日志是事实流，
   不是内容仓库；正文真相源在 ``chunks`` / ``messages``；
4. **写失败不炸穿**：一律经 ``safe_append_event``；检索本身早已在 ``app.retrieval`` 内降级，
   这里再失败也不影响回答。

``from_vector`` / ``from_keyword`` 的口径：从**融合后**的材料上数「哪几路给了分」
（``vector_score`` / ``keyword_score`` 非空）——这是融合结果的真实来源分布，
不需要把两路的原始候选数从检索层再透出来（那需要改 ``retrieve()`` 的返回，本条目不做）。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.retrieval.types import Material
from app.retrieval.rerank import RerankStats
from app.session import events as event_log

logger = logging.getLogger(__name__)

START = "retrieval/start"
RESULT = "retrieval/result"


from app.retrieval.types import material_identities


async def log_retrieval_start(
    db: AsyncSession,
    chat_id: int | None,
    *,
    question: str,
    filters: dict | None,
    retrieval_id: str,
    turn: int | None = None,
) -> None:
    """``retrieval/start``：**检索之前**写（拿不到 chat_id 时零 DB 触碰）。"""
    if not chat_id:
        return
    await event_log.safe_append_event(
        db,
        chat_id,
        START,
        {"question": question, "filters": filters or None, "retrieval_id": retrieval_id},
        turn=turn,
        trace=retrieval_id,
    )


async def log_retrieval_result(
    db: AsyncSession,
    chat_id: int | None,
    *,
    materials: list[Material],
    retrieval_id: str,
    warning: str | None = None,
    turn: int | None = None,
    stats: RerankStats | None = None,
) -> None:
    """``retrieval/result``：**检索之后**写；条数与子路来源都从融合后的快照数出来。"""
    if not chat_id:
        return
    items = list(materials or [])
    top = items[0] if items else None
    payload = {
        "retrieval_id": retrieval_id,
        "hits": len(items),
        "from_vector": sum(1 for item in items if item.vector_score is not None),
        "from_keyword": sum(1 for item in items if item.keyword_score is not None),
        "degraded": bool(warning),
        "warning": warning,
        "top_chunk_id": int(top.chunk_id) if top is not None else None,
        "top_ordinal": int(top.ordinal) if top is not None else None,
        "materials": material_identities(items),
        "semantic_support": None,
        "support_review": "unmeasured",
    }
    if stats is not None:
        payload.update(stats.payload())
    await event_log.safe_append_event(
        db, chat_id, RESULT, payload, turn=turn, trace=retrieval_id
    )


__all__ = ["RESULT", "START", "log_retrieval_result", "log_retrieval_start"]
