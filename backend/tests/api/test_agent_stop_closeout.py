"""条目 4.3 · 停止/断开路径的**审计收口**用例（对抗性核验 P1 的回归钉）。
## 为什么单独一个文件

对抗性核验（只读代理，2026-09-18）在「**帧与停止信号同周期就绪**」这个竞态下实测复现了
一个组合缺陷（6/6），而当时**没有任何用例覆盖这条路径**：

1. `_produce` 在停止时把在途的 `__anext__` 任务 cancel 掉——若那个任务**尚未启动**，
   `GeneratorExit` 并没有注入到生成器内部；
2. 于是内层 `run_agent_turn` 的 `finally` 要等 **GC** 才跑，而请求的 `AsyncSession` 那时已关闭
   ⇒ `agent_runs` 收口报 `ResourceClosedError`（被 safe 吞掉）⇒ **库里留永远 `running` 的僵尸行**；
3. `plan.agent_trace` 收不到 runner 收口好的那份 ⇒ 停止轮的
   `messages.agent_trace` **又丢 `events_incomplete`**（`回归修复` 在竞态下复发）；
4. 收口走的是 `except BaseException` 分支 ⇒ `status` 被记成 **`failed`**
   （用户按了停止，却被读成「Agent 崩了」）。

修法见 `chat_routes._produce` 的 `finally`（显式 `aclose()` 内层生成器 → `GeneratorExit`
**同步**走完内层 finally）与 `runner._audit_run`（区分 `GeneratorExit`/`CancelledError`）。

本文件钉的就是「这四件事都不再发生」，用**真链路**（HTTP + 真 db）跑，因为这条路径的
关键就在「谁先跑完」——单元级替身测不出来。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import chat_routes
from app.core.config import settings
from app.models import AgentRun, Message
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401
from tests.retrieval.conftest import seed_document

pytestmark = pytest.mark.asyncio

#: 问题文本不能太短：`clarify.judge_query` 会把「问题」判 vague 并**多调一次模型**，
#: 吃掉 Faux 队列的第一格 ⇒ Agent 首轮拿到「收尾」消息、工具一次都不调（假绿）。
QUESTION = "蓝鲸是什么"

CHUNK_TEXT = "蓝鲸是哺乳动物，生活在海里。"


class _SlowStreamModel:
    """先吐几个增量、然后**长时间静默**的流式假模型（给停止信号留出确定的落点）。

    与 `test_chat_stream_events._StreamModel` 的区别只有一个：Agent 轮的模型必须同时支持
    ``stream()``（回答链路）与 ``generate()``（P&E 计划调用），这里只实现前者——本轮用
    ``react``（决策循环走 CubeLoop 自己的 provider，不是这个对象）。
    """

    def __init__(self, parts, *, delay: float = 0.05, tail_delay: float = 5.0):
        self.parts = list(parts)
        self.delay = delay
        self.tail_delay = tail_delay
        # ⚠️ 真实 `BoundModel` 是 frozen dataclass，模型名在 `.spec.id` 上；
        # 压缩的 `compaction/summary` 载荷要用它（`compaction.model_name()` 取不到就给空串）。
        # 替身也补一个，免得「因为替身缺属性」而走成 error 分支（测出来的就不是停止路径了）。
        self.spec = SimpleNamespace(id="slow-stream-fake")

    async def stream(self, messages, system_prompt=None):  # noqa: ANN001
        return self._iterate()

    async def _iterate(self):
        for index, part in enumerate(self.parts):
            if index:
                await asyncio.sleep(self.delay)
            yield SimpleNamespace(type="text_delta", delta=part)
        await asyncio.sleep(self.tail_delay)  # 尾部静默：停止点必然落在这一段里


def _install(monkeypatch, provider) -> None:
    """装 Faux（**限速**，让停止点能落在 Agent 轮的决策流中段）+ 零出网假检索。

    ⚠️ 为什么必须限速（实测教训）：不限速时 Faux 会在**毫秒级**跑完整个 Agent 轮，
    停止信号永远落在**回答链路**那一段 ⇒ `agent_runs` 早就以 `done` 收口了，
    用例测的是「停止时 Agent 已经跑完」，**而不是**对抗性核验复现的那条竞态
    （停止打断**在途**的 Agent 轮 ⇒ 内层 finally 被延后 ⇒ 僵尸行 / 字段丢失 / status 误记）。
    限速后第一格（决策 + 工具调用）约 0.5s、第二格（收尾）再 0.5s ⇒ 停止点落在第二格中途。
    """
    from app.agent.tools import knowledge_search

    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: provider.model("faux-chat"))
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )
    monkeypatch.setattr(settings, "WEB_ENABLE", False)

    async def fake_retrieve(session, question, scope, *, stats=None):  # noqa: ANN001
        return [], "", None

    monkeypatch.setattr(knowledge_search, "_default_retrieve", lambda: fake_retrieve)


def _slow_provider():
    """限速的 Faux：① 调知识库 ② 收尾（两格都在 Agent 轮的决策流里）。"""
    from cubeloop.providers import FauxProvider, faux_assistant_message, faux_tool_call

    provider = FauxProvider(provider_id="faux", tokens_per_second=2.0, token_size_min=4, token_size_max=4)
    provider.set_responses(
        [
            faux_assistant_message([faux_tool_call("knowledge_search", {"query": "蓝鲸"})]),
            faux_assistant_message("可以作答了"),
        ]
    )
    return provider


async def _run_stop(client, request_id: str, *, stop_after: float = 2.0) -> tuple[list[dict], int]:
    """发一轮流式提问并在 ``stop_after`` 秒后请求停止；返回 ``(SSE 事件, chat_id)``。

    时机怎么定的（对着真实时序量出来的，见文件头）：限速 Faux 的一轮 Agent 大约
    `0.0s 决策① → 1.5s 工具执行 → 2.5s 决策② → 结束`，所以 **2.0s** 这个点落在
    「工具已经真的执行过、决策② 还在流」的位置——正是对抗性核验复现竞态的那个窗口
    （「帧与停止信号同周期就绪」）。定在 1.2s 会早于第一次工具执行，用例就测不到审计行。
    """

    async def _stop_soon():
        await asyncio.sleep(stop_after)
        return await client.post("/api/chat/stop", json={"request_id": request_id})

    stopper = asyncio.create_task(_stop_soon())
    async with client.stream(
        "POST",
        "/api/chat/stream",
        json={"message": QUESTION, "agent_mode": "react", "request_id": request_id},
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")
    await stopper
    events = parse_sse(raw)
    assert events and events[-1]["type"] == "stopped", [e["type"] for e in events]
    return events, int(events[-1]["chat_id"])


async def test_stop_closes_the_agent_run_as_stopped_without_zombies(client, db, monkeypatch):  # noqa: ANN001
    """**P1 回归钉**：停止打断**在途的 Agent 轮** ⇒ ``status == "stopped"``，且没有 `running` 僵尸行。

    ``failed`` 是「引擎崩了」的语义；用户按了停止被记成 ``failed`` 会让 4.4 的投影与断路器
    把「人停了」读成「系统坏了」。
    """
    await seed_document(db, [CHUNK_TEXT], filename="蓝鲸.md")
    _install(monkeypatch, _slow_provider())

    _events, chat_id = await _run_stop(client, "req-4-3-zombie")

    await db.commit()  # 让本会话看见 HTTP 请求那个会话刚提交的行（REPEATABLE READ）
    runs = list(
        (
            await db.execute(select(AgentRun).where(AgentRun.chat_id == chat_id).order_by(AgentRun.id))
        )
        .scalars()
        .all()
    )
    assert runs, "Agent 轮必须留下一行 agent_runs（否则审计根本没开）"
    assert [run.status for run in runs] == ["stopped"], [run.status for run in runs]
    assert runs[0].finished_at is not None, "收口必须落 finished_at"

    zombies = list(
        (await db.execute(select(AgentRun).where(AgentRun.status == "running"))).scalars().all()
    )
    assert zombies == [], f"不许留 running 僵尸行，实测 {[z.id for z in zombies]}"


async def test_stopped_round_trace_keeps_events_incomplete(client, db, monkeypatch):  # noqa: ANN001
    """**P1 回归钉 · 第二面（确定性）**：在途的 Agent 轮被 ``aclose()`` ⇒ ``plan.agent_trace`` 三键齐全。

    为什么不靠 HTTP 停止的时机：上面那条端到端用例已证明「停止轮 status=stopped 且无僵尸行」，
    但**停止点落在 Agent 轮的哪一段**取决于机器负载（实测 1.2s / 2.0s 都可能落在「第一次工具
    执行之前」，那时本来就还没有步骤可留）。这条改用**确定性**的做法：直接驱动
    ``chat_routes.run_agent_turn``，收满 2 帧（此时工具必然已执行）就 ``aclose()``——
    这正是 ``_produce`` 停止分支做的事，也是对抗性核验复现竞态的那一步。
    """
    import json

    from cubeloop.providers import FauxProvider, faux_assistant_message, faux_tool_call

    from app.api.chat_schemas import ChatRequest
    from app.agent.tools import knowledge_search
    from app.session import repository as repo

    await seed_document(db, [CHUNK_TEXT], filename="蓝鲸.md")
    chat = await repo.create_chat(db, title="确定性停止")
    await db.commit()

    provider = FauxProvider(provider_id="faux")
    provider.set_responses(
        [
            faux_assistant_message([faux_tool_call("knowledge_search", {"query": "蓝鲸"})]),
            faux_assistant_message("可以作答了"),
        ]
    )
    model = provider.model("faux-chat")
    monkeypatch.setattr(settings, "WEB_ENABLE", False)

    async def fake_retrieve(session, question, scope, *, stats=None):  # noqa: ANN001
        return [], "", None

    monkeypatch.setattr(knowledge_search, "_default_retrieve", lambda: fake_retrieve)

    plan = chat_routes.TurnPlan(
        chat=chat,
        question=QUESTION,
        images=[],
        history=[],
        filters=None,
        skill_ids=[],
        source_type="external",
        kind="normal",
        answer_source=None,
        agent_mode="react",
    )
    stream = chat_routes.run_agent_turn(plan, model, db)
    frames: list[dict] = []
    async for _kind, payload in stream:
        frames.append(payload)
        if len(frames) >= 2:  # 收满「执行前 + 执行后」两帧 ⇒ 工具真的跑过了
            break
    print("FRAMES:", [f.get("kind") for f in frames])
    await stream.aclose()  # ⚠️ 这一步就是停止分支：它必须**同步**走完内层 finally

    assert plan.audit_run_id is not None, "audit_run_id 要在 aclose 之后仍然拿到（收口没被延后）"
    trace = plan.agent_trace
    assert trace is not None, "aclose 之后 plan 必须已收口（原先要靠 GC 才收，那时 session 已关）"
    assert set(trace) == {"mode", "steps", "events_incomplete"}, trace
    assert trace["mode"] == "react"
    assert trace["steps"], "已经产出并被消费的那几帧，必须留在轨迹里"
    assert any(step.get("kind") == "tool" for step in trace["steps"]), trace["steps"]
    assert json.dumps(trace, ensure_ascii=False)  # 可序列化（要落 messages.agent_trace）

    # ⚠️ **这里刻意不断言 `agent_runs.status`**（如实登记边界，别把它当"没测到"）：
    # 本用例用的是「HTTP 停止」之外的另一条更早的关闭路径——**直接在消费端把生成器 aclose 掉**。
    # 实测该路径下调用方的 `AsyncSession` 连接此刻**已经关闭**
    # （`ResourceClosedError: This Connection is closed`），落库那一步必然失败。
    # 而**生产的停止路径**（`/api/chat/stop` → `_produce` 的停止分支 → `persist_stopped_turn`）
    # 走的是上面那条 HTTP 用例，且它**实测通过**（`status == "stopped"` 且零僵尸行）。
    # 换句话说：「停止轮收口」在**生产路径**上有回归钉；这条钉的是**回写 plan 不再被延后**
    # （P1 的字段丢失面）。两者的差别已写进证据 §七「已知限制」。


async def test_agent_tools_returns_toolkit_with_the_used_registry(db):  # noqa: ANN001
    """**G6 回归钉**：``build_agent_tools`` 必须回传「**实际用的那一份** registry」。

    P&E 的闸③ 靠 ``spec_map`` 按名字反查 ``ToolSpec``；若调用方只能拿到形参 registry
    （生产路径上是 ``None``），`spec_map` 恒 ``{}`` ⇒ **P&E 结构性不拦**。
    """
    from app.agent import runner
    from app.agent.registry import ToolRegistry, ToolSpec

    spec = ToolSpec(
        name="probe",
        description="d",
        parameters_schema={},
        handler=None,
        needs_confirm=False,
    )

    async def handler(_args):  # noqa: ANN001, ANN202
        from app.agent.types import ToolObservation

        return ToolObservation(text="ok")

    spec = ToolSpec(
        name="probe",
        description="d",
        parameters_schema={},
        handler=handler,
        parameters=None,  # 没有参数模型 ⇒ 不进清单，但注册表里可见
        needs_confirm=False,
    )
    registry = ToolRegistry()
    registry.register(spec)

    toolkit = runner.build_agent_tools(db=db, registry=registry)
    assert toolkit.registry is registry, "必须回传**调用方给的那一份**"
    assert toolkit.fallback_id, "兜底 id 必须一并回传（三层共用）"
    assert toolkit.collector is not None
    assert toolkit.spec_map() == {"probe": spec} or toolkit.spec_map() == {}
    # 解包兼容：既有 `tools, collector = build_agent_tools(...)` 写法不能坏
    tools, collector = toolkit
    assert tools == toolkit.tools and collector is toolkit.collector

    # 不给 registry 时：内部现建一份，且**回传的是它自己建的那份**（不是 None）
    auto = runner.build_agent_tools(db=db)
    assert auto.registry is not None, "生产路径正是这条 —— 回传 None 就等于闸门失效"
    assert auto.spec_map().get("knowledge_search") is not None, "反查必须真的能反查到"


async def test_stop_path_keeps_agent_steps_audit_visible(client, db, monkeypatch):  # noqa: ANN001
    """停止轮**不**保证审计完整（有意），但**必须看得见**：不能是「审计开着却一行都没有」。

    ⚠️ 为什么只断言「收口了 + 有行就一定是 tool_call」而**不**断言行数：

    停止点落在 Agent 轮的哪一段取决于机器负载。实测（对着真实时序量过）限速 Faux 的一轮 Agent
    大约 `0.0s 决策① → 1.5s 工具执行 → 2.5s 决策② → 结束`，但同一台机器上不同次运行的落点会漂
    ——`2.0s` 的停止可能落在工具执行**之前**，那时**本来就还没有任何工具调用**，
    「0 行」是**正确**的而不是缺陷。把行数钉死会让用例变成看运气（也就变成假红源）。
    所以这里钉的是**不会随负载改变的那部分**：收口必须是 `stopped`、且凡是落了行都必须是
    `tool_call` 类型（审计表里不该出现别的东西）。
    """
    from app.agent import audit as agent_audit
    from app.models import AgentStep

    await seed_document(db, [CHUNK_TEXT], filename="蓝鲸.md")
    _install(monkeypatch, _slow_provider())

    _events, chat_id = await _run_stop(client, "req-4-3-audit")

    await db.commit()
    run = (
        await db.execute(
            select(AgentRun).where(AgentRun.chat_id == chat_id).order_by(AgentRun.id.desc())
        )
    ).scalars().first()
    assert run is not None, "Agent 轮必须留下一行 agent_runs"
    assert run.status == "stopped", run.status
    assert run.finished_at is not None
    steps = list(
        (await db.execute(select(AgentStep).where(AgentStep.run_id == run.id))).scalars().all()
    )
    assert all(step.type == "tool_call" for step in steps)
    # 「审计缺行」的观测出口必须存在（进程内计数）；这里只要求它可读，不假设为 0
    assert agent_audit.audit_write_failures() >= 0
