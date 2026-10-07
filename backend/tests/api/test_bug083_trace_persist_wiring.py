"""`回归修复` 笔2「接线」载体：#5 / #6 / #6b 三个溯源落库吞点 → `counters.derived.trace_persist`。

🔴 口径（逐字，防误读）

· 键义 = 「本轮尾部溯源 / 审计落库失败」。出口 = `GET /api/agent/audit` 的
  `counters.derived.trace_persist` ⇒ **机器可读 + 人工 curl 的可见面**；
  ⛔ 本文件**不**声明「用户已能看见派生失败 / 前端可见」——现有前端不消费该端点。
· **恰一次**：计数只在该吞点的 `except` 分支 +1；成功路径（对照腿）零计数。
· 三处 rollback 全部**受保护**：rollback 自身抛错 ⇒ 不冒穿本轮 + 仍恰 +1。
  改前：#5 是裸 `await db.rollback()`（rollback 一抛就冒穿本轮）；#6 / #6b **根本没有** rollback
  ⇒ 本文件的「rollback 抛错」腿与「事务复位」腿在改前都是红的（这正是本次加固的判据）。
· 事务复位行为腿：commit 失败后事务停在失败态（PendingRollbackError 态）⇒ 同一会话在吞点之后
  必须仍可用。构造 = `bind_message` / `add_attributions` 替身往**同一个会话**塞
  `Message(chat_id=None, role=None)`（两处 NOT NULL 违反，MySQL / SQLite 都抛）⇒ 调用方的
  `await db.commit()` 抛 ⇒ 走吞点。该替身的三条腿都用**同一会话的事后 `SELECT 1`** 判：
  改前无 rollback ⇒ PendingRollbackError；改后「复位 + 重取」⇒ 可用。
· **复位之后还要重取**（本次实测追加，非原卡面）：失败 flush 之后的 rollback 会把本会话里的 ORM
  对象全部置为 `expired`（SQLAlchemy 既有语义，实测干净事务 rollback 不过期、失败 flush 后 rollback
  必过期）⇒ 紧随其后的同步属性访问（`chat.id` / `assistant_msg.id` / `plan.chat.id`）撞
  `MissingGreenlet` ⇒ **光 rollback 仍会「炸穿本轮」**（实测栈顶 `chat_routes.py:1394 in persist_turn`）。
  故三处吞点在复位后 `await db.refresh(...)` 重取后续还要读的实例；同一机制的既有先例 =
  `ensure_turn_session`（条目 4.6 · `回归修复`，`backend/app/api/chat_routes.py:1481`）。
· **#6b 三条腿走「进程内直调」**（不再是 HTTP 停止腿）：停止轮的 assistant 块被
  `backend/app/api/chat_routes.py:1590` 的 `if partial or (reasoning or "").strip():` 守着，而
  既有停止腿的 Agent 决策文本不是答案正文 ⇒ `answer` 空 ⇒ bind 与 commit 整体跳过
  （实测 `bind_calls=0`）⇒ HTTP 停止腿**到不了站点**。直调用真聊天行 + `SimpleNamespace` 替身 +
  `await chat_routes.persist_stopped_turn(db, plan, "半截回答")`（照
  `backend/tests/session/test_event_attribution_4_3.py:31` 的建聊天行构造）⇒ 真到站点。

| # | 落点（按符号定位，行号随批次漂移） | 失败面 |
|---|---|---|
| #5 | `chat_routes.persist_turn` → `repo.add_attributions` 的 try/except | 归因行写失败（原：裸 rollback） |
| #6 | `chat_routes.persist_turn` → `agent_audit.bind_message` 的 try/except | 审计回填失败（原：零 rollback） |
| #6b | `chat_routes.persist_stopped_turn` → `agent_audit.bind_message` 的 try/except | 停止轮回填失败（原：零 rollback） |
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import audit as agent_audit
from app.api import chat_routes
from app.core.config import settings
from app.models import Message, MessageAttribution
from app.session import repository as repo
from tests.api.test_chat_agent import (
    _events,
    _install_model,
    _install_retrieval,
    _provider_with_tool_call,
)
from tests.api.test_chat_citation import _assistant_message
from tests.api.test_chat_contract import client  # noqa: F401 —— client 是夹具
from tests.api.test_chat_retrieval_wiring import _install
from tests.retrieval.conftest import seed_document

pytestmark = pytest.mark.asyncio

KEY = "trace_persist"
OTHER_KEY = "tool_result_channel"
CHUNK_TEXT = "向量库使用 Qdrant 内嵌模式，向量化由上游接口完成。"


def _counts() -> dict:
    from app.core import derived_counters

    return derived_counters.ordered_counts()


@pytest.fixture(autouse=True)
def _reset_derived_counts():
    """每个用例前后清零派生计数（计数腿是「增量」判据，必须从 0 起跑）。"""
    from app.core import derived_counters

    derived_counters.reset_failure_counts()
    try:
        yield
    finally:
        derived_counters.reset_failure_counts()


@pytest.fixture(autouse=True)
def _pin_agent_mode_off(monkeypatch):
    """本文件自钉 ``settings.AGENT_MODE = "off"``（回归修复 诊断 · 见
    ``_bugfix10-scratch/fix-split/T10-F1载体聚合4红-诊断.md``）。

    本机 ``.env`` 设了 ``AGENT_MODE=react``（``app/core/config.py:160`` 的 ``env_file``）⇒
    进程默认值是 ``react``；而 ``tests/api/conftest.py::pin_agent_mode_off`` 这条公共 autouse
    护栏在「``tests`` Package 被重复 collect ⇒ 出现第二个 ``tests/api`` Package 节点」的
    场景下**不会绑到本文件的 item 上**（实测：13 载体同进程跑时本文件 11 条腿全部读到
    ``react`` ⇒ 4 条 HTTP 非流式腿撞 ``guard_non_stream_agent_mode`` 400）。本夹具绑在
    **本文件的 Module 节点**上，不受该收集树异常影响 ⇒ 与公共护栏形成双保险；也不改变
    被测语义（仍走「请求未给 ⇒ 取进程默认值」的路径，只是把默认值钉成出厂态 ``off``）。
    """
    monkeypatch.setattr(settings, "AGENT_MODE", "off")


# --------------------------------------------------------------------------
# 探针脚手架
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def probe_env(engine, monkeypatch):
    """请求会话**可捕获且不自动 close** 的 ASGI 客户端。

    只为本文件的「rollback 抛错」与「事务复位」两类腿存在：两者都要在**响应之后**继续观察
    同一个请求会话的事务态，而标准 `client` 夹具（`async with factory() as session`）在响应
    结束时就 close 了会话（close 内部会回滚 ⇒ 观察不到「未复位」）。
    """
    from app.core.config import settings
    from app.db.session import get_db
    from main import app

    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", True)
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", True)
    monkeypatch.setattr(settings, "ROUTING_MODE", "regex")

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    captured: dict = {"sessions": []}

    async def _override_get_db():
        session = factory()
        captured["sessions"].append(session)
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield SimpleNamespace(client=c, captured=captured)
    finally:
        app.dependency_overrides.pop(get_db, None)
        for session in captured["sessions"]:
            await session.close()


def _patch_rollback_for(monkeypatch, sessions: list) -> list[str]:
    """把 `AsyncSession.rollback` 换成「对**指定会话**抛错」的替身（其余会话照常）。

    只拦指定会话：pytest 夹具 / 独立会话照常——否则「别处的 rollback 抛了」会被误记成这条腿的
    结论。请求级腿传 `probe_env.captured["sessions"]`，进程内直调腿传 `[db]`。
    """
    real = AsyncSession.rollback
    hits: list[str] = []

    async def _guarded(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if any(self is session for session in sessions):
            hits.append("raised")
            raise RuntimeError("probe: rollback itself failed")
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "rollback", _guarded)
    return hits


def _poison_bind_message(monkeypatch) -> None:
    """让 `bind_message` 在**请求会话**里塞一个必然 flush 失败的行 ⇒ 调用方 commit 抛。"""

    async def _poison(session, _run_id, _message_id):  # noqa: ANN001
        session.add(Message(chat_id=None, role=None, content="t13-poison"))
        return None

    monkeypatch.setattr(agent_audit, "bind_message", _poison)


async def _assert_sessions_usable(sessions) -> None:  # noqa: ANN001
    assert sessions, "没有捕获到请求会话（探针失效 ⇒ 会假绿）"
    for session in sessions:
        result = await session.execute(sa_text("SELECT 1"))
        assert result.scalar() == 1


async def _react_round(client, db, monkeypatch) -> list[dict]:  # noqa: ANN001
    """一次真 ReAct 轮（含审计 run）——照 `test_bug083_persist_turn_swallow_pins` 的构造。"""
    from app.core.config import settings
    from tests.api.test_chat_agent import _seed_materials

    monkeypatch.setattr(settings, "AGENT_MODE", "off")
    materials = await _seed_materials(
        db, ["蓝鲸是哺乳动物。", "鲸类生活在海里。"], filename="蓝鲸.md"
    )
    _install_retrieval(monkeypatch, materials)
    provider = _provider_with_tool_call("蓝鲸")
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[1]")
    return await _events(client, {"message": "蓝鲸是什么", "agent_mode": "react"})


async def _new_chat(db, title: str):  # noqa: ANN001
    """建一行真聊天（停止轮落库要过外键；照 `tests/session/test_event_attribution_4_3.py:31`）。"""
    chat = await repo.create_chat(db, title=title)
    await db.commit()
    return chat


def _stopped_plan(chat, *, audit_run_id: int | None = 1) -> SimpleNamespace:  # noqa: ANN001
    """停止轮 `TurnPlan` 替身（字段集 = `persist_user_turn` / `persist_stopped_turn` 真读到的）。"""
    return SimpleNamespace(
        chat=chat,
        question="停止轮的问题",
        source_type="external",
        filters=None,
        skill_ids=[],
        turn=1,
        audit_run_id=audit_run_id,
        agent_trace=None,
    )


async def _assistant_rows(db, chat_id: int) -> list:  # noqa: ANN001
    """该会话下的助手消息行（直调腿的「停止语义不变」判据）。"""
    rows = (
        await db.execute(
            select(Message).where(Message.chat_id == chat_id, Message.role == "assistant")
        )
    ).scalars().all()
    return list(rows)


# --------------------------------------------------------------------------
# 零态 / #5 / #6 / #6b：计数腿
# --------------------------------------------------------------------------


async def test_zero_state_counts_are_zero(client):
    """零态：不发任何请求 ⇒ 两键均为 0（防「恒非零」的假绿）。"""
    counts = _counts()
    assert counts[KEY] == 0 and counts[OTHER_KEY] == 0, counts


async def test_05_attribution_failure_counts_once(client, db, monkeypatch):
    """#5：归因 `add_attributions` 必抛 ⇒ 恰 +1，并能从审计端点读回来。"""

    async def _boom(_db, _rows):
        raise RuntimeError("probe: 归因落库失败")

    monkeypatch.setattr(repo, "add_attributions", _boom)
    await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    _install(monkeypatch, reply="据资料 [1]，向量库用 Qdrant。")

    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "normal"
    counts = _counts()
    assert counts[KEY] == 1, counts
    assert counts[OTHER_KEY] == 0, "不得误记别的键"
    audit = await client.get("/api/agent/audit")
    assert audit.status_code == 200, audit.text
    derived = audit.json()["counters"]["derived"]
    assert derived[KEY] == 1, derived


async def test_05_control_success_path_counts_zero(client, db, monkeypatch):
    """#5 对照腿（防空转）：不打桩时归因真落行，计数保持 0。"""
    await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    _install(monkeypatch, reply="据资料 [1]，向量库用 Qdrant。")

    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})

    assert resp.status_code == 200
    message = await _assistant_message(db, resp.json()["chat_id"])
    rows = (
        await db.execute(
            select(MessageAttribution).where(MessageAttribution.message_id == message.id)
        )
    ).scalars().all()
    assert len(rows) == 1, "对照腿：不打桩时归因真落行（红点确实打在真位置）"
    assert _counts()[KEY] == 0, "成功路径零计数"


async def test_06_audit_bind_failure_counts_once(client, db, monkeypatch):
    """#6：`bind_message` 必抛 ⇒ 本轮照常收尾 + 恰 +1。"""

    async def _boom(_db, _run_id, _message_id):
        raise RuntimeError("probe: 审计回填失败")

    monkeypatch.setattr(agent_audit, "bind_message", _boom)
    events = await _react_round(client, db, monkeypatch)

    kinds = [event["type"] for event in events]
    assert kinds[0] == "meta" and "done" in kinds, kinds
    assert _counts()[KEY] == 1, _counts()


async def test_06b_stopped_turn_bind_failure_counts_once(db, monkeypatch):
    """#6b：停止轮的 `bind_message` 必抛 ⇒ 停止轮照常返回 + 恰 +1（进程内直调到站点）。"""

    async def _boom(_db, _run_id, _message_id):
        raise RuntimeError("probe: 停止轮审计回填失败")

    monkeypatch.setattr(agent_audit, "bind_message", _boom)
    chat = await _new_chat(db, "t13-6b-计数")
    plan = _stopped_plan(chat)

    returned = await chat_routes.persist_stopped_turn(db, plan, "半截回答")

    assert returned.chat_id == chat.id and returned.role == "user", "用户消息仍落（提问不丢）"
    rows = await _assistant_rows(db, chat.id)
    assert len(rows) == 1 and rows[0].kind == "stopped", "停止轮的助手消息语义不变"
    assert _counts()[KEY] == 1, _counts()


# --------------------------------------------------------------------------
# 三处 rollback 的硬加固腿（rollback 自身抛 ⇒ 不冒穿 + 仍恰 +1）
# --------------------------------------------------------------------------


async def test_05_rollback_raising_does_not_escape_and_counts_once(probe_env, db, monkeypatch):
    """#5：rollback 自身抛 ⇒ 不冒穿本轮（改前是裸调用 ⇒ 直接冒穿），且仍恰 +1。"""

    async def _boom(_db, _rows):
        raise RuntimeError("probe: 归因落库失败")

    monkeypatch.setattr(repo, "add_attributions", _boom)
    hits = _patch_rollback_for(monkeypatch, probe_env.captured["sessions"])
    await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    _install(monkeypatch, reply="据资料 [1]，向量库用 Qdrant。")

    resp = await probe_env.client.post("/api/chat", json={"message": "向量库用什么存储？"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "normal"
    assert hits, "探针没被触发 ⇒ 复现失效（会假绿）"
    assert _counts()[KEY] == 1, _counts()


async def test_06_rollback_raising_does_not_escape_and_counts_once(
    probe_env, db, monkeypatch
):
    """#6：新增的受保护 rollback 自身抛 ⇒ 本轮照常 done，且仍恰 +1。"""
    hits = _patch_rollback_for(monkeypatch, probe_env.captured["sessions"])

    async def _boom(_db, _run_id, _message_id):
        raise RuntimeError("probe: 审计回填失败")

    monkeypatch.setattr(agent_audit, "bind_message", _boom)
    events = await _react_round(probe_env.client, db, monkeypatch)

    kinds = [event["type"] for event in events]
    assert "done" in kinds and "error" not in kinds, kinds
    assert hits, "探针没被触发 ⇒ 复现失效（会假绿）"
    assert _counts()[KEY] == 1, _counts()


async def test_06b_rollback_raising_does_not_escape_and_counts_once(
    db, monkeypatch
):
    """#6b：停止轮新增的受保护 rollback 自身抛 ⇒ 停止语义不变，且仍恰 +1。

    探针只拦**本用例的 `db` 会话**（进程内直调腿没有请求会话可拦）；用 `monkeypatch.context()`
    圈住调用 ⇒ 断言前就把替身摘掉（否则夹具收尾那次 rollback 会被替身打红：红的是收尾，不是判据）。
    返回的 `user_msg` 仍可读 ⇒ 生产侧复位后把本轮落库实例（`plan.chat` / `user_msg`）也重取回来了。
    """

    async def _boom(_db, _run_id, _message_id):
        raise RuntimeError("probe: 停止轮审计回填失败")

    chat = await _new_chat(db, "t13-6b-复位抛错")
    plan = _stopped_plan(chat)

    with monkeypatch.context() as probe:
        probe.setattr(agent_audit, "bind_message", _boom)
        hits = _patch_rollback_for(probe, [db])
        returned = await chat_routes.persist_stopped_turn(db, plan, "半截回答")

    assert returned.role == "user", "rollback 自身抛 ⇒ 也不冒穿（停止轮照常返回）"
    assert hits, "探针没被触发 ⇒ 复现失效（会假绿）"
    assert _counts()[KEY] == 1, _counts()


# --------------------------------------------------------------------------
# 事务复位行为腿（commit 失败 ⇒ 同一会话在吞点之后仍可用）
# --------------------------------------------------------------------------


async def test_06_session_is_usable_after_swallow(probe_env, db, monkeypatch):
    """#6：真 commit 失败（flush 违反 NOT NULL）后，同一请求会话仍可执行 + 本轮照常 done。

    改前无 rollback ⇒ 该会话停在 PendingRollbackError 态 ⇒ 事后 `SELECT 1` 抛（本腿红）；
    只 rollback 不重取 ⇒ `chat_routes.py:1394` 的 `chat.id` / `assistant_msg.id` 撞
    MissingGreenlet ⇒ 收尾从 `done` 掉成 `error`（本腿同样红）。
    """
    _poison_bind_message(monkeypatch)

    events = await _react_round(probe_env.client, db, monkeypatch)

    kinds = [event["type"] for event in events]
    assert "done" in kinds, kinds
    assert _counts()[KEY] == 1, _counts()
    await _assert_sessions_usable(probe_env.captured["sessions"])


async def test_05_session_is_usable_after_swallow(probe_env, db, monkeypatch):
    """#5：归因 commit 真失败后，同一请求会话仍可执行 + 本轮照常返回 normal（复位 + 重取）。

    构造 = `add_attributions` 替身把坏行塞进**请求会话** ⇒ 接线里那次 `await db.commit()` 抛。
    改前：裸 rollback（抛错冒穿）/ 只 rollback 不重取（后续 `chat.id` 撞 MissingGreenlet）。
    """

    async def _poison(session, _rows):  # noqa: ANN001
        session.add(Message(chat_id=None, role=None, content="t13-poison-attr"))
        return None

    monkeypatch.setattr(repo, "add_attributions", _poison)
    await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    _install(monkeypatch, reply="据资料 [1]，向量库用 Qdrant。")

    resp = await probe_env.client.post("/api/chat", json={"message": "向量库用什么存储？"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "normal"
    assert _counts()[KEY] == 1, _counts()
    await _assert_sessions_usable(probe_env.captured["sessions"])


async def test_06b_session_is_usable_after_swallow(db, monkeypatch):
    """#6b：停止轮的 commit 真失败（flush 违反 NOT NULL）后，同一会话仍可执行。

    改前无 rollback ⇒ 该会话停在 PendingRollbackError 态（本腿红）；**只 rollback 不重取** ⇒
    紧随其后的 `plan.chat.id` 撞 MissingGreenlet（本腿同样红，实测留痕见证据件）。
    """
    _poison_bind_message(monkeypatch)
    chat = await _new_chat(db, "t13-6b-复位")
    plan = _stopped_plan(chat)

    returned = await chat_routes.persist_stopped_turn(db, plan, "半截回答")

    assert returned.role == "user"
    assert _counts()[KEY] == 1, _counts()
    assert len(await _assistant_rows(db, chat.id)) == 1, "回声：助手行已落库（本轮没被毁掉）"
    result = await db.execute(sa_text("SELECT 1"))
    assert result.scalar() == 1, "吞点之后同一会话必须仍可用"
