"""会话事件写入/读取入口（阶段 1 · 条目 1.2；I9 决策日志的承载）。

``session_events`` 表由阶段 0 的 0002 迁移建好，其口径是「只追加的类型化事实日志」：
同一 ``(chat_id, seq)`` 至多一行、行永不 UPDATE、删除只随会话级联。
本模块是**唯一写入入口**——业务代码禁止直接 ``SessionEvent(...)`` 构造行。

事件词汇表沿用老仓库口径（老系统源码只读快照对照，不整文件搬运）：

- **表层事件**：模型可见，会产生模型消息，必须携带 ``surface_op``；
- **仅日志事件**：不产生消息，只在需要时被投影读取——本条目新增的路由决策
  ``routing/decided`` 属于这一类（它描述「怎么分路的」，不是对话内容）。

本条目只实现 I9 需要的**追加 + 按会话查询**；阶段 4.4 的恢复/投影（recover、
derive_context）不在本条范围。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, NamedTuple
from weakref import WeakKeyDictionary

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import SessionEvent

logger = logging.getLogger(__name__)

# --- 事件词汇表 ---------------------------------------------------------

# 表层事件：模型可见，必须带 surface_op
SURFACE_EVENT_TYPES: tuple[str, ...] = (
    "system/message",
    "user/message",
    "assistant/message",
    "tool/result",
)

# 仅日志事件：不产生消息。本条目新增 "routing/decided"（I9 路由决策）。
LOG_ONLY_EVENT_TYPES: tuple[str, ...] = (
    "turn/start",
    "turn/end",
    "step/start",
    "step/end",
    "retrieval/start",
    "retrieval/result",
    "rewrite/applied",
    "grade/decided",
    "compaction/start",
    "compaction/summary",
    "compaction/end",
    "citation/used",
    "citation/unresolved",
    "tool/call",
    "assistant/attempt",
    "stream/interrupted",
    "recovery/synthesized",
    "chat/title",
    # 阶段 1 · 1.2.3：一次意图路由的决策留痕（I9 可追溯）
    "routing/decided",
)

KNOWN_EVENT_TYPES: frozenset[str] = frozenset(SURFACE_EVENT_TYPES) | frozenset(
    LOG_ONLY_EVENT_TYPES
)

# 路由决策事件类型（routing 层与本模块共用同一常量，避免字面量漂移）
ROUTING_DECIDED = "routing/decided"

# 压缩三事件（阶段 4 · 条目 4.3.3）：词汇表里早就有（见上面的 LOG_ONLY_EVENT_TYPES），
# 常量放这里的原因与 ROUTING_DECIDED 一致——**字面量只许出现一处**，消费方 import 常量。
COMPACTION_START = "compaction/start"
COMPACTION_SUMMARY = "compaction/summary"
COMPACTION_END = "compaction/end"

# 事件日志开关：默认开（I9 要求可追溯）。关掉时 append 直接返回 None、不写库。
DEFAULT_EVENT_LOG_ENABLE = True

# --- 「缺行」的两条独立计数（阶段 4 · 条目 4.4，D5）------------------------
#
# 为什么是**两条**而不是把取消也折进写失败（`回归修复` 行自己写着「折进 `_FAILURE_COUNT`
# 会让语义含混；真要出口应当另设字段」）：
#
# | 计数 | 语义 | 谁在记 |
# |---|---|---|
# | ``event_write_failures``（既有，语义冻结） | **写失败**：调用了、报错了 | `safe_append_event` 的 `except Exception` |
# | ``event_incomplete``（本条新增） | **被取消 / 未写完**的缺行 | `CancelledError` 逃逸 |
#
# ``reason`` 是「这一轮为什么会有缺行」的**封闭取值域**——加值要走契约变更。
# 后两条来自契约 ``02:312`` 的同一格原文：「日志开关关闭时不计数；读取失败回落 0」。
INCOMPLETE_NONE = "none"
INCOMPLETE_CANCELLED = "cancelled"
INCOMPLETE_WRITE_FAILED = "write_failed"
INCOMPLETE_DISABLED = "disabled"
INCOMPLETE_READ_FAILED = "read_failed"

#: 五值封闭（顺序即优先级：越靠前越「需要被看见」）
INCOMPLETE_REASONS: tuple[str, ...] = (
    INCOMPLETE_WRITE_FAILED,
    INCOMPLETE_CANCELLED,
    INCOMPLETE_DISABLED,
    INCOMPLETE_READ_FAILED,
    INCOMPLETE_NONE,
)

_REASON_PRIORITY: dict[str, int] = {
    reason: len(INCOMPLETE_REASONS) - index for index, reason in enumerate(INCOMPLETE_REASONS)
}


class EventError(Exception):
    """事件层错误基类。"""


class UnknownEventTypeError(EventError):
    """事件类型不在词汇表内。"""


class SurfaceOpError(EventError):
    """表层事件必须带 surface_op；仅日志事件禁止带。"""


class RequestIdConflict(EventError):
    """同一 ``(chat_id, request_id)`` 已有事件（``回归修复`` / `020`）。

    ``session_events`` 上有 ``uk_session_events_chat_request_id``：**同会话同 ``request_id`` 只允许一行**。
    一轮里只有 ``routing/decided`` 带它；Agent/器官要把多个事件串成一条链，请用 ``trace`` 列
    （见 ``开发约定`` 与 ``app/observe/trace.py``）。

    单独成类的原因：这种冲突**重试不可能成功**（换 ``seq`` 解决不了 ``(chat_id, request_id)`` 的重复），
    原实现会白重试一次、再抛出「同一 ``(chat_id, seq)`` 连续两次冲突」——**把原因归错**，
    排障方向被带偏。现在这里**不重试**、直接给出准确原因。
    """


# 唯一约束名（0002 迁移建的幂等键）。SQLite 的报错文本里**不含约束名**，故还要按列名兜底识别。
REQUEST_ID_UNIQUE_CONSTRAINT = "uk_session_events_chat_request_id"


def _is_request_id_conflict(exc: IntegrityError) -> bool:
    """这条 ``IntegrityError`` 是不是撞了 ``(chat_id, request_id)`` 唯一约束。

    两种方言的报错文本形态不同（实测）：
    MySQL —— ``Duplicate entry '…' for key 'session_events.uk_session_events_chat_request_id'``；
    SQLite —— ``UNIQUE constraint failed: session_events.chat_id, session_events.request_id``。
    故：先认约束名，再按「含 ``request_id`` 且含 unique/duplicate」兜底。
    """
    text = str(getattr(exc, "orig", exc)).lower()
    return REQUEST_ID_UNIQUE_CONSTRAINT in text or (
        "request_id" in text and ("unique" in text or "duplicate" in text)
    )


def is_surface_event(event_type: str) -> bool:
    """该事件类型是否表层事件——**唯一控制点**。"""
    return event_type in SURFACE_EVENT_TYPES


def event_log_enabled() -> bool:
    """读事件日志开关（``settings.CHAT_EVENT_LOG_ENABLE``，缺省为开）。"""
    from app.core.config import settings

    return bool(getattr(settings, "CHAT_EVENT_LOG_ENABLE", DEFAULT_EVENT_LOG_ENABLE))


# --- 校验 ---------------------------------------------------------------


def _check_lossless(value: Any) -> None:
    """载荷必须是**无损 JSON**：拒绝 NaN/Inf 与超出 JS 安全整数的整数。"""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise EventError(f"事件载荷不是无损 JSON：{value!r}")
        return
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise EventError(f"事件载荷整数超出 JS 安全范围：{value}")
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _check_lossless(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventError(f"事件载荷键必须是字符串：{key!r}")
            _check_lossless(item)
        return
    raise EventError(f"事件载荷含不可序列化类型：{type(value).__name__}")


def _check_type(event_type: str, surface_op: str | None) -> None:
    if event_type not in KNOWN_EVENT_TYPES:
        raise UnknownEventTypeError(f"未知事件类型：{event_type!r}")
    if is_surface_event(event_type) and not surface_op:
        raise SurfaceOpError(f"表层事件必须携带 surface_op：{event_type}")
    if not is_surface_event(event_type) and surface_op:
        raise SurfaceOpError(f"仅日志事件禁止携带 surface_op：{event_type}")


# --- 写入 ---------------------------------------------------------------


async def _next_seq(db: AsyncSession, chat_id: int) -> int:
    """同会话 ``MAX(seq) + 1``；空会话为 0（从 0 起，不断号）。

    ⚠️ 本函数只提供**基线**（库内读数）；真正决定写哪个 seq 的是 :func:`_reserve_seq`
    ——它在这条读数之上叠一层**进程内预留台账**（``回归修复``）。
    """
    stmt = select(func.coalesce(func.max(SessionEvent.seq), -1) + 1).where(
        SessionEvent.chat_id == chat_id
    )
    return int((await db.execute(stmt)).scalar_one())


# ---------------------------------------------------------------------------
# 回归修复：seq 分配的**进程内预留台账**（同会话并发轮不再撞 seq、不再整轮丢事件）
# ---------------------------------------------------------------------------
#
# 缺陷本体（阶段六执行期实测，缺陷清单-2 的 ``回归修复``）：``seq = MAX(seq) + 1`` 是
# 「读改写」。同一 ``chat_id`` 的两条并发轮各自读到同一个 ``MAX(seq)`` ⇒ 同一个 seq ⇒
# 唯一约束 ``uk_session_events_chat_seq`` 把其中一条拒掉；**重试也读不到新值**
# （MySQL REPEATABLE READ 下同一事务的一致读快照不变）⇒ 两次都撞 ⇒ :class:`EventError`
# ⇒ 被 :func:`safe_append_event` 吞成一条 warning ⇒ **整轮事件**
# （``turn/start`` / ``user/message`` / ``assistant/message`` / ``turn/end``）全丢。
#
# 修法 = 照 ``回归修复`` 的同款：把「算 seq」串进**一条无 ``await`` 的临界区**
# （:func:`_reserve_seq`）——库内读数只提供「基线」，「上一次分配出去的 seq」提供「在途避让」。
# 串行时读数与改前**逐字同值**；并发时后来者拿 ``库内读数 + 2`` 那一档。
# **释放点 = 该函数返回**：不持数据库行锁、不跨请求存活；也**不写** ``SELECT … FOR UPDATE``
# （SQLite 桌面档不支持，且行锁必须跨整个请求持有——那正是本条禁止的形态）。
#
# 🔴 已登记残余窗口 1（不得含糊）：台账是**进程内**的（按 ``AsyncEngine`` 分表）
# ⇒ **多进程 / 多 worker 部署**（同一库、两个后端进程）下两个进程仍可能同时算出同一个 seq；
# 跨进程闭合需要给会话加持久化计数器列 + 出迁移（**本批 scope 外**）。
# 减轻项：每次重试都会**重读库值并推进台账**（``entry[1] + 1``）⇒ 跨进程撞上的那一方通常能在
# 3 次内自愈；但「绝不丢」这条保证只在**同进程 / 同引擎**内成立。
#
# 🔴 已登记残余窗口 2：**分配了但没落库 ⇒ 该 seq 白耗、出现空洞且不可回退**（照 120-1 同款）。
# 事件在整轮收尾前落库（``回归修复`` 已登记的「收尾前断开 ⇒ 后端什么都不落库」）⇒ 断开 /
# 本地 abort / 分配后走到不落库的出口都会白耗号；``seq`` 的**唯一性与单调性**不受影响。
# ``⛔`` 不做回收：那需要「在途号集合 + 每个出口都释放」，漏一个出口是永久多占（与现状同级），
# 错放一个出口就把唯一性窗口重新打开。
_SEQ_LEDGER: "WeakKeyDictionary[AsyncEngine, dict[int, tuple[int, int]]]" = WeakKeyDictionary()

#: 台账最多跟踪多少个会话；超限淘汰**最早插入**的一条（``dict`` 保序 ⇒ O(1)）。
#: 取值照同仓两条先例（都 512）：``api/chat_routes.py::_TURN_LEDGER_MAX_CHATS`` 与
#: ``session/compaction.py::BREAKER_MAX_TRACKED``。
_SEQ_LEDGER_MAX_CHATS = 512


def _seq_ledger_of(db: AsyncSession) -> dict[int, tuple[int, int]] | None:
    """取（或建）``db`` 所属引擎的 seq 台账；取不到引擎返回 ``None``（调用方回落纯 db 值）。

    按引擎分表（而不是一个模块级全局 dict）的理由照 ``回归修复``：① 生产态全进程
    只有一台引擎 ⇒ 等价于「按进程共享」，同会话的并发请求看得到彼此的预留；② 单测里
    每个用例一台临时引擎 ⇒ 台账**不跨用例泄漏**，既有「库内读数 + 1」的断言一条都不受影响。
    ⚠️ ``None`` 是**有意的回落支路**：取不到引擎 ⇒ 纯 db 值 ⇒ 与改前逐字同行为，
    绝不因为台账把事件写入炸穿。
    """
    bind = getattr(db, "bind", None)
    if not isinstance(bind, AsyncEngine):
        try:
            bind = db.get_bind()
        except Exception:  # noqa: BLE001 —— 取不到就当没有（调用方回落），绝不因为台账炸穿写入
            return None
    if not isinstance(bind, AsyncEngine):
        return None
    ledger = _SEQ_LEDGER.get(bind)
    if ledger is None:
        ledger = {}
        _SEQ_LEDGER[bind] = ledger
    return ledger


def _reserve_seq(
    ledger: dict[int, tuple[int, int]] | None, chat_id: int, db_next: int
) -> int:
    """算 seq + 记账，**本函数无 ``await``**（= 本条目的全部临界区）。

    事件循环只在 ``await`` 处切换协程 ⇒ 这段同步代码单进程内不可能被打断；
    **释放点 = 函数返回**（不持数据库行锁、不跨请求存活）。

    seq = ``max(库内读数, 上一次分配出去的 seq + 1)``：串行场景退化成「库内读数」
    （与改前**逐字同值**），并发场景把在途的那一格让开。

    ``(基准读数, 最大已分配 seq)`` 里的**基准读数**判「条目是否还属于当前这代人」：
    库内读数比基准**更小** ⇒ 表被清过 / SQLite 档 rowid 复用（``回归修复`` 同族）
    ⇒ 重新起算；否则旧高水位会把新会话的 seq 顶上去。产线上不构成路径（事件表只增），
    是为 SQLite 桌面档与测试清表留的安全阀。

    取不到台账（``ledger is None``）⇒ 原样返回库内读数 = 改前行为。
    """
    if ledger is None:
        return db_next
    entry = ledger.pop(chat_id, None)
    if entry is None or db_next < entry[0]:
        base, seq = db_next, db_next
    else:
        base, seq = entry[0], max(db_next, entry[1] + 1)
    ledger[chat_id] = (base, seq)
    while len(ledger) > _SEQ_LEDGER_MAX_CHATS:
        # 淘汰最早插入的一条（本函数刚写的那条在尾部 ⇒ 不会被自己淘汰）
        del ledger[next(iter(ledger))]
    return seq


async def append_event(
    db: AsyncSession,
    chat_id: int,
    event_type: str,
    data: dict,
    *,
    trace: str | None = None,
    surface_op: str | None = None,
    turn: int | None = None,
    step: int | None = None,
    request_id: str | None = None,
    tool_call_id: str | None = None,
    attempt_id: str | None = None,
) -> int | None:
    """追加一条事件，返回其 ``seq``（开关关闭时返回 ``None``，什么都没发生）。

    顺序固定：① 开关 → ② 无损 JSON 校验 → ③ 类型白名单校验 → ④ 插入。

    ``seq`` 的分配（``回归修复`` 起）：``库内 MAX(seq)+1`` 与**进程内预留台账**
    （:func:`_reserve_seq`，无 ``await`` 的临界区）取大值 ⇒ 同会话并发轮的双方在**分配阶段**
    就拿到不同 seq；``MAX+1`` 只提供基线，唯一约束仍是最后一道兜底。
    冲突（``IntegrityError``）在**同一事务内**用 SAVEPOINT 回滚后重试（**有界 3 次**），
    每次重试都**重新读库值并推进台账**——**禁止** ``db.rollback()``，那会连带回滚调用方
    未提交的落库（坑清单）。

    ``tool_call_id`` / ``attempt_id``（阶段 4 · 条目 4.1 新增）：两列由 0002 迁移建好
    （带索引 ``ix_session_events_tool_call`` / ``ix_session_events_attempt``），但此前
    **没有任何写入点**（`回归修复`）。I4 要求 ``tool/call`` 与 ``tool/result``
    共用同一个 ``tool_call_id``，所以这里补上形参——**不是**老仓库那套「从载荷同名键自动
    镜像」的机制（v2 不存在 ``_link_from_payload``，别照旧注释写）。
    """
    if not event_log_enabled():
        return None
    _check_lossless(data)
    _check_type(event_type, surface_op)

    last_error: IntegrityError | None = None
    # 台账句柄在**循环外**取一次：重试复用同一份（同一引擎的台账），不受 db 被替换影响。
    ledger = _seq_ledger_of(db)
    for attempt in (1, 2, 3):
        # 每次尝试都**重新读库值**并推进台账（跨进程撞号时让台账吸收真实库值）。
        seq = _reserve_seq(ledger, chat_id, await _next_seq(db, chat_id))
        row = SessionEvent(
            chat_id=chat_id,
            seq=seq,
            turn=turn,
            step=step,
            type=event_type,
            data=data,
            trace=trace,
            request_id=request_id,
            tool_call_id=tool_call_id,
            attempt_id=attempt_id,
        )
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
            return seq
        except IntegrityError as exc:
            if _is_request_id_conflict(exc):
                # 不重试：换 seq 解决不了 (chat_id, request_id) 的重复（回归修复 / 020）
                raise RequestIdConflict(
                    f"session_events 追加被拒：同一 (chat_id={chat_id}, request_id={request_id}) "
                    f"已有事件（type={event_type}）。一轮只允许**一条**事件带 request_id"
                    "（现为 routing/decided）；要把多个事件串成一条链请用 trace 列——"
                    "见 开发约定 与 app/observe/trace.py。"
                ) from exc
            last_error = exc
            logger.warning(
                "session_events 追加冲突（chat_id=%s type=%s seq=%s），第 %s/3 次重试",
                chat_id,
                event_type,
                seq,
                attempt,
            )
    raise EventError(
        f"session_events 追加失败（chat_id={chat_id} type={event_type}）："
        "同一 (chat_id, seq) 连续三次冲突（多半是同会话并发写抢同一个 seq，"
        "或台账跨进程未共享），请重试"
    ) from last_error


async def safe_append_event(
    db: AsyncSession,
    chat_id: int,
    event_type: str,
    data: dict,
    *,
    turn_key: tuple[int | None, int | None] | None = None,
    **kwargs: Any,
) -> int | None:
    """``append_event`` 的失败隔离版：**绝不向调用方抛异常**。

    路由决策留痕不该拖垮问答主流程（I5 绝不炸穿），所以调用方一律用本函数；
    任何异常只留一条 warning + 失败计数。

    ``turn_key``（阶段 4 · 条目 4.3）是**归属标记** ``(chat_id, turn)``：给了它，这次失败
    就会同时记进该轮的桶里，调用方就能用 :func:`failures_since` 回答「**本轮**有没有缺行」
    ——进程全局计数做不到这件事（别的会话写失败会让本轮误报，``回归修复``）。
    不给则只进全局计数（既有调用点全部如此，行为与新增前逐字一致）。

    阶段 4 · 条目 4.4 在这里补两件事（**只增语义，不改既有判定**）：

    - ``CancelledError``（``BaseException``，原先**穿过去不计数也无出口**）现在记进本轮桶、
      并**原样抛出**——取消必须继续向上传播，绝不在这里被吞；
    - 日志开关关着时显式记 ``disabled``：契约 ``02:312`` 明写「开关关闭时不计数」，
      于是「事件表零行」在开关关掉时**没有**任何出口（假绿的一种）。现在它有一个
      看得见的原因（原因值，不是计数——开关关掉是**预期**，不是失败）。
    """
    if not event_log_enabled():
        # 开关关着 ⇒ 事件表零行是**预期**（契约 02:312），不是写失败 ⇒ 不加计数，只留原因。
        # 早返回与 append_event 内部的同款判定等价（那里也是先 return None 再做任何校验）。
        _note_incomplete(turn_key, INCOMPLETE_DISABLED)
        return None
    try:
        return await append_event(db, chat_id, event_type, data, **kwargs)
    except asyncio.CancelledError:
        # 取消造成的缺行：既不是「写失败」（没报错），也不能没有出口（`回归修复`）。
        _note_incomplete(turn_key, INCOMPLETE_CANCELLED)
        raise
    except Exception as exc:  # noqa: BLE001 —— 隔离是硬要求
        note_event_write_failure(chat_id, event_type, exc, turn_key=turn_key)
        return None


def note_event_write_failure(
    chat_id: int | None,
    event_type: str,
    exc: BaseException,
    *,
    turn_key: tuple[int | None, int | None] | None = None,
) -> int:
    """记一次「事件写失败」并返回**累计值**（全局桶 + 本轮桶 + 缺行原因）。

    为什么要有这个**公开的记账口**（阶段 4 · 条目 4.4 · N1）：有一类调用方**必须自己保留
    原子性语义**，不能改用 :func:`safe_append_event`——压缩的三事件包在同一个 SAVEPOINT 里，
    要的是「要么三条全落地、要么一条都不留」。若把那里的 ``append_event`` 换成
    ``safe_append_event``，摘要写失败会被吞掉、``compaction/end`` 却照样落成 ``ok=True``
    ⇒ 变成**另一种假绿**（日志说压缩成功了，摘要其实没有）。

    ⇒ 分工是：**失败怎么处理**由调用方定（抛 / 吞），**失败怎么记账**只有这一处实现。
    ``safe_append_event`` 也是走它，所以两条路径的计数口径不可能漂移。

    ``turn_key`` 给了就同时记进该轮的桶（与 ``回归修复`` 的按轮归属同款）——
    压缩事件也是**某一个轮**里写失败的事件，没有理由让它的失败记不到轮上。
    """
    global _FAILURE_COUNT
    _FAILURE_COUNT += 1
    if turn_key is not None:
        key = _normalize_key(turn_key)
        _TURN_FAILURES[key] = _TURN_FAILURES.get(key, 0) + 1
        # 回归修复：本函数的按轮记账**自己**收口上限（不依赖下面 _note_incomplete 那一步，
        # 否则将来谁挪掉那一行，_TURN_FAILURES 的界就会**静默失效**）。
        _mark_turn_recent(key)
        _evict_oldest_turn_keys()
    _note_incomplete(turn_key, INCOMPLETE_WRITE_FAILED)
    logger.warning(
        "会话事件写入失败（已隔离，主流程不受影响）：chat_id=%s type=%s %s: %s（累计第 %s 次）",
        chat_id,
        event_type,
        exc.__class__.__name__,
        exc,
        _FAILURE_COUNT,
    )
    return _FAILURE_COUNT


_FAILURE_COUNT = 0

#: 按轮分桶的失败计数：``(chat_id, turn)`` → 次数（阶段 4 · 条目 4.3，``回归修复``）。
#: **纯进程内**：它只服务「本轮要不要报 ``events_incomplete``」，跨进程根本没有意义
#: （跨重启的语义说不清，也没有消费方）。
#: ⚠️ **有上限**（``回归修复``）：见 :data:`TURN_TRACK_MAX_KEYS` —— 超限按「最近写入序」
#: 淘汰最旧的键；被淘汰的轮若之后不再写失败，它的 ``failures_since`` 会回落 False（登记在案）。
_TURN_FAILURES: dict[tuple[int | None, int | None], int] = {}

#: 「被取消 / 未写完」的缺行计数与原因（阶段 4 · 条目 4.4，D5）。
#: ⚠️ **与 ``_TURN_FAILURES`` 同款：按 ``(chat_id, turn)`` 分桶**——写成进程内全局会让
#: 并发另一轮的原因覆盖本轮的（那正是 ``回归修复`` 刚修完的那类错）。
#: ⚠️ 与 ``_TURN_FAILURES`` 同款上限：见 :data:`TURN_TRACK_MAX_KEYS`。
_TURN_INCOMPLETE: dict[tuple[int | None, int | None], int] = {}
#: ⚠️ 同上；本桶是三个桶里**唯一**会被「开关关闭」这条常见路径灌满的（``disabled`` 只写原因）。
_TURN_REASON: dict[tuple[int | None, int | None], str] = {}

#: 三个按轮桶的**共同上限**（``回归修复``）：超过就按**最近写入序**淘汰最旧的键。
#:
#: **为什么需要**：这三个桶此前**只增不减**（生产路径零 ``pop``；清理口只有测试用的
#: :func:`reset_event_write_failures` / :func:`restore_event_write_failures`）。
#: ``CHAT_EVENT_LOG_ENABLE=false`` 时 :func:`safe_append_event` **每次调用**都走
#: :func:`_note_incomplete`(…, ``INCOMPLETE_DISABLED``) ⇒ 每轮（≈8 个事件）都在
#: ``_TURN_REASON`` 留一条**永不删除**的痕迹 ⇒ 键数量随轮数无界增长；写失败 / 取消轮另在
#: 另两个桶里留条目。
#:
#: **为什么取 512**：与同仓两条先例**同值同口径** —— ``session/compaction.py::BREAKER_MAX_TRACKED``
#: 与 ``api/chat_routes.py::_TURN_LEDGER_MAX_CHATS``（都 512）。单进程「同时在途的轮」远小于它
#: ⇒ 被淘汰的只可能是早已收口、不再需要证据的旧轮。
#:
#: **为什么淘汰序是「最近写入」而不是「纯插入」**：纯插入序会把一个「开得早、**还在写**」的轮
#: 挤出去 ⇒ 那一轮的 :func:`failures_since` 从 True 回落 False（**漏报**，正是本条要防的东西）。
#: 每次带归属的记账都把该键刷到末尾（``dict`` 保序 ⇒ O(1)）⇒ **在途轮不会被淘汰**。
#:
#: ⚠️ **残余窗口（如实登记）**：某个轮**最后一次写之后**又发生 512 个**其它键**的写入时，
#: 它的键仍会被淘汰；若该轮此前的失败也已被淘汰、之后也不再写失败，读数会回落 False
#: （**漏报**，不是误报）。触发条件 = 「一轮静默期间另有 512 个不同轮写事件」，正常负载不可达；
#: 彻底消除要引入在途登记表，**超出本条范围**（不声称「已彻底消除」）。
TURN_TRACK_MAX_KEYS = 512

#: 三个桶的**同一份引用**（只在下面两处助手与 reset/restore 里用；桶本身从不被重新绑定）。
_TURN_TRACKS: tuple[dict[Any, Any], ...] = (_TURN_FAILURES, _TURN_INCOMPLETE, _TURN_REASON)

#: 进程级总数：**只作日志**，不进任何轮的字段（按轮归属是硬要求）。
_INCOMPLETE_TOTAL = 0


def _mark_turn_recent(key: tuple[int | None, int | None]) -> None:
    """把该轮的键在**三个桶**里刷到「最近写入」的末尾（不存在的桶**不动** ⇒ 不新建键）。

    ``track[key] = track.pop(key)`` 是 O(1) 的「移到末尾」（dict 保序；重新赋值**不会**移动，
    必须先 ``pop``）。不新建键这一点是硬要求：三个桶的键集本来就可以不同
    （``disabled`` 只写 ``_TURN_REASON``、不进 ``_TURN_INCOMPLETE``），刷顺序不得改键集。
    """
    for track in _TURN_TRACKS:
        if key in track:
            track[key] = track.pop(key)


def _evict_oldest_turn_keys() -> None:
    """三个桶各自超限时淘汰**最旧**（= 最近写入序最靠前）的键 —— 一处实现、三处共用。

    逐桶**独立**淘汰，不做「按一个桶的键去同步删另外两个桶」：三个桶的键集可以不同，
    用「键集必然一致」的假设去同步删除会提前删掉**还在另一个桶里活着**的证据。
    """
    for track in _TURN_TRACKS:
        while len(track) > TURN_TRACK_MAX_KEYS:
            track.pop(next(iter(track)), None)


def _note_incomplete(
    turn_key: tuple[int | None, int | None] | None, reason: str
) -> None:
    """记一次「本轮有缺行」的事实（**按轮**；进程级总数只作日志）。

    ``reason`` 按 :data:`INCOMPLETE_REASONS` 的优先级取「更该被看见」的那个：同一轮里
    既写失败又被取消时，``write_failed`` 优先（它是真报错，取消多半是它的下游）。
    ``cancelled`` / ``write_failed`` 加计数；``disabled`` 只写原因（预期，不是失败）。
    """
    global _INCOMPLETE_TOTAL
    if turn_key is None:
        # 没有归属就不动任何轮的字段（宁可少报也不假阳性——这个信号的价值全在「报了就是真的」）。
        return
    key = _normalize_key(turn_key)
    if reason in (INCOMPLETE_CANCELLED, INCOMPLETE_WRITE_FAILED):
        _TURN_INCOMPLETE[key] = _TURN_INCOMPLETE.get(key, 0) + 1
        _INCOMPLETE_TOTAL += 1
    current = _TURN_REASON.get(key, INCOMPLETE_NONE)
    if _REASON_PRIORITY.get(reason, 0) > _REASON_PRIORITY.get(current, 0):
        _TURN_REASON[key] = reason
    # 回归修复：每次带归属的记账都把该键刷到「最近写入」末尾，并收口三个桶的上限。
    # 未超限时这两步**不改任何值、不新建键**（刷顺序只动 dict 的迭代序，消费侧全是 .get）。
    _mark_turn_recent(key)
    _evict_oldest_turn_keys()


def _normalize_key(turn_key: tuple[int | None, int | None]) -> tuple[int | None, int | None]:
    """把归属键归一成 ``(int|None, int|None)``——``1`` 与 ``"1"`` 必须落进同一个桶。"""
    raw_chat, raw_turn = turn_key
    try:
        chat = int(raw_chat) if raw_chat is not None else None
    except (TypeError, ValueError):
        chat = None
    try:
        turn = int(raw_turn) if raw_turn is not None else None
    except (TypeError, ValueError):
        turn = None
    return chat, turn


class FailureSnapshot(NamedTuple):
    """一次失败快照：全局累计 + 按轮分桶的计数（:func:`failures_since` 的输入）。

    阶段 4 · 条目 4.4 追加两个字段（**带默认值 ⇒ 旧构造与旧读法逐字不变**）：
    ``incomplete`` / ``reasons`` 是「被取消 / 未写完」的按轮桶与原因，
    供 :func:`turn_event_incomplete` 与测试的还原口使用。
    """

    total: int
    turns: dict[tuple[int | None, int | None], int]
    incomplete: dict[tuple[int | None, int | None], int] = {}
    reasons: dict[tuple[int | None, int | None], str] = {}


def failure_snapshot() -> FailureSnapshot:
    """取当前失败快照（要在**本轮开始之前**取，作为增量比较的基线）。"""
    return FailureSnapshot(
        total=_FAILURE_COUNT,
        turns=dict(_TURN_FAILURES),
        incomplete=dict(_TURN_INCOMPLETE),
        reasons=dict(_TURN_REASON),
    )


def failures_since(
    before: int | FailureSnapshot,
    *,
    turn_key: tuple[int | None, int | None] | None = None,
) -> bool:
    """自 ``before`` 以来**是否发生过**事件写入失败。

    - ``turn_key`` 给了 ⇒ 只认**该轮**的新增失败（本轮有没有缺行——``回归修复`` 的修法）；
    - 没给 ⇒ 回落进程全局增量（旧口径，行为与新增前逐字一致）。

    ``before`` 兼容 ``int``（旧的全局计数快照）与 :class:`FailureSnapshot`：两者都能算全局增量，
    只有 :class:`FailureSnapshot` 才带分桶信息。传了 ``turn_key`` 却是旧式 ``int`` 时一律返回
    ``False``——**宁可少报也不假阳性**（这个信号的价值全在「报了就是真的」）。
    """
    if isinstance(before, FailureSnapshot):
        if turn_key is None:
            return _FAILURE_COUNT > before.total
        key = _normalize_key(turn_key)
        return _TURN_FAILURES.get(key, 0) > before.turns.get(key, 0)
    if turn_key is not None:
        return False
    return _FAILURE_COUNT > int(before)


def event_write_failures() -> int:
    """进程内累计的事件写入失败次数（全局；`/api/health` 的 ``event_write_failures`` 用它）。"""
    return _FAILURE_COUNT


def note_event_cancelled(turn_key: tuple[int | None, int | None] | None) -> None:
    """实际调用已开始但停止时没有结果事件，由调用桥接登记缺行。"""
    _note_incomplete(turn_key, INCOMPLETE_CANCELLED)


def event_incomplete_total() -> int:
    """进程内累计的「被取消 / 未写完」缺行次数。

    ⚠️ **不进 ``/api/health``**（契约 ``02:312`` 只有 ``event_write_failures`` 一个键）。
    出口 = 日志 + 测试断言 + **``GET /api/agent/audit`` 的 ``counters.events.incomplete_total``**
    （阶段 5 · 条目 `5.5` 的面板化收口）。也**不进任何轮的字段**——轮的字段一律读按轮桶
    （:func:`turn_event_incomplete`），否则并发时会张冠李戴（``回归修复`` 同型）。
    """
    return _INCOMPLETE_TOTAL


def turn_event_incomplete(turn_key: tuple[int | None, int | None]) -> tuple[int, str]:
    """该轮「缺了几行、为什么缺」——``(event_incomplete, reason)``，**只读本轮桶**。

    读取自身失败时按契约 ``02:312`` 的口径回落 ``(0, "read_failed")``：观测项绝不该
    让调用方炸掉（这个函数在停止/收口的路径上被调用）。
    """
    try:
        key = _normalize_key(turn_key)
        return int(_TURN_INCOMPLETE.get(key, 0)), str(_TURN_REASON.get(key, INCOMPLETE_NONE))
    except Exception as exc:  # noqa: BLE001 —— 观测失败回落，绝不炸穿
        logger.warning("事件缺行计数读取失败（回落 read_failed）：%s: %s", exc.__class__.__name__, exc)
        return 0, INCOMPLETE_READ_FAILED


def turn_write_failures(turn_key: tuple[int | None, int | None]) -> int:
    """该轮累计的事件写入失败次数（只读，供测试与诊断）。"""
    return _TURN_FAILURES.get(_normalize_key(turn_key), 0)


def reset_event_write_failures() -> None:
    """清零失败计数与缺行计数（测试用）。**两套计数与按轮桶是同一次重置。**"""
    global _FAILURE_COUNT, _INCOMPLETE_TOTAL
    _FAILURE_COUNT = 0
    _INCOMPLETE_TOTAL = 0
    _TURN_FAILURES.clear()
    _TURN_INCOMPLETE.clear()
    _TURN_REASON.clear()


def restore_event_write_failures(snapshot: FailureSnapshot) -> None:
    """把全局计数与按轮桶**还原到某个快照**（测试用）。

    与 :func:`reset_event_write_failures` 的区别：那个是「清零」，这个是「还原」。
    给它的存在理由：某些用例会**故意**制造写失败，而既有用例断言的是
    ``event_write_failures() == 0``——不还原就会让那些用例在**整目录连跑**时假红
    （单跑却绿）。测试要用「可还原的全局状态」时，这就是那个还原口。
    """
    global _FAILURE_COUNT, _INCOMPLETE_TOTAL
    _FAILURE_COUNT = int(snapshot.total)
    _INCOMPLETE_TOTAL = sum(int(value) for value in snapshot.incomplete.values())
    _TURN_FAILURES.clear()
    _TURN_FAILURES.update(snapshot.turns)
    _TURN_INCOMPLETE.clear()
    _TURN_INCOMPLETE.update(snapshot.incomplete)
    _TURN_REASON.clear()
    _TURN_REASON.update(snapshot.reasons)


# --- 读取 ---------------------------------------------------------------


async def list_events(db: AsyncSession, chat_id: int, from_seq: int = 0) -> list[SessionEvent]:
    """按 ``seq`` 升序返回该会话事件（``from_seq`` 含）。只读，不做任何改写。"""
    stmt = (
        select(SessionEvent)
        .where(SessionEvent.chat_id == chat_id, SessionEvent.seq >= from_seq)
        .order_by(SessionEvent.seq)
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_events_by_type(
    db: AsyncSession, chat_id: int, event_type: str
) -> list[SessionEvent]:
    """按类型取该会话事件（I9 决策日志查询用），``seq`` 升序。"""
    stmt = (
        select(SessionEvent)
        .where(SessionEvent.chat_id == chat_id, SessionEvent.type == event_type)
        .order_by(SessionEvent.seq)
    )
    return list((await db.execute(stmt)).scalars().all())


__all__ = [
    "COMPACTION_END",
    "COMPACTION_START",
    "COMPACTION_SUMMARY",
    "DEFAULT_EVENT_LOG_ENABLE",
    "INCOMPLETE_CANCELLED",
    "INCOMPLETE_DISABLED",
    "INCOMPLETE_NONE",
    "INCOMPLETE_READ_FAILED",
    "INCOMPLETE_REASONS",
    "INCOMPLETE_WRITE_FAILED",
    "KNOWN_EVENT_TYPES",
    "LOG_ONLY_EVENT_TYPES",
    "REQUEST_ID_UNIQUE_CONSTRAINT",
    "ROUTING_DECIDED",
    "SURFACE_EVENT_TYPES",
    "EventError",
    "FailureSnapshot",
    "RequestIdConflict",
    "SurfaceOpError",
    "UnknownEventTypeError",
    "append_event",
    "event_incomplete_total",
    "event_log_enabled",
    "event_write_failures",
    "failure_snapshot",
    "failures_since",
    "is_surface_event",
    "list_events",
    "list_events_by_type",
    "note_event_write_failure",
    "reset_event_write_failures",
    "restore_event_write_failures",
    "safe_append_event",
    "turn_event_incomplete",
    "turn_write_failures",
]
