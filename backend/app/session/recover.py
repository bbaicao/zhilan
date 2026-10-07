"""崩溃恢复：补平工具配对缺口 + 合成**可读结论**（阶段 4 · 条目 4.4）。

**这是老基线 ``<legacy-repo>\\backend\\app\\rag\\recover.py`` 的口径移植**（行为基线，逐条照搬语义）：
真相源 §7.4「崩溃修复（模型可见效果）」的两种情形——

| 情形 | 触发 | 合成结论 |
|---|---|---|
| A | 有持久 ``tool/call`` 但没有同 id 的 ``tool/result`` | ``TOOL_OUTCOME_UNKNOWN`` |
| B | assistant 声明要用工具，但压根**没有**发 ``tool/call`` | ``TOOL_NOT_STARTED`` |

落地口径（对齐统筹 §4.2 不变量 1「库内零改写」与不变量 6「恢复流程绝不删除已提交事件」）：

1. **只追加**：恢复通过事件层 ``append_event`` 写事件，**绝不** UPDATE / DELETE 既有事件行或
   ``messages`` 行——「当时发生了什么」只在日志里补一句结论，不改写历史（断言 4）；
2. **成对补齐**：除留痕用的 ``recovery/synthesized``（仅日志事件）之外，还会补一条**表层事件**
   ``tool/result``——只有表层事件会被投影进下一轮的模型历史，「模型知道上次那个工具没结果」
   这句话才落得了地。情形 B 连合成的 ``tool/call`` 一起补（否则出现「有 result 没有 call」的
   倒挂）；**所有恢复载荷都带 ``synthesized: True``**（读日志的人一眼能分出「真执行的」与
   「合成的」——老基线原注释）；
3. **幂等**：同一 ``(tool_call_id, kind)`` 只合成一次（先读既有 ``recovery/synthesized`` 再补）；
4. **只认带 ``tool_call_id`` 的痕迹**：阶段 4 之前的 ``tool/call`` 载荷没有这个键，
   若把它们都判成「有 call 无 result」，会给每一个历史 Agent 轮次都合成一条「结论未知」，
   那是**假事实**。故无 id 的调用与无 id 的步骤一律跳过，并如实登记这条局限
   （「无法区分『当时日志开关是关的』与『数据早于阶段 4』」）；
5. **不改 ``messages``**：界面所需的 ``recovered: true`` 由 :func:`recovered_message_ids`
   从事件里**现算**（不是列、也不吃 :func:`ensure_balanced` 的返回值——那个只在「本次新合成」
   时有内容，拿它当判据会让提示一闪就没，老基线注释明写）；
6. **挂载点口径（N2′，2026-09-19 核验代理 B3 打回后落在这里）**：结论要挂回**哪条消息**由
   ``step_owner.get(call_id) or last_message_id`` 决定——
   ``step_owner`` 是从 ``agent_trace`` 扫出的「哪条 assistant 消息发起了这次调用」；
   扫不到时**退化为该会话最后一条消息**。为什么必须退化：``「工具执行中杀进程」``
   这个**唯一可达场景**下，assistant 消息**根本还没落库**（它只在轮末写），于是结论无处可挂、
   界面一个字都不提示；退化为挂在那一轮的消息上，界面才能说「这一轮的工具结果不明」。
   **幂等保证这条退化只在首次恢复时生效**（补完就有 result，之后不再重算）；
7. **写入用调用方的会话**（v2 的**有意差异**，如实登记）：老基线 ``ensure_balanced``
   写入走**自开会话**、明确不提交调用方的 session；v2 的 ``safe_append_event``
   **自己做 SAVEPOINT、不 commit**（提交归调用方），所以不存在「提前提交调用方未提交的落库」
   这个风险，直接沿用调用方会话即可。调用方若要让新事件落库，自己 ``commit()``
   （``GET /api/chats/{id}`` 就是这么做的）。这条差别与 ``回归修复`` 同源，
   「Agent 用独立 session」的根治在条目 ``4.4.4``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message, SessionEvent
from app.session import events as event_log

logger = logging.getLogger(__name__)

# 两种合成结论的类型名（对齐真相源 §7.4，便于逐行对照）
TOOL_OUTCOME_UNKNOWN = "TOOL_OUTCOME_UNKNOWN"
TOOL_NOT_STARTED = "TOOL_NOT_STARTED"

# 留痕事件（仅日志）与模型可见的补写事件（表层）
RECOVERY_EVENT = "recovery/synthesized"
SURFACE_RESULT_EVENT = "tool/result"
CALL_EVENT = "tool/call"

__all__ = [
    "CALL_EVENT",
    "RECOVERY_EVENT",
    "SURFACE_RESULT_EVENT",
    "TOOL_NOT_STARTED",
    "TOOL_OUTCOME_UNKNOWN",
    "Synthesized",
    "ensure_balanced",
    "not_started_text",
    "outcome_unknown_text",
    "recovered_message_ids",
]


# ---------------------------------------------------------------------------
# 文案（**逐字沿用老基线**；老基线自己的注释写着「意译，非照抄 DSH」）
# 语义必须保留：结论未知 / 只重试只读或幂等 / 有副作用先核实 / 如仍需要请重试
# ---------------------------------------------------------------------------


def _tool_label(tool: str | None) -> str:
    return f"「{tool}」" if tool else "那个工具"


def outcome_unknown_text(tool: str | None = None) -> str:
    """情形 A：工具已经开始但没留下结果——注意「结论**未知**」而不是「没做」。"""
    return (
        f"{_tool_label(tool)}已经开始了，但没有留下结果——结论未知。"
        "只重试只读或幂等的操作；有副作用的操作请先核实外部状态再决定。"
    )


def not_started_text(tool: str | None = None) -> str:
    """情形 B：打算调用但还没开始就被中断了。"""
    return (
        f"上一次回答打算调用{_tool_label(tool)}，但还没开始就被中断了（日志里没有它的调用记录）。"
        "如果仍然需要，请重试。"
    )


_TEXT_BUILDERS = {
    TOOL_OUTCOME_UNKNOWN: outcome_unknown_text,
    TOOL_NOT_STARTED: not_started_text,
}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Synthesized:
    """一条合成结论：给模型看的话 + 给界面用的归属。"""

    kind: str  # TOOL_OUTCOME_UNKNOWN | TOOL_NOT_STARTED
    tool_call_id: str
    tool: str = ""
    text: str = ""
    #: 归属的消息 id（能定位到就带上；崩溃在工具执行中时退化为「该会话最后一条消息」）
    message_id: int | None = None

    def payload(self) -> dict:
        """``recovery/synthesized`` 的载荷：规格三键 + 两个便于回查的扩展键。

        ``tool`` / ``message_id`` 是**只增字段**（规格给的是 ``{tool_call_id, kind, text}``）：
        没有它们，日志读方要重新扫全表才能知道「这是哪个工具的结论」「界面该标哪条消息」。
        ``synthesized`` 是第 6 个键（老基线三处载荷都带，本函数只负责这一条）。
        """
        return {
            "tool_call_id": self.tool_call_id,
            "kind": self.kind,
            "text": self.text,
            "tool": self.tool,
            "message_id": self.message_id,
            "synthesized": True,
        }


# ---------------------------------------------------------------------------
# 痕迹读取（薄封装：便于用例注入 stub）
# ---------------------------------------------------------------------------


def _payload_of(row: Any) -> dict:
    payload = getattr(row, "data", None)
    return payload if isinstance(payload, dict) else {}


def _event_type_of(row: Any) -> str:
    return str(getattr(row, "type", "") or "")


def _tool_call_id_of(payload: Any) -> str | None:
    """从载荷（或 ``agent_trace`` 的步骤 dict，二者同形）里取 ``tool_call_id``。"""
    if not isinstance(payload, dict):
        return None
    value = payload.get("tool_call_id")
    if isinstance(value, str) and value:
        return value
    return None


def _row_tool_call_id(row: SessionEvent) -> str | None:
    """事件行上的配对 id：**优先取物化列**（``ix_session_events_tool_call`` 就是为它建的），
    列上没有再看载荷同名键。

    v2 **有意差异**（如实登记）：老基线只认载荷里的同名键（那时列还没有写入点）；
    v2 自 4.1 起两条一起写，所以「列 + 载荷」是一致的，先读列能走索引、也少一次字典查找。
    """
    value = str(getattr(row, "tool_call_id", "") or "").strip()
    if value:
        return value
    return _tool_call_id_of(_payload_of(row))


def _tool_steps(agent_trace: Any) -> list[dict]:
    """``agent_trace.steps`` 里的工具步骤（形状不对就当没有，绝不抛）。"""
    if not isinstance(agent_trace, dict):
        return []
    steps = agent_trace.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict) and step.get("kind") == "tool"]


async def _load_messages(db: AsyncSession, chat_id: int) -> list[Message]:
    stmt = select(Message).where(Message.chat_id == chat_id).order_by(Message.id)
    return list((await db.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def ensure_balanced(db: AsyncSession, chat_id: int | None) -> list[Synthesized]:
    """把该会话里的工具配对缺口补平，返回**本次新合成**的结论（幂等）。

    - 情形 A：有带 ``tool_call_id`` 的 ``tool/call``、没有同 id 的 ``tool/result`` →
      ``TOOL_OUTCOME_UNKNOWN``（补一条表层 ``tool/result``）；
    - 情形 B：assistant 消息的 ``agent_trace`` 里有带 ``tool_call_id`` 的工具步骤、但日志里
      没有同 id 的 ``tool/call`` → ``TOOL_NOT_STARTED``（补 ``tool/call`` + ``tool/result`` 一对）；
    - ``chat_id`` 为空 → 直接返回 ``[]``（**零 DB 触碰**，与既有调用方兼容）；
    - 本函数**只读 + 只追加**：不 UPDATE / DELETE 任何行；写入走事件层的失败隔离
      （任何一层失败都只 warning + 计数，绝不抛给调用方，I5）；
    - 幂等：连跑两次，第二次**零新增事件**（断言 1）。
    """
    if not chat_id:
        return []

    rows = await event_log.list_events(db, chat_id)
    messages = await _load_messages(db, chat_id)

    calls: dict[str, dict] = {}
    results: set[str] = set()
    repaired: set[tuple[str, str]] = set()
    for row in rows:
        event_type = _event_type_of(row)
        call_id = _row_tool_call_id(row)
        if not call_id:
            continue
        if event_type == CALL_EVENT:
            calls.setdefault(call_id, _payload_of(row))
        elif event_type == SURFACE_RESULT_EVENT:
            results.add(call_id)
        elif event_type == RECOVERY_EVENT:
            kind = _payload_of(row).get("kind")
            if isinstance(kind, str):
                repaired.add((call_id, kind))

    # 步骤 → 归属消息：情形 A 的结论要挂回它所属的那条 assistant 消息（能挂就挂）
    step_owner: dict[str, int] = {}
    for message in messages:
        if getattr(message, "role", None) != "assistant":
            continue
        for step in _tool_steps(getattr(message, "agent_trace", None)):
            step_call_id = _tool_call_id_of(step)
            if step_call_id and step_call_id not in step_owner:
                step_owner[step_call_id] = int(message.id)

    pending: list[Synthesized] = []
    # 退化用的兜底挂载点：**该会话最后一条消息**（口径见模块 docstring 第 6 条）。
    last_message_id = int(messages[-1].id) if messages else None

    # 情形 A：有 call 无 result
    for call_id, payload in calls.items():
        if call_id in results or (call_id, TOOL_OUTCOME_UNKNOWN) in repaired:
            continue
        tool = str(payload.get("tool") or "")
        pending.append(
            Synthesized(
                kind=TOOL_OUTCOME_UNKNOWN,
                tool_call_id=call_id,
                tool=tool,
                text=outcome_unknown_text(tool),
                # 优先挂回真正的归属消息（agent_trace 里记过这次调用的那条 assistant 消息）；
                # 挂不上就退化为「该会话的最后一条消息」（N2′）。
                message_id=step_owner.get(call_id) or last_message_id,
            )
        )

    # 情形 B：assistant 声明要用工具但没有 call（只认带 id 的步骤，见模块 docstring 第 4 条）
    for message in messages:
        if getattr(message, "role", None) != "assistant":
            continue
        for step in _tool_steps(getattr(message, "agent_trace", None)):
            call_id = _tool_call_id_of(step)
            if not call_id or call_id in calls:
                continue
            if (call_id, TOOL_NOT_STARTED) in repaired:
                continue
            if any(item.tool_call_id == call_id for item in pending):
                continue
            tool = str(step.get("tool") or "")
            pending.append(
                Synthesized(
                    kind=TOOL_NOT_STARTED,
                    tool_call_id=call_id,
                    tool=tool,
                    text=not_started_text(tool),
                    message_id=int(message.id),
                )
            )

    for item in pending:
        await _append_recovery(db, chat_id, item)
    return pending


async def _append_recovery(db: AsyncSession, chat_id: int, item: Synthesized) -> None:
    """把一条结论落成事件：``recovery/synthesized`` + 配对的表层 ``tool/result``。

    情形 B 额外补一条合成的 ``tool/call``——不补就会出现「有 result 没有 call」的倒挂，
    而那正是本阶段要消灭的东西。**三处载荷都带 ``synthesized: True``**。

    表层 ``tool/result`` 的 ``surface_op`` 写死 ``"append"``（老基线同值）：
    事件层的 ``_check_type`` 只要求「非空」，不写死必然漂移（核验代理 B6）。

    ⚠️ **三处都要显式传 ``tool_call_id=`` 形参**（v2 的有意差异，别照老基线抄漏）：
    v2 的配对键被**物化成列**（``ix_session_events_tool_call`` 就是为它建的），
    而 ``append_event`` **不会**从载荷同名键自动镜像（没有老仓库那套 ``_link_from_payload``）
    ——只把 id 写进 ``data`` 会让**列上是 NULL**，按列读的读方（含本模块自己的
    :func:`_row_tool_call_id`）就得回落，I4 的对账键在索引上查不到。
    """
    await event_log.safe_append_event(
        db, chat_id, RECOVERY_EVENT, item.payload(), tool_call_id=item.tool_call_id
    )
    if item.kind == TOOL_NOT_STARTED:
        await event_log.safe_append_event(
            db,
            chat_id,
            CALL_EVENT,
            {
                "tool_call_id": item.tool_call_id,
                "tool": item.tool,
                "args": {},
                "round": None,
                "synthesized": True,
            },
            tool_call_id=item.tool_call_id,
        )
    await event_log.safe_append_event(
        db,
        chat_id,
        SURFACE_RESULT_EVENT,
        {
            "tool_call_id": item.tool_call_id,
            "tool": item.tool,
            "ok": False,
            "chars": len(item.text),
            # v2 的读数名是 ``materials_hit``（老基线叫 ``chunks_hit``——v2 的工具结果里
            # 没有「命中几个分块」这个概念）；这是**有意差异**，读方按 v2 形状取。
            "materials_hit": 0,
            "warnings": 0,
            "error": item.kind,
            "content": item.text,
            "synthesized": True,
        },
        surface_op="append",
        tool_call_id=item.tool_call_id,
    )


async def recovered_message_ids(db: AsyncSession, chat_id: int | None) -> set[int]:
    """该会话里「已被恢复流程标过」的消息 id 集合（界面 ``recovered: true`` 的判据）。

    从 ``recovery/synthesized`` 事件的载荷**现算**，而**不是**从 :func:`ensure_balanced`
    的返回值——后者只在「本次新合成」时有内容，第二次进同一个会话时是空的，
    拿它当判据会让提示一闪就没（老基线原注释）。
    """
    if not chat_id:
        return set()
    found: set[int] = set()
    for row in await event_log.list_events(db, chat_id):
        if _event_type_of(row) != RECOVERY_EVENT:
            continue
        message_id = _payload_of(row).get("message_id")
        if isinstance(message_id, int):
            found.add(message_id)
    return found
