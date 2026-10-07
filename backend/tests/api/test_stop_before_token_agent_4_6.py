"""条目 4.6：**首正文前（Agent 工具途中）HTTP 停止**的钉子（真机失败 → 定位 → 修复的回归）。

## 这份用例为什么存在

2026-09-19 的真机探针（`scripts/s4_real_agent_probe.py`）在 `stop-before` 场景报
`AssertionError`，且开发库里 **chat 42 整条不存在**——用 `AUTO_INCREMENT` 反查证明：
行**被 flush 过**（id 被消耗）但**事务被回滚**（`chats` 44↔43、`messages` 100↔99、
`session_events` 278↔277、`agent_runs` 4↔3）。

真机复现台（`scripts/_bisect_stop_before.py`：真 uvicorn + 真 TCP + 真开发库 + 慢速替身）
把链条钉到了底：

1. 停止 = **取消**，而取消可能正好命中请求会话**在途的那条语句**（实测命中
   `INSERT INTO session_events … 'tool/result'`）⇒ 事务进入 **pending-rollback**
   （后续语句一律 `PendingRollbackError`）；
2. `runner._safe_finish` 为清掉这个状态做**恢复性 `db.rollback()`** ⇒ 本轮**尚未提交**的
   会话行（`build_turn_plan` 里只 flush 的那一行）被一起抹掉；
3. 紧接着 `persist_stopped_turn` 要落「用户消息」，而 `messages.chat_id → chats.id` 的
   **外键直接冲突**（1452）⇒ 这一轮**连提问一起消失**。

修法两处（`回归修复`）：会话行在 `session/service.resolve_chat` 里**建时就提交**；
`persist_user_turn` 开头调 `ensure_turn_session`（回滚会把会话对象置为 expired，同步属性
访问会撞 `MissingGreenlet`，这里重新读一份）。

本文件两条用例：
- 第 1 条：真链路（HTTP + 真 DB）跑「Agent 轮 + 停止落在工具途中」，钉住**边界本身**；
- 第 2 条：**确定性复刻失败链条**（把「第一条语句被取消作废」注入成 `finish_run` 首调用抛
  `PendingRollbackError`，其余全真），钉住**会话行与提问不许丢**——修之前它是红的。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import PendingRollbackError

from app.agent import audit as agent_audit
from app.api import chat_routes
from app.core.config import settings
from app.models import Chat, Message, SessionEvent
from app.session import service as session_service
from tests.api.test_agent_stop_closeout import _slow_provider  # noqa: F401 —— 复用同一份慢速 Faux
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401
from tests.retrieval.conftest import seed_document

pytestmark = pytest.mark.asyncio

QUESTION = "夜市那条视频是怎么拍的？请先检索拍摄记录。"
CHUNK_TEXT = "夜市视频拍摄记录：三脚架固定机位，手持补拍特写，BGM 用轻快电子乐。"

#: 现行路由/检索三事件之后，原停止五事件必须全部保留且顺序不变。
STOP_TURN_EVENTS = (
    "routing/decided",
    "retrieval/start",
    "retrieval/result",
    "turn/start",
    "user/message",
    "stream/interrupted",
    "assistant/attempt",
    "turn/end",
)


def _install_slow_tool(monkeypatch, provider, *, tool_delay: float = 2.0):
    """装慢速 Faux + **慢速**假检索（真机里工具要在 embed/rerank 上花 ~1s）。

    返回一个 ``asyncio.Event``：**工具真的开始执行**时置位。停止请求等它——这样「停止落在
    工具执行途中」是**事件驱动**的，不靠睡眠猜时序（真机那一格正是这个位置）。
    """
    from app.agent.tools import knowledge_search

    bound = provider.model("faux-chat")
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: bound)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: bound)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )
    monkeypatch.setattr(settings, "WEB_ENABLE", False)

    tool_started = asyncio.Event()

    async def slow_retrieve(session, question, scope, *, stats=None):  # noqa: ANN001
        tool_started.set()
        await asyncio.sleep(tool_delay)
        return [], "", None

    monkeypatch.setattr(knowledge_search, "_default_retrieve", lambda: slow_retrieve)
    return tool_started


async def _drive_and_stop(client, request_id: str, tool_started: asyncio.Event) -> tuple[list[dict], dict]:
    """驱动 SSE 并在**工具执行途中**请求停止（事件驱动，不猜时序）。"""

    async def _stop_when_tool_runs():
        await asyncio.wait_for(tool_started.wait(), timeout=30)
        return await client.post("/api/chat/stop", json={"request_id": request_id})

    stopper = asyncio.create_task(_stop_when_tool_runs())
    async with client.stream(
        "POST",
        "/api/chat/stream",
        json={"message": QUESTION, "agent_mode": "react", "request_id": request_id},
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")
    if not tool_started.is_set():
        stopper.cancel()
        await asyncio.gather(stopper, return_exceptions=True)
        pytest.fail(f"首正文前停止的工具前提未成立；实际 SSE：{raw}")
    response = await stopper
    response.raise_for_status()
    return parse_sse(raw), response.json()


async def _rows(db, chat_id: int) -> tuple[list[Message], list[SessionEvent]]:
    messages = list(
        (await db.scalars(select(Message).where(Message.chat_id == chat_id).order_by(Message.id))).all()
    )
    events = list(
        (
            await db.scalars(
                select(SessionEvent).where(SessionEvent.chat_id == chat_id).order_by(SessionEvent.id)
            )
        ).all()
    )
    return messages, events


async def test_stop_on_first_agent_tool_frame_persists_the_user_turn(client, db, monkeypatch):  # noqa: F401
    """Agent 轮 + 停止落在工具途中 ⇒ `stopped` 收尾，且**提问必须还在库里**（真链路）。"""
    await seed_document(db, [CHUNK_TEXT], filename="拍摄记录.md")
    tool_started = _install_slow_tool(monkeypatch, _slow_provider())

    request_id = "s4-stop-before-agent"
    frames, stop_response = await _drive_and_stop(client, request_id, tool_started)

    kinds = [frame["type"] for frame in frames]
    assert stop_response == {"stopped": True}, kinds
    assert kinds[-1] == "stopped" and "done" not in kinds and "error" not in kinds
    assert "token" not in kinds, "停止点必须落在首正文之前"

    chat_id = frames[-1]["chat_id"]
    messages, _events = await _rows(db, chat_id)
    assert [row.role for row in messages] == ["user"], [row.role for row in messages]
    assert messages[0].content == QUESTION


async def test_new_chat_row_is_durable_before_the_turn_ends(db, fresh_db):  # noqa: F401
    """**机制钉子**：新建会话**建时就落库**——换一个连接也读得到，回滚也带不走它。

    （真机失败时这一行只在 flush 里躺着，`_safe_finish` 的恢复性回滚把它抹掉 ⇒ 后面的
    用户消息外键冲突。这里钉住「会话行先落地」这条不变量。）
    """
    chat = await session_service.resolve_chat(db, chat_id=None, message=QUESTION)

    assert (await fresh_db.get(Chat, chat.id)) is not None, "新建会话必须当场可见（已提交）"

    await db.rollback()  # 复刻 `_safe_finish` 的恢复性回滚
    assert (await fresh_db.get(Chat, chat.id)) is not None, "回滚不许带走会话行"


async def test_stop_before_token_survives_a_recovery_rollback(client, db, monkeypatch):  # noqa: F401
    """**失败链条的确定性复刻**（修之前红）：事务被作废 ⇒ 恢复性回滚 ⇒ 整轮仍要留得住。

    注入点只有一处：`agent_audit.finish_run` 的**第一次**调用抛 `PendingRollbackError`
    ——就是真机里「取消命中在途语句」之后 `_safe_finish` 看到的那个错。其余全真：
    真 HTTP、真 DB、真 `_audit_run` / `_safe_finish` / `persist_stopped_turn`。
    """
    await seed_document(db, [CHUNK_TEXT], filename="拍摄记录.md")
    tool_started = _install_slow_tool(monkeypatch, _slow_provider())

    real_finish = agent_audit.finish_run
    seen: list[str] = []

    async def flaky_finish(db_, run_id, *, status, error=None):  # noqa: ANN001
        assert tool_started.is_set(), "故障必须注入在实际工具开始之后"
        assert status == "stopped", "故障必须注入在停止收口"
        seen.append(status)
        if len(seen) == 1:
            raise PendingRollbackError("模拟：取消命中在途语句 ⇒ 事务 pending-rollback")
        return await real_finish(db_, run_id, status=status, error=error)

    monkeypatch.setattr(agent_audit, "finish_run", flaky_finish)

    frames, stop_response = await _drive_and_stop(client, "s4-stop-before-rollback", tool_started)

    kinds = [frame["type"] for frame in frames]
    assert stop_response == {"stopped": True}, kinds
    assert kinds[-1] == "stopped" and "done" not in kinds and "error" not in kinds
    assert len(seen) >= 2, "收口必须至少被尝试两次（第一次失败 + 恢复后的重试）"

    chat_id = frames[-1]["chat_id"]
    messages, events = await _rows(db, chat_id)
    assert [row.role for row in messages] == ["user"], [row.role for row in messages]
    assert messages[0].content == QUESTION, "提问不许丢"
    assert [event.type for event in events] == list(STOP_TURN_EVENTS), [
        event.type for event in events
    ]
    interrupted = [event for event in events if event.type == "stream/interrupted"][0]
    assert interrupted.data["reason"] == "stopped"
    assert json.loads(json.dumps(interrupted.data))["chars"] == 0
