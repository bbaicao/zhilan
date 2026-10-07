"""用量写入层（任务清单 27 / 阶段 4 · 条目 4.5.4）：``usage_events`` 的**唯一写入入口**。

背景（本轮实测）：``usage_events`` 在 v2 里**全仓零写入点** ⇒ ``GET /api/usage`` 与
``/api/usage/export`` 即使接得通也永远拿不到任何数据（与 ``回归修复`` 同型：
「端点接得通 ≠ 有数据」）。本模块把写入点补上，口径**逐条沿用老基线**
``<legacy-repo>\\backend\\app\\rag\\usage.py``：

1. **实际尝试**：provider send 记录成功、失败、超时和停止，重试每次独立一行；
   调用数不等于计费金额；未发生 send 的替身或缓存命中不得虚构调用；
2. **拿不到 tokens 就写 NULL**：厂商响应里给了才写，拿不到就是 ``None``
   （典型：流式对话不加 ``stream_options``——**请求体逐字不变**是既有口径，
   不能为了拿 usage 去改请求体）。界面按「未知」展示（``unknown_calls`` 计数）；
3. **绝不抛**：落库失败只 ``logger.warning``，绝不影响主流程；
4. 共享 SDK 与原生 embed/rerank HTTP 观测记录真实尝试，role 区分回答及辅助调用；
   最终答案收口不重复写入。

**两种落库形态**（同一个口径，按调用点手里有没有会话选）：

- :func:`record_usage` —— **fire-and-forget**（老基线同款）：落库在后台任务里做
  （``asyncio`` 只留弱引用，故这里攥着强引用），没有事件循环时才同步落一次。
  embed / rerank 的出网点手里没有会话，走这条；传了 ``db`` 就用**它所属的引擎**新开独立
  会话（``new_independent_session``，条目 4.4 引入的同一套解析）——生产态与请求同库，
  单测里因此落在**测试库**而不是开发库；没传就回落进程级 ``SessionLocal``。
- :func:`record_usage_on` —— 写进**调用方那个会话**（SAVEPOINT 隔离，不额外提交、不多开
  连接）的兼容入口；本轮回答收口已由 provider send 观测记账，不再调用它。

⚠️ **本模块只写 ``usage_events``**：不 import 上层（api / retrieval / vector），
分包方向是「上层 → 本层 → db/core」，因此三个调用点都能安全地 import 它。
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import derived_counters
from app.db.session import SessionLocal, new_independent_session
from app.models import UsageEvent

logger = logging.getLogger(__name__)

#: 四类用量（聚合时固定按这个顺序给全，缺的类型补零，界面不用猜键）。
#: ``vision``（回归修复，2026-09-27 起）：**带图轮**的回答调用记 ``vision``——
#: 此前恒 0（旧口径「图片只随消息落库、没有走视觉模型的调用路径」已随图文问答
#: 实装作废），不为它编造数据的纪律不变：只记**真实发生**的调用。
KINDS: tuple[str, ...] = ("chat", "vision", "embed", "rerank")

#: 在途落库任务的强引用（asyncio 只留弱引用，不攥着可能中途被 GC）。
_pending: set[asyncio.Task] = set()


def _open_session(db: AsyncSession | None) -> AsyncSession:
    """打开一个**专用**会话：给了调用方会话就绑它的引擎，否则回落进程级工厂。

    为什么要独立会话（而不是直接用调用方那个）：写入是 **fire-and-forget**，
    与请求事务**共用连接**会在驱动层撞「同一连接并发使用」；更要紧的是
    「只记成功」要求写入**绝不能**把请求事务带崩——独立会话失败只影响这一条记录。
    """
    if db is not None:
        session = new_independent_session(bound_to=db)
        if session is not None:
            return session
    return SessionLocal()


async def _insert_event(
    session: AsyncSession,
    kind: str,
    provider: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    calls: int,
    meta: dict | None,
) -> None:
    """真正落库的那一下：任何失败只 warning，绝不往上传。"""
    try:
        async with session:
            session.add(
                UsageEvent(
                    kind=str(kind or "")[:20],
                    provider=str(provider or "")[:50],
                    model=str(model or "")[:100],
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    calls=max(1, int(calls or 1)),
                    meta=meta,
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 —— 记录失败绝不影响主流程
        logger.warning(
            "用量记录失败（不影响主流程）：kind=%s model=%s：%s", kind, model, exc
        )
        # `回归修复`：派生失败必须可计数（否则它只活在一条没人看的日志里；
        # 叠加 `回归修复`「冻结态应用层日志不进控制台」= 等于不可见）。S6。
        derived_counters.bump("usage_events")


def record_usage(
    kind: str,
    provider: str = "",
    model: str = "",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    calls: int = 1,
    meta: dict | None = None,
    db: AsyncSession | None = None,
) -> None:
    """记一条用量（fire-and-forget）：同步返回，落库在后台任务里做，**绝不抛**。

    ``tokens`` 三兄弟传 ``None`` 即「未知」口径（不是 0——0 会被算进合计）。
    ``db`` 可选：传了就把记录写进**它所属引擎**的库（生产态 = 请求同一个库）。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        # 没有事件循环（纯同步上下文，如脚本 / 线程）：直接同步落一次。
        # 建会话与落库都兜在 try 里，**绝不抛**。
        try:
            asyncio.run(
                _insert_event(
                    _open_session(db),
                    kind,
                    provider,
                    model,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    calls,
                    meta,
                )
            )
        except Exception as exc:  # noqa: BLE001 —— 绝不抛
            logger.warning(
                "用量记录未执行（无可用事件循环）：kind=%s model=%s：%s", kind, model, exc
            )
            # `回归修复`：同口径可计数（S7）。
            derived_counters.bump("usage_events")
        return

    try:
        session = _open_session(db)
    except Exception as exc:  # noqa: BLE001 —— 连会话都建不出来时也绝不抛
        logger.warning("用量记录未执行（会话不可用）：kind=%s model=%s：%s", kind, model, exc)
        # `回归修复`：同口径可计数（S8）。
        derived_counters.bump("usage_events")
        return

    task = loop.create_task(
        _insert_event(
            session,
            kind,
            provider,
            model,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            calls,
            meta,
        )
    )
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def record_usage_on(
    db: AsyncSession,
    kind: str,
    provider: str = "",
    model: str = "",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    calls: int = 1,
    meta: dict | None = None,
) -> None:
    """兼容入口：把一条用量写进调用方会话，不新开连接、不建后台任务。

    **事务语义**：用 **SAVEPOINT**（``begin_nested``）隔离——写入失败只回滚这一条，
    绝不污染调用方事务；这一行随调用方后续 ``commit`` 一起落库，不额外提交。
    本轮产品回答收口不再调用此入口：shared provider send 已逐次记录真实尝试，
    再在收口写入会重复计数。此函数保留兼容用途。

    回答收口写入的时序风险：
    单独的会话 + 额外提交、或后台任务，都会让紧随其后的 ``/api/chat/stream`` 请求慢过
    「先登记停止信号」的 50ms 窗口，把 ``test_chat_stream_events`` 的停止用例打红。
    该历史记录不代表当前 provider send 观测路径的验证结果。

    失败只记日志、绝不抛；``tokens`` 拿不到就是 NULL。当前 provider send 观测与原生
    embed/rerank HTTP 观测走 :func:`record_usage`，使用独立会话异步写入。
    """
    try:
        async with db.begin_nested():
            db.add(
                UsageEvent(
                    kind=str(kind or "")[:20],
                    provider=str(provider or "")[:50],
                    model=str(model or "")[:100],
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    calls=max(1, int(calls or 1)),
                    meta=meta,
                )
            )
    except Exception as exc:  # noqa: BLE001 —— 记录失败绝不影响主流程
        logger.warning("用量记录失败（不影响主流程）：kind=%s model=%s：%s", kind, model, exc)
        # `回归修复`：同口径可计数（S9；本函数 = 回答收口路径，失败同样只隔离这一条）。
        derived_counters.bump("usage_events")


async def drain_pending() -> None:
    """等所有在途落库任务跑完（测试与关停收尾用；生产路径**不**调用）。

    生产路径不能等：它是 fire-and-forget，等一等就把「不阻塞主流程」那条口径丢了。
    """
    for _ in range(50):
        if not _pending:
            return
        await asyncio.gather(*list(_pending), return_exceptions=True)
        await asyncio.sleep(0)


__all__ = ["KINDS", "drain_pending", "record_usage", "record_usage_on"]
