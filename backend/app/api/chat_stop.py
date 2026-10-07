"""流式停止注册表（阶段 3 · 条目 3.4 · 契约 §1.1 的 ``POST /api/chat/stop``）。

进程内一张表：``request_id`` → ``asyncio.Event``。

- 客户端发起流式提问时带上自己生成的 ``request_id``，服务端**在流开始前登记**、在流结束时
  （``finally``）**注销**；
- 用户点停止 → ``POST /api/chat/stop`` 只把这枚 Event **置位**，真正的收尾由流内检查点完成：
  「取下一个增量」与「停止信号」**赛跑**（``asyncio.wait(..., FIRST_COMPLETED)``）⇒ 厂商静默期、
  检索期也能即时停下，而不是等下一个增量到达；
- **幂等友好**：``request_id`` 不在册（已结束 / 从未存在）返回 ``False`` 而不是报错——
  用户点停止的瞬间回答恰好收尾，是正常竞态。

**单进程口径**：桌面版与开发态都是单进程 uvicorn（契约 §5 桌面壳：单进程内起 uvicorn），
不需要跨进程广播；换成多 worker 部署时这张表会退化成「只在本 worker 生效」，届时再上 Redis 广播。
"""

from __future__ import annotations

import asyncio

# request_id → 停止信号
_events: dict[str, asyncio.Event] = {}


def register(request_id: str | None) -> asyncio.Event | None:
    """登记一次流；``request_id`` 为空则不登记（调用方照常跑，只是这一轮停不了）。

    同一 ``request_id`` 被**覆盖登记**（客户端重试 / 上层复用 id）时，先置位上一轮的
    ``Event``：旧登记一旦被覆盖就再没有任何入口能置位它，那一轮只能跑到自然结束
    （回归修复）；置位后它照常走自己的停止检查点，**本轮不受影响**。
    """
    if not request_id:
        return None
    event = asyncio.Event()
    old = _events.get(request_id)
    if old is not None:
        old.set()
    _events[request_id] = event
    return event


def unregister(request_id: str | None, event: asyncio.Event | None = None) -> None:
    """注销登记（流结束时**必调**，放 ``finally``）。

    只在「表里那枚仍是我这一轮的」时才删除：同一 ``request_id`` 被后一轮覆盖登记时，
    先结束的那一轮**不许**把后一轮的信号误删（否则后一轮就永远停不下来）。
    覆盖登记时旧 ``Event`` 已由 ``register`` 置位（回归修复），这里的身份守卫照旧生效。
    """
    if not request_id:
        return
    if event is not None and _events.get(request_id) is not event:
        return
    _events.pop(request_id, None)


def request_stop(request_id: str) -> bool:
    """置位停止信号；``request_id`` 不在册返回 ``False``（不报错）。"""
    event = _events.get(request_id)
    if event is None:
        return False
    event.set()
    return True


def is_registered(request_id: str) -> bool:
    """该 ``request_id`` 当前是否有在跑的流（测试与排障用）。"""
    return request_id in _events


def active_count() -> int:
    """当前登记在册的流数量（测试用；正常应回到 0）。"""
    return len(_events)


def reset() -> None:
    """清空注册表（**仅测试用**：用例之间隔离）。"""
    _events.clear()


__all__ = [
    "active_count",
    "is_registered",
    "register",
    "request_stop",
    "reset",
    "unregister",
]
