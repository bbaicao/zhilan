"""执行侧安全闸门单测（阶段 4 · 条目 4.3.1；验收编号 A1 / A2 / A3）。

钉的是 ``dangerous`` 的**第三道闸——执行点**。前三道里①MCP 装配（``build_specs`` 不为危险
工具生成 ``ToolSpec``）与②注册表（``ToolRegistry.for_model`` 过滤）编码的是**同一个假设**：
「危险工具根本没进注册表」。而 ``registry.register()`` 接受任何 ``dangerous=True`` 的 spec
（它只查重名），4.5.3 绑真 MCP transport 时工具是**运行时**从 server 握手拿到的——那一刻
闸①②都失效。本文件测的正是那一刻：**execute 拿到 dangerous spec 时拦不拦**。

## 危险工具的「前提」怎么构造（A1/A2 的核心）

``for_model()`` 一定会把 dangerous 的 spec 滤掉 ⇒ **走真实装配路径时它进不了模型清单**，
所以用例走「清单注入」：

```python
registry = registry_with(dangerous_spec)            # 前提①：spec 真的在注册表里
tool = runner._as_agent_tool(dangerous_spec, ...)   # 前提②：手工包出真的 AgentTool
frames = await collect(turn, question="q", model=..., registry=registry, tools=[tool])
```

``tools`` 注入**同时**传 ``registry`` 是有意的：ReAct 侧闸门的判据是 ``_as_agent_tool``
闭包**捕获的 spec 本体**（天然准确），P&E 侧走 ``_spec_map(tools, registry)`` **按名字反查**
——两条路径都要注册表在场。每条用例还显式断言 ``registry.get(name).dangerous is True``：
没有这一条，本文件就退化成 4.1 的「清单过滤」用例，**测不到执行点**。

⚠️ 注入 ``tools`` 时必须一起处理的一处：``stream_agent_turn`` 在内部**自建**
``ToolCollector``（``tools`` 注入不会把它交出来），而步骤流与 ``tool/*`` 事件都从**运行器那一份**
collector 取观察文本。故用例把 ``runner.ToolCollector`` 换成一个**预建的共享实例**
（``shared_collector`` 夹具），让注入的工具闭包与运行器读写同一份——否则被拦的那一帧会显示
``NO_HIT_TEXT``/空串，而不是冻结文案（观察文本其实产出了，只是没人读得到它）。

## A3 的路径选择（父代理要求二选一）

A3 用**运行器层 + 真 ``db`` 夹具**，不用 ``POST /api/chat/stream``。理由：dangerous 工具
**根本进不了 HTTP 链路**（HTTP 侧没有清单注入点，注册表由 ``build_registry`` 现建，
而 ``build_specs`` 连 spec 都不为危险的 MCP 工具生成），要端到端只能 monkeypatch 注册表内部，
那已经不是「真实路径」。运行器层这条已覆盖 A3 的实质：真 ``db`` + 真 ``session_events`` 表 +
真 ``_write_tool_events`` → ``safe_append_event`` 落库链路，只省掉 HTTP 外壳。

联网一律钉死：本目录不在 ``tests/api/`` 下，那边 conftest 的 autouse 钉子（``pin_web_off`` 等）
**不生效**，自己钉。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.agent import runner, safety
from app.agent.registry import ToolSpec
from app.agent.runner import AgentTurn, ToolCollector
from app.agent.types import ToolObservation
from app.core.config import settings
from app.models import Chat, SessionEvent
from tests.agent.test_runner import (  # 复用既有辅助（写法与 test_runner 同一套）
    RecordingModel,
    collect,
    faux_model,
    kinds_of,
    plan_json,
    registry_with,
    says,
    tool_call,
)

#: 危险工具的契约名形态（与 4.1 既有的 MCP 危险工具用例一致：``mcp:{slug}:{tool}``）
DANGEROUS_NAME = "mcp:evil:drop_table"
#: 负向对照那条同名形态的安全工具
SAFE_NAME = "mcp:evil:peek_table"


@pytest.fixture(autouse=True)
def pin_web_off(monkeypatch):
    """钉住联网开关。

    ``tests/api/conftest.py`` 的同名 autouse 夹具只对 ``tests/api/`` 生效；本文件在
    ``tests/agent/`` 下，本机 ``.env`` 的 ``WEB_ENABLE=true`` 会盖过 ``Settings`` 默认值，
    不钉就有真出网的风险（统筹 §3.4）。
    """
    monkeypatch.setattr(settings, "WEB_ENABLE", False)


class ProbeArgs(BaseModel):
    """替身工具的入参模型（真 ``ToolSpec`` 必须有 ``parameters`` 才能被 CubeLoop 调用）。"""

    query: str = ""


def probe_spec(handler, *, name: str, dangerous: bool) -> ToolSpec:
    """造一条真的 ``ToolSpec``（可指定 ``dangerous``）——用例的「危险工具」替身。"""
    return ToolSpec(
        name=name,
        description="用例替身工具",
        parameters_schema=ProbeArgs.model_json_schema(),
        handler=handler,
        dangerous=dangerous,
        parameters=ProbeArgs,
        needs_web=False,
        needs_confirm=False,
    )


def as_agent_tool(spec, *, collector: ToolCollector, turn: AgentTurn, db=None):
    """照 :func:`app.agent.runner._as_agent_tool` 的形参**手工**包一条 CubeLoop ``AgentTool``。

    这是用例构造前提的那一步：``build_agent_tools`` 走 ``for_model`` 会滤掉 dangerous，
    只有手工包才能让「dangerous 的 spec」与「真的在模型清单里的 AgentTool」同时成立。
    """
    from cubeloop import AgentTool, AgentToolResult
    from cubeloop.providers.base import TextContent

    return runner._as_agent_tool(
        spec, collector, AgentTool, AgentToolResult, TextContent, turn, db, None
    )


@pytest.fixture
def shared_collector(monkeypatch) -> ToolCollector:
    """让运行器用它自建的那一份 collector——与注入的工具闭包共用同一个实例。

    ``stream_agent_turn`` 内部 ``collector = ToolCollector()`` 是它自己的局部变量，
    ``tools`` 注入只换清单、不换 collector。不共用的后果是：工具把观察写进 A，
    步骤流与 ``tool/*`` 事件从 B 读 ⇒ ``observation.detail`` 退化成 ``NO_HIT_TEXT``、
    ``tool/result.content`` 变成空串（I4 的载荷不诚实，但不是本条目要测的东西）。
    """
    collector = ToolCollector()
    monkeypatch.setattr(runner, "ToolCollector", lambda: collector)
    return collector


async def _new_chat(db) -> int:
    """建一行真 ``chats``：``agent_runs.chat_id`` 有外键，假 id 会撞 FK。"""
    chat = Chat(title="安全闸门用例")
    db.add(chat)
    await db.flush()
    return int(chat.id)


# --------------------------------------------------------------------------
# A1 · ReAct 执行点闸门
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_react_execution_gate_blocks_dangerous_tool(shared_collector):
    """A1：ReAct 执行点闸门——dangerous 工具 handler **调用计数 == 0**，观察是冻结文案的 ``ok=False``。"""
    calls: list[str] = []

    async def handler(args):  # noqa: ANN001
        calls.append(args.query)
        return ToolObservation(text="不该被调用")

    spec = probe_spec(handler, name=DANGEROUS_NAME, dangerous=True)
    registry = registry_with(spec)
    # 前提必须真的成立（否则用例退化成 4.1 的清单用例、测不到执行点）
    assert registry.get(spec.name).dangerous is True
    # 闸②的事实：for_model() 一定滤掉它 ⇒ 真实装配路径给不出「它在清单里」这个前提
    assert [s.name for s in registry.for_model(web_enabled=False)] == []

    turn = AgentTurn(mode="react")
    tool = as_agent_tool(spec, collector=shared_collector, turn=turn)
    model = faux_model([tool_call(spec.name, {"query": "drop"}), says("好")])
    frames = await collect(turn, question="问题", model=model, registry=registry, tools=[tool])

    assert calls == []  # handler 一次都没被调用
    observations = [f for f in frames if f["kind"] == "observation"]
    assert len(observations) == 1
    assert observations[0]["ok"] is False
    assert observations[0]["detail"] == safety.DANGEROUS_BLOCKED_TEMPLATE.format(tool=spec.name)
    # 「拦了」必须看得见：仍是 tool + observation 两帧，不是静默跳过
    assert kinds_of(frames)[:2] == ["tool", "observation"]
    assert frames[0]["tool"] == spec.name  # 真的走到了执行点（模型点名 → CubeLoop 调了它）
    assert frames[0]["tool_call_id"] == observations[0]["tool_call_id"]


@pytest.mark.asyncio
async def test_safe_tool_of_the_same_shape_is_really_executed(shared_collector):
    """A1 负向对照：``dangerous=False`` 的同类工具 handler **真的被调用**——闸门不是「一律不执行」。"""
    calls: list[str] = []

    async def handler(args):  # noqa: ANN001
        calls.append(args.query)
        return ToolObservation(text="安全工具真的跑了")

    spec = probe_spec(handler, name=SAFE_NAME, dangerous=False)
    registry = registry_with(spec)
    assert registry.get(spec.name).dangerous is False
    # 与 A1 的差别**只有** dangerous 这一位：这条本来就会被 for_model 放行
    assert [s.name for s in registry.for_model(web_enabled=False)] == [spec.name]

    turn = AgentTurn(mode="react")
    tool = as_agent_tool(spec, collector=shared_collector, turn=turn)
    model = faux_model([tool_call(spec.name, {"query": "peek"}), says("好")])
    frames = await collect(turn, question="问题", model=model, registry=registry, tools=[tool])

    assert calls == ["peek"]  # 真的执行了
    observation = [f for f in frames if f["kind"] == "observation"][0]
    assert observation["ok"] is True
    assert observation["detail"] == "安全工具真的跑了"
    assert observation["detail"] != safety.DANGEROUS_BLOCKED_TEMPLATE.format(tool=spec.name)


# --------------------------------------------------------------------------
# A2 · P&E 执行点闸门（单独存在，不许用 A1 代替）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_execute_gate_blocks_dangerous_tool(shared_collector):
    """A2：P&E 执行点闸门（``_run_plan_step`` 经 ``_spec_map`` 反查 spec）——同样 0 次执行、同样文案。"""
    calls: list[str] = []

    async def handler(args):  # noqa: ANN001
        calls.append(args.query)
        return ToolObservation(text="不该被调用")

    spec = probe_spec(handler, name=DANGEROUS_NAME, dangerous=True)
    registry = registry_with(spec)
    assert registry.get(spec.name).dangerous is True
    assert [s.name for s in registry.for_model(web_enabled=False)] == []

    turn = AgentTurn(mode="plan_execute")
    tool = as_agent_tool(spec, collector=shared_collector, turn=turn)
    model = RecordingModel(
        [plan_json({"goal": "把库干掉", "tool": spec.name, "args": {"query": "drop"}})]
    )
    frames = await collect(turn, question="问题", model=model, registry=registry, tools=[tool])

    assert calls == []  # P&E 这条路径也一次都没执行
    assert kinds_of(frames) == ["plan", "thought", "tool", "observation"]
    observation = frames[3]
    assert observation["ok"] is False
    assert observation["title"] == "工具执行失败"
    assert observation["detail"] == safety.DANGEROUS_BLOCKED_TEMPLATE.format(tool=spec.name)
    assert observation["tool_call_id"] == frames[2]["tool_call_id"]  # 一次调用一个 id（I4）


# --------------------------------------------------------------------------
# A3 · 闸门也留痕（真 db + 真 session_events）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_step_leaves_paired_events_with_the_frozen_text(db, shared_collector):
    """A3：被拦的这一步仍产 ``tool`` + ``observation`` 两帧，且 ``tool/call`` ↔ ``tool/result`` 同 id 成对落库。

    用**真 db 夹具 + 真 chat_id**（理由见模块 docstring 的「A3 的路径选择」）：
    落库走的是运行器真实的 ``_write_tool_events`` → ``safe_append_event``，只是省掉 HTTP 外壳。
    """
    chat_id = await _new_chat(db)
    calls: list[str] = []

    async def handler(args):  # noqa: ANN001
        calls.append(args.query)
        return ToolObservation(text="不该被调用")

    spec = probe_spec(handler, name=DANGEROUS_NAME, dangerous=True)
    registry = registry_with(spec)
    assert registry.get(spec.name).dangerous is True

    turn = AgentTurn(mode="react")
    tool = as_agent_tool(spec, collector=shared_collector, turn=turn, db=db)
    model = faux_model([tool_call(spec.name, {"query": "drop"}), says("好")])
    frames = await collect(
        turn,
        question="问题",
        model=model,
        registry=registry,
        tools=[tool],
        db=db,
        chat_id=chat_id,
        turn_no=1,
    )

    assert calls == []
    frozen = safety.DANGEROUS_BLOCKED_TEMPLATE.format(tool=spec.name)
    assert kinds_of(frames)[:2] == ["tool", "observation"]
    observation = frames[1]
    assert observation["ok"] is False
    assert observation["detail"] == frozen

    rows = await db.execute(
        select(SessionEvent)
        .where(
            SessionEvent.chat_id == chat_id,
            SessionEvent.type.in_(("tool/call", "tool/result")),
        )
        .order_by(SessionEvent.seq)
    )
    events = list(rows.scalars().all())
    assert [e.type for e in events] == ["tool/call", "tool/result"]  # 两帧事件都在（不静默跳过）
    call, result = events
    assert call.tool_call_id and call.tool_call_id == result.tool_call_id  # I4 配对
    assert call.tool_call_id == observation["tool_call_id"]  # 步骤流与日志同一个 id
    assert call.data["tool"] == spec.name
    assert result.data["ok"] is False
    assert frozen in result.data["content"]  # 冻结文案真的落库（会被 4.4 的投影读到）


# --------------------------------------------------------------------------
# 专项「工具调用能力」第 0 批 · A19 · **I6 地板**（被拦工具的参数与观察文本不进回答数据区）
# --------------------------------------------------------------------------
#
# 开工包 A19 的复核**改过判据**（原「请求体里 grep 不到危险工具名」与 D1/D2′/D6 互斥、
# 且天然绿）：D1 的 ``ToolContextItem`` **带 ``tool``（工具名）字段** ⇒ 工具名本身**可以**
# 出现，不得拿「名字不出现」当判据。新的判据盯**参数与观察文本**。


@pytest.mark.asyncio
async def test_a19_blocked_tool_arguments_never_reach_the_answer_channel(shared_collector):
    """A19：被闸拦下的危险工具 —— ① `dangerous` 仍不进模型清单；② **参数与观察文本
    不出现在回答请求体的数据区**；③ `tool/call` 与 `tool/result` **仍成对**（I4 不破）；
    ④ 观察文本里**没有参数回显**。

    「回答请求体的数据区」在这里 = ``turn.tool_context``（第 0 批新通道）
    + ``plan.materials_text`` 的语境 —— 两者都由 ``ToolCollector`` 供料，
    所以判据落在 ``turn.tool_context`` 上（**通道的唯一产物**）。

    ⚠️ 与 A1/A3 的区别：那两条钉「拦得住、留得下痕」；本条钉「拦下的东西**不会**被
    第 0 批的新通道**带进最终回答请求体**」—— 新通道是**新增的泄漏面**，
    没有这条，被拦工具的参数就可能顺着 ``observation.text`` 进回答上下文。

    ⚠️ **必须用 ``shared_collector`` 夹具**（本文件 A1 的同款纪律）：``stream_agent_turn``
    内部 ``collector = ToolCollector()`` 是它自己的局部变量，自己新建一个 collector 传进去
    只会让 `_steps_for` 从**另一个**实例读 —— 观察详情退化成 ``NO_HIT_TEXT``（实测：
    「没有检索到相关资料。」），本条判据当场失去意义。
    """
    secret = "T0-SECRET-ARG-不该进回答请求体"
    calls: list[str] = []
    collector = shared_collector

    async def handler(args):  # noqa: ANN001
        calls.append(args.query)
        return ToolObservation(text=f"不该被调用（{secret}）")

    spec = probe_spec(handler, name=DANGEROUS_NAME, dangerous=True)
    registry = registry_with(spec)

    # ① 前提：危险工具**不进模型可见清单**（I6 地板的第一层）
    assert registry.get(spec.name).dangerous is True
    assert [s.name for s in registry.for_model(web_enabled=False)] == []

    turn = AgentTurn(mode="react")
    tool = as_agent_tool(spec, collector=collector, turn=turn)
    # 构造「模型**显式**点名一个被过滤掉的危险工具，并把敏感值当参数递过去」这条路
    model = faux_model([tool_call(spec.name, {"query": secret}), says("好")])
    frames = await collect(turn, question="问题", model=model, registry=registry, tools=[tool])

    # 被拦：handler 一次都没跑（参数没有被执行，也没有被回显进观察）
    assert calls == []
    frozen = safety.DANGEROUS_BLOCKED_TEMPLATE.format(tool=spec.name)
    observation = next(f for f in frames if f["kind"] == "observation")
    assert observation["ok"] is False
    assert observation["detail"] == frozen
    assert secret not in observation["detail"], "观察文本回显了参数"

    # ② 通道（= 回答请求体的数据区来源）里：观察文本在，**参数不在**
    items = turn.tool_context
    assert isinstance(items, list)
    joined = " ".join(f"{i.tool}|{i.text}" for i in items)
    assert secret not in joined, f"被拦工具的参数进了回答数据区：{joined}"
    assert all(secret not in str(i.text) for i in items)
    # 工具名**可以**出现（D1 的五键里有 `tool`）—— 这条是「不要把名字当判据」的正面钉
    assert any(i.tool == spec.name for i in items), "被拦条目连工具名都没进通道（D6 要求它可读）"
    assert all(frozen in i.text or i.text == "" for i in items if i.tool == spec.name)

    # ③ I4 不破：`tool/call` ↔ `tool/result` 仍成对（第 0 批不产生额外事件）
    assert kinds_of(frames)[:2] == ["tool", "observation"]
    assert frames[0]["tool_call_id"] == observation["tool_call_id"]
    assert [i.tool_call_id for i in items if i.tool == spec.name] == [
        observation["tool_call_id"]
    ]

# --------------------------------------------------------------------------
# 阶段 5 · `回归检查` · **B2 负控**：P&E 反查不到的裸 AgentTool 不再「一律拦」
# --------------------------------------------------------------------------
#
# 为什么必须补这一条（`audit-write` 的 B2 发现，2026-09-26）：
#
# `回归检查` 把 `safety.is_blocked` 改成**反向 fail-closed**（`needs_confirm` 取不到 ⇒ 拦）之后，
# A2（上面那条 P&E 用例）的判别力**结构性地下降**了：
#
#   · **改前**：A2 的拦下靠**闸②**（`_spec_map` 按名字反查**命中** `dangerous=True` 的 spec）；
#   · **改后**：即使 `_spec_map` 彻底坏掉 / 反查落空（拿到裸 `AgentTool`），
#     闸① 的 fail-closed **也会拦** ⇒ **A2 照绿**。
#
# ⇒ 「反查正确」与「一律拦」在 A2 上**不可区分** —— 这正是 `D-回归检查` 对 A20 腿② 所说的
#   「没有负控，把 `is_blocked` 改成 `return True` 也能过」的**同型病**。
#
# ⇒ 本用例补**负控**：P&E 路径上注入一个 `needs_confirm=False` 的**安全裸 AgentTool**，
#   **必须真的被执行**（handler 调用计数 > 0）⇒ 证明闸① 不是「一律拦」。


@pytest.mark.asyncio
async def test_b2_plan_execute_still_runs_a_safe_unresolvable_tool(shared_collector):
    """**负控（B2）**：P&E 侧反查落空的**安全**工具 ⇒ **必须真执行**（不得被 fail-closed 误拦）。

    构造：注入一个 `needs_confirm=False` 的**安全**工具，且**名字就在注册表里** ⇒
    `runner.py:2353` 的 `spec = spec_map.get(tool_name, tool)` **反查命中真 spec**
    ⇒ 改后的闸① 放行 ⇒ handler 必须被调用。

    若本条失败（calls 为空）⇒ 说明闸① 退化成了「**一律拦**」——
    那时 A2 仍会因为「拦住了」而绿，但**真实原因是错的**（`A20` 腿② 的同型病）。

    ⚠️ **一条如实登记**：P&E 的 `spec_map` 反查**要求工具名在注册表里**；
    「注入裸 `AgentTool` 且名字不在注册表」这一形态**仍会被闸① 拦下** ——
    那是 fail-closed 的**预期**行为（拿不到声明面 ⇒ 不敢放行），**不是缺陷**。
    本用例只钉「反查命中 ⇒ 放行」这一条，不主张裸工具应当放行。
    """
    calls: list[str] = []

    async def handler(args):  # noqa: ANN001
        calls.append(args.query)
        return ToolObservation(text="安全工具真的跑了")

    # 安全 spec（needs_confirm=False）且**名字就在注册表里** ⇒ `_spec_map` 反查**命中**
    spec = probe_spec(handler, name=SAFE_NAME, dangerous=False)
    registry = registry_with(spec)
    assert safety.is_blocked(spec) is False, "前提①：spec 本体是安全的（显式声明）"
    assert registry.get(SAFE_NAME) is spec, "前提②：反查必须命中（否则落回裸 AgentTool）"

    turn = AgentTurn(mode="plan_execute")
    tool = as_agent_tool(spec, collector=shared_collector, turn=turn)

    model = RecordingModel(
        [plan_json({"goal": "安全地看一眼", "tool": SAFE_NAME, "args": {"query": "peek"}})]
    )
    frames = await collect(
        turn, question="问题", model=model, registry=registry, tools=[tool]
    )

    # 🔴 负控的核心断言：**真的执行了**（不是被「一律拦」蒙过）
    assert calls == ["peek"], (
        "反查命中的**安全**工具必须真执行 —— 若这里为空，说明闸① 退化成了「一律拦」"
    )
    observations = [f for f in frames if f["kind"] == "observation"]
    assert observations, f"必须有观察帧：{kinds_of(frames)}"
    assert observations[0]["ok"] is True
    assert observations[0]["detail"] == "安全工具真的跑了"
