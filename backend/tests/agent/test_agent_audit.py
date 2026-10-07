"""Agent 审计单测（阶段 4 · 条目 4.3.2；验收编号 A4 / A5 / A6 / A7 / A8 / A9）。

钉的是 ``agent_runs`` / ``agent_steps`` 的**写入**（真执行的工具调用）与**生命周期收口**
（所有退出路径都落 ``status`` + ``finished_at``，库里不留 ``running`` 僵尸行）。

四条退出路径（A5）在**运行器层**测：``stream_agent_turn`` 直接收 ``db`` / ``chat_id``，
比走 HTTP 更精确地指向 ``runner._audit_run`` 的四个出口（正常 / 工具炸 / 模型不可用 / 取消）；
两条 **HTTP 层**用例（A6 ``message_id`` 回填、A7 审计写入失败不炸穿、A8 开关关掉后逐字一致）
走 ``tests/api/test_chat_contract.py`` 的 ``client`` 夹具（ASGI 直连、不起端口）。

⚠️ 本文件在 ``tests/agent/`` 下，``tests/api/conftest.py`` 的 autouse 钉子**不生效**，
故：① 自己钉 ``WEB_ENABLE=False``（本机 ``.env`` 会盖过默认值）；② 显式 import 那三个
隔离夹具（向量路 / 重排），保证 HTTP 用例零真出网。

进程内计数器（``audit_write_failures()``）跨用例共享 ⇒ 断言增量前先 ``reset``（硬性要求 4）。
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select

from app.agent import audit as agent_audit
from app.agent import runner
from app.agent.registry import ToolSpec
from app.agent.runner import AgentTurn
from app.agent.tools import knowledge_search
from app.agent.types import ToolObservation
from app.api import chat_routes
from app.core.config import settings
from app.models import AgentRun, AgentStep, Chat, Message, SessionEvent
from tests.agent.test_runner import (
    CollectingRetrieval,
    collect,
    faux_model,
    material,
    registry_with,
    says,
    tool_call,
)
from tests.api.conftest import (  # noqa: F401 —— 引进来才有 autouse 效果（见模块 docstring）
    isolate_retrieval_vectors,
    pin_aux_model_delegates,
    pin_rerank_off,
)
from tests.api.test_chat_contract import (  # noqa: F401 —— client 夹具与 SSE 解析
    _StubClassifier,
    client,
    parse_sse,
)

#: A4 那条「成败各一次」的替身工具名
PROBE_NAME = "probe_tool"

#: HTTP 用例的问题文本。
#: ⚠️ **不能**用「问题」这种短句：澄清判定（1.4）会判它 ``looks_vague`` 并**多叫一次模型**
#: （``clarify.draft_clarify_question`` → ``generate``），把 Faux 响应队列的第一格吃掉——
#: 于是 Agent 的第一轮决策拿到的是「收尾」那条，工具一次都不调用，用例会假绿（实测踩到）。
QUESTION = "蓝鲸是什么"


@pytest.fixture(autouse=True)
def pin_web_off(monkeypatch):
    """钉住联网开关（理由见模块 docstring：本目录拿不到 ``tests/api`` 的 autouse 钉子）。"""
    monkeypatch.setattr(settings, "WEB_ENABLE", False)


# --------------------------------------------------------------------------
# 助手
# --------------------------------------------------------------------------


async def _new_chat(db) -> int:
    """建一行真 ``chats``：``agent_runs.chat_id`` 有外键，假 id 会撞 FK。"""
    chat = Chat(title="审计用例")
    db.add(chat)
    await db.flush()
    return int(chat.id)


async def _latest_run(db, chat_id: int) -> AgentRun | None:
    """该会话**最新**一轮的 ``agent_runs`` 行（``id desc`` 取本轮）。"""
    rows = await db.execute(
        select(AgentRun).where(AgentRun.chat_id == chat_id).order_by(AgentRun.id.desc())
    )
    return rows.scalars().first()


async def _count(db, model, **filters) -> int:  # noqa: ANN001
    stmt = select(func.count()).select_from(model)
    for key, value in filters.items():
        stmt = stmt.where(getattr(model, key) == value)
    return int((await db.execute(stmt)).scalar_one())


async def _assert_no_zombie_running(db) -> None:  # noqa: ANN001
    """「不许留 ``running`` 僵尸行」是 A5 的全局断言（不是只看本轮那一行）。"""
    assert await _count(db, AgentRun, status="running") == 0


class FlakyArgs(BaseModel):
    """A4 替身工具的入参模型。"""

    query: str = ""


def flaky_spec(calls: list[str]) -> ToolSpec:
    """第一次成功、第二次抛异常的工具（A4：「成败都记」需要两类各一条）。"""

    async def handler(args):  # noqa: ANN001
        calls.append(args.query)
        if len(calls) == 2:
            raise RuntimeError("工具炸了")
        return ToolObservation(text="第一次成功")

    return ToolSpec(
        name=PROBE_NAME,
        description="成败各一次的替身工具",
        parameters_schema=FlakyArgs.model_json_schema(),
        handler=handler,
        dangerous=False,
        parameters=FlakyArgs,
        needs_web=False,
        needs_confirm=False,
    )


def _tool_call_provider(query: str, *, finish: str = "可以作答了"):
    """HTTP 用例的响应队列：① 调 ``knowledge_search`` ② 收尾（回答链路那一格由 ``_install_model`` 补）。"""
    from cubeloop.providers import FauxProvider, faux_assistant_message, faux_tool_call

    provider = FauxProvider(provider_id="faux")
    provider.set_responses(
        [
            faux_assistant_message([faux_tool_call("knowledge_search", {"query": query})]),
            faux_assistant_message(finish),
        ]
    )
    return provider


def _install_model(monkeypatch, provider, *, answer_text: str, label: str = "internal_db") -> None:
    """把端点里的模型装配换成 Faux（零真模型），并补上回答链路那一格。"""
    from cubeloop.providers import faux_assistant_message

    provider.append_responses([faux_assistant_message(answer_text)])
    model = provider.model("faux-chat")
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(chat_routes, "resolve_routing_classifier", lambda: _StubClassifier(label))


def _install_empty_retrieval(monkeypatch) -> None:
    """把 ``knowledge_search`` 的检索实现换成**空结果**假件（零向量库 / 零 embedding / 零出网）。"""

    async def empty(session, question, scope, *, stats=None):  # noqa: ANN001
        return [], "", None

    monkeypatch.setattr(knowledge_search, "_default_retrieve", lambda: empty)


async def _refresh(db) -> None:  # noqa: ANN001
    """结束 ``db`` 会话当前读事务，让后面的断言能看见 HTTP 请求**另一个会话**刚提交的行。

    MySQL 默认 REPEATABLE READ：``db`` 夹具造语料时读过一次就钉住了快照，不刷新会看到
    「一行都没有」——与真假无关（与 ``tests/api/test_chat_agent.py::_refresh`` 同一写法）。
    """
    await db.commit()


async def _stream(client, payload: dict) -> list[dict]:  # noqa: ANN001
    resp = await client.post("/api/chat/stream", json=payload)
    assert resp.status_code == 200, resp.text
    return parse_sse(resp.text)


def _agent_titles(events: list[dict]) -> list[str]:
    return [e["step"]["title"] for e in events if e["type"] == "agent"]


# --------------------------------------------------------------------------
# A4 · 审计成败都记
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_records_both_success_and_failure(db):
    """A4：一次成功 + 一次失败 ⇒ ``agent_steps`` 两行，``meta["ok"]`` 为 True/False，且 id 与 ``tool/call`` 同值。

    附带钉住**并发写库的修复**：多工具轮的审计行必须**零失败**（``audit_write_failures()`` 增量 0）。
    修复前这条会红——ReAct 的审计写在 CubeLoop 的任务里、事件写在消费端任务里，两者共用同一
    ``AsyncSession`` ⇒ 稳定抛 ``InvalidRequestError: Session is already flushing``，被 ``safe_*``
    吞成「已隔离」但**那一行审计可能丢**（实测 5/5 复现）。现在记账与写库分离（``runner.ToolAudit``
    + 消费端串行 ``_flush_audit``），这条用例把「别再退回去」钉住。
    """
    agent_audit.reset_audit_write_failures()
    before = agent_audit.audit_write_failures()
    chat_id = await _new_chat(db)
    calls: list[str] = []
    spec = flaky_spec(calls)
    model = faux_model(
        [
            tool_call(PROBE_NAME, {"query": "a"}),
            tool_call(PROBE_NAME, {"query": "b"}),
            says("够了"),
        ]
    )
    turn = AgentTurn(mode="react")
    await collect(
        turn,
        question="问题",
        model=model,
        registry=registry_with(spec),
        db=db,
        chat_id=chat_id,
        turn_no=1,
        max_rounds=5,
    )
    assert calls == ["a", "b"]  # 两次调用都真的执行了（失败那次不是被跳过）

    run = await _latest_run(db, chat_id)
    assert run is not None
    steps = list(
        (
            await db.execute(
                select(AgentStep)
                .where(AgentStep.run_id == run.id)
                .order_by(AgentStep.step_no)
            )
        )
        .scalars()
        .all()
    )
    assert len(steps) == 2  # 成败**都**记（「只记成功的」会让失败调用在审计里消失）
    assert [s.type for s in steps] == ["tool_call", "tool_call"]
    assert [s.tool_name for s in steps] == [PROBE_NAME, PROBE_NAME]
    assert [s.meta["ok"] for s in steps] == [True, False]
    assert [s.step_no for s in steps] == [1, 2]  # 整轮序号（1 起）
    assert steps[0].content and "第一次成功" in steps[0].content
    # 回归修复：审计行内容也只带固定形状（异常原文不外泄）
    assert steps[1].content and "工具炸了" not in steps[1].content
    assert "probe_tool 执行失败（RuntimeError）。" in steps[1].content

    rows = await db.execute(
        select(SessionEvent)
        .where(
            SessionEvent.chat_id == chat_id,
            SessionEvent.type == "tool/call",
        )
        .order_by(SessionEvent.seq)
    )
    event_calls = list(rows.scalars().all())
    assert len(event_calls) == 2
    # 审计行与事件行**同一个** tool_call_id（对账键，I4 的审计侧）
    assert [s.tool_call_id for s in steps] == [e.tool_call_id for e in event_calls]
    assert all(e.tool_call_id for e in event_calls)
    # 两行都真的写进去了、一次失败都没有（并发写库的回归钉，见 docstring）
    assert agent_audit.audit_write_failures() - before == 0


@pytest.mark.asyncio
async def test_react_audit_meta_args_digest_is_recorded(db):
    """A4 附带（缺陷钉）：ReAct 路径的 ``meta["args"]`` 应当记下本次调用的参数摘要。

    为什么单独成一条：A4 的验收原文只要求 ``meta["ok"]`` 与 ``tool_call_id``；参数摘要是
    ``app/agent/audit.py`` 模块 docstring 承诺的字段（``meta = {ok, ms, args, error}``），
    单独钉住能让「修没修」一眼可见，又不至于把 A4 主用例拖红。

    ⚠️ **这条钉的是一个真实抓到的缺陷**：ReAct 路径上 CubeLoop 会把工具参数**校验成
    ``ToolSpec.parameters`` 的实例**（pydantic 模型）再交给执行闭包，而摘要函数原先只有
    ``isinstance(args, dict)`` 一个判据 ⇒ ``meta["args"]`` 恒为空 dict（P&E 那条传的本来就是
    dict，因此只在 ReAct 上暴露）。修法：先 ``model_dump()`` 再摘要（``runner._args_digest``
    与 ``audit._args_digest`` 两处都要，否则「同一份参数两个真相源」）。
    修好之前本条是以 ``xfail(strict=True)`` 登记的，修好后按**正常用例**通过。
    """
    chat_id = await _new_chat(db)
    calls: list[str] = []
    spec = flaky_spec(calls)  # 第一次成功（只调一次）
    model = faux_model([tool_call(PROBE_NAME, {"query": "甲"}), says("够了")])
    turn = AgentTurn(mode="react")
    await collect(
        turn,
        question="问题",
        model=model,
        registry=registry_with(spec),
        db=db,
        chat_id=chat_id,
        turn_no=1,
    )
    run = await _latest_run(db, chat_id)
    steps = list(
        (
            await db.execute(
                select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.step_no)
            )
        )
        .scalars()
        .all()
    )
    assert [c for c in calls] == ["甲"]
    assert steps[0].meta["args"] == {"query": "甲"}, "ReAct 的参数摘要不能是空 dict"
    # 与事件层那份同源（同一个 `_args_digest` 口径）——两处不一致就说明有第二份实现
    calls_events = list(
        (
            await db.execute(
                select(SessionEvent)
                .where(SessionEvent.chat_id == chat_id, SessionEvent.type == "tool/call")
                .order_by(SessionEvent.seq)
            )
        )
        .scalars()
        .all()
    )
    assert calls_events[0].data["args"] == steps[0].meta["args"]


# --------------------------------------------------------------------------
# A5 · agent_runs 全路径收口（四条出口）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_is_closed_as_done_on_a_normal_turn(db):
    """A5（正常轮）：``status == "done"``、``finished_at`` 非空、库里没有一行 ``running``。"""
    chat_id = await _new_chat(db)
    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[material(1)]]))
    model = faux_model([tool_call("knowledge_search", {"query": "q"}), says("够了")])
    turn = AgentTurn(mode="react")
    await collect(
        turn,
        question="问题",
        model=model,
        registry=registry_with(spec),
        db=db,
        chat_id=chat_id,
        turn_no=1,
    )

    run = await _latest_run(db, chat_id)
    assert run is not None and run.status == "done"
    assert run.finished_at is not None
    assert run.mode == "react"
    await _assert_no_zombie_running(db)


@pytest.mark.asyncio
async def test_run_is_closed_as_done_when_a_tool_explodes(db):
    """A5（工具炸）：handler 抛异常 ⇒ 本轮仍**收尾**为 ``done``（不炸穿、不留僵尸行）。"""

    async def boom(session, question, scope, *, stats=None):  # noqa: ANN001
        raise RuntimeError("向量库炸了")

    chat_id = await _new_chat(db)
    spec = knowledge_search.build_spec(db=object(), retrieve=boom)
    model = faux_model([tool_call("knowledge_search", {"query": "q"}), says("换路")])
    turn = AgentTurn(mode="react")
    await collect(
        turn,
        question="问题",
        model=model,
        registry=registry_with(spec),
        db=db,
        chat_id=chat_id,
        turn_no=1,
        max_rounds=3,
    )

    run = await _latest_run(db, chat_id)
    assert run is not None
    # 「这一轮没跑成」（基础设施问题）与「这一轮跑了、只是某个工具没成」（业务正常）必须分得清
    assert run.status == "done"
    assert run.finished_at is not None
    await _assert_no_zombie_running(db)


@pytest.mark.asyncio
async def test_run_is_closed_as_done_when_the_model_is_missing(db):
    """A5（模型不可用）：``model=None`` 走 ``note("模型不可用")`` 出口，同样 ``done`` + 有 ``finished_at``。"""
    chat_id = await _new_chat(db)
    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[]]))
    turn = AgentTurn(mode="react")
    await collect(
        turn,
        question="问题",
        model=None,
        registry=registry_with(spec),
        db=db,
        chat_id=chat_id,
        turn_no=1,
    )
    assert turn.note.startswith("模型不可用")

    run = await _latest_run(db, chat_id)
    assert run is not None and run.status == "done"
    assert run.finished_at is not None
    await _assert_no_zombie_running(db)


@pytest.mark.asyncio
async def test_run_is_closed_as_stopped_when_the_stream_is_cancelled(db, monkeypatch):
    """A5（流式中停）：消费方取消 ⇒ ``status == "stopped"`` + ``finished_at`` 非空（不是僵尸 ``running``）。"""
    import cubeloop

    real_agent = cubeloop.Agent

    class HangingAgent(real_agent):  # type: ignore[misc, valid-type]
        """决策循环永远挂起：只有取消能让这一轮结束（比「等 Faux 慢慢吐字」确定得多）。"""

        async def prompt(self, *_args, **_kwargs):  # noqa: ANN003
            await asyncio.Event().wait()

    monkeypatch.setattr(cubeloop, "Agent", HangingAgent)

    chat_id = await _new_chat(db)
    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[]]))
    turn = AgentTurn(mode="react")

    async def consume():
        async for _frame in runner.stream_agent_turn(
            turn,
            question="问题",
            model=faux_model([says("很长的一段话")]),
            registry=registry_with(spec),
            db=db,
            chat_id=chat_id,
            turn_no=1,
        ):
            pass

    task = asyncio.ensure_future(consume())
    for _ in range(200):  # 等审计行先建成（否则这条用例测不到「取消时收口」）
        if turn.run_id is not None:
            break
        await asyncio.sleep(0.01)
    assert turn.run_id is not None, "审计行没建成：本用例的前提不成立"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert turn.aborted is True  # 取消是**预期路径**（由 _audit_run 置位）
    run = await _latest_run(db, chat_id)
    assert run is not None and run.status == "stopped"
    assert run.finished_at is not None
    await _assert_no_zombie_running(db)


# --------------------------------------------------------------------------
# A6 · message_id 回填（HTTP 全链路）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_id_is_backfilled_through_chat_stream(client, db, monkeypatch):  # noqa: ANN001
    """A6：``POST /api/chat/stream`` 跑完一轮 react 后，``agent_runs.message_id`` == 落库的 assistant 消息 id。"""
    _install_empty_retrieval(monkeypatch)
    provider = _tool_call_provider("q")
    _install_model(monkeypatch, provider, answer_text="好的。")

    events = await _stream(
        client,
        {"message": QUESTION, "agent_mode": "react", "request_id": "req-audit-msgid"},
    )
    assert events[-1]["type"] == "done"
    assert events[-1]["agent_trace"]["mode"] == "react"

    await _refresh(db)
    chat_id = int((await db.execute(select(Message.chat_id).order_by(Message.id))).scalars().first())
    assistant_id = (
        await db.execute(
            select(Message.id)
            .where(Message.chat_id == chat_id, Message.role == "assistant")
            .order_by(Message.id.desc())
        )
    ).scalar_one()

    run = await _latest_run(db, chat_id)
    assert run is not None
    assert run.message_id is not None  # 关键：**不是** None（建行时还不知道 assistant id）
    assert run.message_id == assistant_id
    assert run.status == "done" and run.finished_at is not None


# --------------------------------------------------------------------------
# A7 · 审计不炸穿（I5）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_write_failure_does_not_break_the_turn(client, db, monkeypatch):  # noqa: ANN001
    """A7：审计写入失败 ⇒ 主流程照常（SSE 有 ``done``、``agent_trace`` 仍在），失败计数 +1。

    ⚠️ **失败点钉在「模块内部的行构造」上，而不是把 ``write_tool_step`` 整个换成抛异常的替身**：
    ``write_tool_step`` 的 ``try/except`` **就是**被测的隔离层，替换掉它等于绕过被测对象；
    而运行器侧不再包裹（设计如此，见 ``app/agent/audit.py`` 的「绝不炸穿」段），一个真抛的
    ``write_tool_step`` 会走进 CubeLoop 的工具错误通道——主流程活着，但**计数不会 +1**。
    让 ``AgentStep(...)`` 抛（库写失败的真实形态）才落在 ``safe_*`` 形状里。
    """
    _install_empty_retrieval(monkeypatch)

    class _BoomStep:
        """``agent_steps`` 行构造的替身：一构造就抛（等价于库拒绝这次写入）。"""

        def __init__(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("审计库写失败（用例注入）")

    agent_audit.reset_audit_write_failures()
    before = agent_audit.audit_write_failures()
    monkeypatch.setattr(agent_audit, "AgentStep", _BoomStep)

    provider = _tool_call_provider("q")
    _install_model(monkeypatch, provider, answer_text="好的。")
    events = await _stream(client, {"message": QUESTION, "agent_mode": "react"})

    assert events[-1]["type"] == "done"  # 主流程照常收尾
    trace = events[-1]["agent_trace"]
    assert trace["mode"] == "react" and trace["steps"]  # 轨迹仍在
    assert agent_audit.audit_write_failures() - before == 1  # 失败被**计数**，不是静默吞掉


# --------------------------------------------------------------------------
# A8 · AGENT_ENABLE_AUDIT=False
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_disabled_writes_nothing_and_keeps_behaviour(client, db, monkeypatch):  # noqa: ANN001
    """A8：审计关 ⇒ 两张表**零行**，且帧标题序列与开启时**逐字一致**（``done`` 帧正常）。"""
    _install_empty_retrieval(monkeypatch)

    async def run_once(request_id: str) -> list[dict]:
        provider = _tool_call_provider("q")
        _install_model(monkeypatch, provider, answer_text="好的。")
        return await _stream(
            client,
            {"message": QUESTION, "agent_mode": "react", "request_id": request_id},
        )

    # ① 关：零写入（表在用例开始时由 db 夹具清空，所以这里能断言「零行」）
    monkeypatch.setattr(settings, "AGENT_ENABLE_AUDIT", False)
    off = await run_once("req-audit-off")
    assert [e["type"] for e in off][-1] == "done"
    assert _agent_titles(off)  # 步骤流照常（关审计不影响编排）
    await _refresh(db)
    assert await _count(db, AgentRun) == 0
    assert await _count(db, AgentStep) == 0

    # ② 开：同一轮跑法 ⇒ 帧标题序列逐字一致（负向对照），这一次真的落两张表的行
    monkeypatch.setattr(settings, "AGENT_ENABLE_AUDIT", True)
    on = await run_once("req-audit-on")
    assert [e["type"] for e in on][-1] == "done"
    assert _agent_titles(on) == _agent_titles(off)
    assert on[-1]["agent_trace"]["mode"] == off[-1]["agent_trace"]["mode"] == "react"
    await _refresh(db)
    assert await _count(db, AgentRun) == 1
    assert await _count(db, AgentStep) >= 1
    await _assert_no_zombie_running(db)


# --------------------------------------------------------------------------
# A9 · 只读 / 辅助函数
# --------------------------------------------------------------------------


def test_safe_limit_clamps_into_range():
    """A9：``safe_limit`` 夹在 1..500，非法值回落 200（读接口不该因坏查询参数 500）。"""
    assert agent_audit.safe_limit(0) == 1
    assert agent_audit.safe_limit(-5) == 1
    assert agent_audit.safe_limit(9999) == 500
    assert agent_audit.safe_limit(200) == 200
    assert agent_audit.safe_limit(None) == 200
    # 边界与非法值（同一条夹取口径的其余取样点）
    assert agent_audit.safe_limit(1) == 1
    assert agent_audit.safe_limit(500) == 500
    assert agent_audit.safe_limit("12") == 12
    assert agent_audit.safe_limit("abc") == 200
    assert agent_audit.MAX_AUDIT_LIMIT == 500


def test_audit_enabled_follows_the_setting(monkeypatch):
    """A9：``audit_enabled()`` 跟随 ``settings.AGENT_ENABLE_AUDIT``；键缺失按**开**（老基线口径）。"""
    monkeypatch.setattr(settings, "AGENT_ENABLE_AUDIT", False)
    assert agent_audit.audit_enabled() is False
    monkeypatch.setattr(settings, "AGENT_ENABLE_AUDIT", True)
    assert agent_audit.audit_enabled() is True
    monkeypatch.delattr(settings, "AGENT_ENABLE_AUDIT", raising=False)
    assert agent_audit.audit_enabled() is True  # getattr 兜底：缺键 = 开


def test_clip_constants_are_pinned_equal_across_modules():
    """A9：审计与运行器的截断口径**两处相等**（``audit`` 有意不 import ``runner``，靠本用例钉住）。

    ⚠️ 验收原文写的是 ``runner.CONTENT_CLIP``，但 runner 侧的对应常量名是
    :data:`app.agent.runner.STEP_CLIP`（``agent_steps.content`` 与 ``tool/result.content``
    同源同口径；runner 里**没有** ``CONTENT_CLIP`` 这个名字）。这里按**实际常量**钉。
    """
    assert agent_audit.CONTENT_CLIP == runner.STEP_CLIP == 800
    assert agent_audit.ARGS_CLIP == runner.ARGS_CLIP == 200
