"""路由决策日志（阶段 1 · 条目 1.2.3，不变量 I9）。

I9 原文：**每次问答可追溯路由决策（正则/LLM/缓存/默认）与 source_type**。

落点选择：``session_events``（阶段 0 的 0002 迁移已建，零新表）。
它是「只追加的类型化事实日志」，正是决策留痕的语义；事件类型
``routing/decided`` 属仅日志事件（不产生模型消息）。阶段 4.4 的会话恢复、
阶段 5 的全量回放都能直接复用这条流。

两条纪律：

- 写库走 ``safe_append_event``（失败隔离）——**留痕失败不许拖垮问答**（I5）；
- 没有会话时（``chat_id`` 为空）**只记录不落库**：路由本身是纯函数，
  不能因为「还没有会话」就无法分路。此时决策仍可通过返回值拿到，
  由调用方在会话建立后补记（1.5 的 /chat 会先建会话再路由）。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.routing.decision import RouteDecision
from app.session import events as event_log

logger = logging.getLogger(__name__)


async def record_decision(
    db: AsyncSession | None,
    chat_id: int | None,
    decision: RouteDecision,
    *,
    request_id: str | None = None,
) -> int | None:
    """把一次路由决策写进 ``session_events``，返回 ``seq``。

    没有 db / 没有 chat_id 时跳过落库（返回 ``None``）——调用方仍持有
    ``decision`` 本身，不丢信息。任何写库异常都被 ``safe_append_event``
    吞掉并计数（I5：绝不炸穿）。
    """
    if db is None or chat_id is None:
        logger.debug(
            "跳过路由决策落库（无会话上下文）：source=%s source_type=%s",
            decision.source,
            decision.source_type,
        )
        return None
    return await event_log.safe_append_event(
        db,
        chat_id,
        event_log.ROUTING_DECIDED,
        decision.to_event_data(),
        request_id=request_id,
    )


async def load_decisions(db: AsyncSession, chat_id: int) -> list[RouteDecision]:
    """按序读回该会话的路由决策（I9「可查询」的读取侧）。"""
    rows = await event_log.list_events_by_type(db, chat_id, event_log.ROUTING_DECIDED)
    return [RouteDecision.from_event_data(row.data) for row in rows]


async def load_latest_decision(db: AsyncSession, chat_id: int) -> RouteDecision | None:
    """取该会话最后一次路由决策（排障/回看用）。"""
    decisions = await load_decisions(db, chat_id)
    return decisions[-1] if decisions else None


__all__ = ["load_decisions", "load_latest_decision", "record_decision"]
