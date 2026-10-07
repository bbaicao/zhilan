"""专项 · 工具调用能力第 1 批 · **副作用面 / 停轮面**的判据补钉（A4 / A23 / A18①）+ 一条窄路径定性。

## 这份文件为什么存在

第 1 批（提交 `6eb3a73`）的三份用例里，开工包 §4 的 **A4 / A23** 是**零判据**、**A18①**
（「决策 / 收口任一步抛错不炸穿」）没有定向用例 —— 独立复核（`证据/_独立复核-20260922/`）
用变异实证过：把 `run_agent_turn` 的守卫 `if completed and not turn.aborted:` 改成
`if completed:`（= 停止轮也走收口），既有停止用例 **全绿**（0 failed / 7 passed）。
本文件把这三条补齐，并按同一份复核的「功能性偏离 #2」把一条**窄路径**写成用例定性。

## 三条判据的断法（都刻意避开「必然绿」的写法）

- **A4**：`kind == "clarify"` / `refs == []` 这类是 `persist_turn` / `_apply_agent_closeout`
  **写死**的，断了也必然绿 ⇒ 只断**真会被污染的四样**：真库 `chats.clarify_rounds` +1、
  `usage_events` **实际决策发送恰好一行**、`schedule_extract` **零调度**、`assistant/attempt` 条数与
  `off` 链**相同**。
  OPT5 后 `record_answer_usage` 只保留兼容入口；真实发送才记账，澄清轮没有最终回答
  也不能抹去已发生的决策请求。
- **A23**：`kind == "stopped"` / `refs == []` 由 `persist_stopped_turn` **写死** ⇒ 同样不断。
  改断：① 落库 **user 行**的 `source_type`；② `turn/end` 的 `final_material_count` /
  `final_empty_reason`（`plan.materials_finalized` 为假 ⇒ **两键应缺席**）；
  ③ 停止轮交给 `persist_stopped_turn` 的那份 `plan` 里 **`plan.materials` 仍是候选快照**
  （未被 `_apply_agent_closeout` 替换 / 清空）。
- **A18①**：两个注入点各一条腿（决策步 = `runner.parse_closeout_message`；收口步 =
  `chat_routes._apply_agent_closeout`），断言**不炸穿**（HTTP 200 且流走完）+ **恰好一次**收尾帧。

## 窄路径（**已定性 ⇒ 已修 ⇒ 现为硬钉子**）

「**前置候选为空** + 助手用工具检到材料 + 协议块**坏**」⇒ 收口复核时实测：
`chat_routes._apply_agent_closeout` 的 invalid 分支是 `if not invalid and turn.materials:`
⇒ **助手材料被整个丢掉**（对照改前 `if turn.materials:` 是无条件安装）⇒ 登记为
**回归修复** 并单点修复（条件改成 `if turn.materials and (not invalid or not plan.materials):`
⇒ 有候选仍优先采候选、只在「没有候选可采」时接住助手材料）。
实测读数（`test_narrow_invalid_closeout_keeps_agent_materials`）：改前 `plan.materials=[]`、
`kind=direct`、回答请求体里**资料区整段消失**（只剩工具结果通道那一段）；同一构造把协议块
换成合法值（`test_narrow_control_valid_closeout_keeps_agent_materials`）材料就保留
⇒ 差异**只**由 invalid 分支造成。**修后两条腿都绿，本文件不再有 xfail。**
⚠️ 材料**只能**来自带 `materials` 的工具结果：MCP 工具的结果按产品冻结语义**恒无材料**
（`app/agent/tools/mcp.py:_make_handler`），所以「助手用 `fake_mcp` 工具」这一腿承载的是
**工具调用/步骤**（`fake_mcp` 夹具注入的 `mcp:fauxkit:probe`），材料那一腿由
`knowledge_search`（注入假检索）承载 —— 两条腿都在同一轮里真的跑过，可各自从 SSE 步骤帧看到。

## 夹具坑（照 `t0_wire` 的口径）

本文件在 `tests/api/` 下 ⇒ `conftest.py` 的 9 个 autouse 钉子已生效（含 `pin_agent_mode_off`
/ `pin_memory_off`）⇒ 请求体**必须显式给 `agent_mode`**；要真测「记忆有没有被调度」必须
**显式打开 `settings.MEMORY_ENABLE`**（见 A4 用例里的注释）。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import func, select

from app.agent import runner as runner_mod
from app.agent.tools import knowledge_search
from app.api import chat_routes
from app.core.config import settings
from app.generation import clarify
from app.generation import source_map
from app.models import Chat, Chunk, Message, UsageEvent
from app.retrieval.types import Material, number, render_materials
from app.session import events as event_log
from app.session import memory as memory_mod
from tests.api.t0_wire import (  # noqa: F401 —— client / wire_env / fake_mcp 是夹具
    FAKE_TOOL_CONTRACT,
    Wire,
    attach_wire,
    client,
    fake_mcp,
    run_stream,
    wire_env,
    wire_sse_text,
    wire_sse_tool_call,
)
from tests.api.test_chat_contract import _StubClassifier, parse_sse
from tests.retrieval.conftest import seed_document

pytestmark = pytest.mark.asyncio

#: 短于 ``clarify.MIN_LENGTH``（现为 4）的非空中文 —— A4 的两条腿用**同一句**。
SHORT = "好"
#: Agent 自决澄清的追问文本。
ASK = "你指的是哪一份资料？"
#: 正常长度的问题（避免落进 off 链的短句澄清）。
MSG = "帮我看一下资料"
#: 前置候选的正文（A23 要它**在停止轮里活下来**）。
CANDIDATE = "候选句子甲乙丙。"
#: 助手用工具检到的材料正文（窄路径判据的哨兵串）。
AGENT_MATERIAL = "助手用工具检到的材料丁戊己庚。"
#: A23 的停止轮 request_id（`/api/chat/stop` 用它找登记）。
STOP_REQUEST_ID = "t1-side-effects-stop"

#: 合法协议块（`action=answer` + **明确** `use_candidates=False`）。
PAYLOAD_DROP_CANDIDATES = '{"action":"answer","use_candidates":false,"clarification":""}'
#: 合法协议块（`action=answer` + 采用候选）—— 窄路径的**对照腿**。
PAYLOAD_KEEP_CANDIDATES = '{"action":"answer","use_candidates":true,"clarification":""}'


class _Boom(RuntimeError):
    """A18① 注入的异常（类名进断言，便于把「谁炸的」写进失败信息）。"""


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _fence(payload: str) -> str:
    """协议块（`<<<CLOSEOUT>>>` 围栏）——与 `test_agent_closeout_http_t1` 同形。"""
    return f"说明\n<<<CLOSEOUT>>>\n{payload}\n<<<END>>>\n"


def _done(events: list[dict]) -> dict:
    done = [event for event in events if event.get("type") == "done"]
    assert len(done) == 1, events
    return done[0]


def _terminal(events: list[dict]) -> list[dict]:
    """收尾帧（**恰好一条**是 I5 / guard 器官的硬约束）。"""
    return [
        event
        for event in events
        if event.get("type") in ("done", "error", "stopped")
    ]


def _answer_text(events: list[dict]) -> str:
    return "".join(event.get("text", "") for event in events if event.get("type") == "token")


def _steps(events: list[dict]) -> list[dict]:
    return [
        event["step"] for event in events if event.get("type") == "agent" and event.get("step")
    ]


def _capture_plan(monkeypatch, func_name: str, *, mutate=None) -> dict:
    """截住 ``chat_routes.<func_name>``，把**落库那一刻的 ``TurnPlan`` 快照**记下来。

    为什么用 spy 而不是事后从 SSE 反推：A23③ 要判的正是「交给落库函数的那个 plan 里
    `materials` 是什么」——prompt 拼装、引用判定都在它之后，反推只能得到间接证据。
    快照在**调用时刻**取（plan 是可变对象，事后读会被后续步骤污染）。

    ``mutate``（可选）**只服务变异自证**：在快照之前就地把 plan 改掉，等价于「把坏实现
    的后果注入到同一条路径上」——用来证明本文件的断言**真的会红**，而不必改 `app/**`。
    """
    real = getattr(chat_routes, func_name)
    sink: dict = {}

    async def _spy(db, plan, answer, **kwargs):  # noqa: ANN001
        if mutate is not None:
            mutate(plan)
        sink["snapshot"] = {
            "kind": plan.kind,
            "source_type": plan.source_type,
            "materials": [getattr(item, "content", "") for item in plan.materials],
            "materials_text": plan.materials_text,
            "all_refs": len(plan.all_refs),
            "materials_finalized": getattr(plan, "materials_finalized", None),
            "attempt_id": getattr(plan, "attempt_id", None),
            "needs_model": plan.needs_model,
        }
        return await real(db, plan, answer, **kwargs)

    monkeypatch.setattr(chat_routes, func_name, _spy)
    return sink


async def _seed_material(db, text: str, filename: str) -> list[Material]:
    """造一份**真**材料（真文档 + 真分块，避免归因落库撞外键）。"""
    document = await seed_document(db, [text], filename=filename)
    rows = await db.execute(
        select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ordinal)
    )
    return [
        Material(
            chunk_id=chunk.id,
            document_id=document.id,
            content=chunk.content,
            filename=filename,
            block_ordinal=chunk.ordinal,
        )
        for chunk in rows.scalars().all()
    ]


async def _install_candidates(db, monkeypatch) -> str:
    """把**前置检索**钉成「恰好命中一条候选」（A23 要候选在停止轮里活下来）。"""
    materials = await _seed_material(db, CANDIDATE, "候选.md")
    rendered = render_materials(number(materials))

    async def _fake_retrieve(session, text, filters=None, *, stats=None):  # noqa: ANN001
        return materials, rendered, None

    monkeypatch.setattr(chat_routes, "retrieve_materials", _fake_retrieve)
    return rendered


def _route_internal(monkeypatch) -> None:
    """路由分类器钉成「库内命中」（`wire_env` 默认钉的是 external）。"""
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )


def _faux_provider(*responses, tokens_per_second: float = 30.0):
    """限速的 Faux（停止点要落在一段**流式中**的决策里，不能瞬间跑完）。"""
    from cubeloop.providers import FauxProvider

    provider = FauxProvider(
        provider_id="faux",
        tokens_per_second=tokens_per_second,
        token_size_min=4,
        token_size_max=4,
    )
    provider.set_responses(list(responses))
    return provider


async def _stop_when_tool_runs(
    client, request_id: str, tool_started: asyncio.Event, *, settle: float = 0.6
) -> tuple[list[dict], list[dict]]:
    """**事件驱动**的停止：等「工具真的开始执行」再发停止（照 `test_stop_before_token_agent_4_6` 的写法）。

    为什么不能靠「读 SSE 帧到某个标记再停」：`tests/api` 的 `client` 是 **httpx ASGITransport**，
    它把整个响应体**攒完才交给客户端**（`httpx/_transports/asgi.py`：`body_parts` 只有在
    `await self.app(...)` 返回之后才组装成 `ASGIResponseStream`）⇒ 客户端**看不到流中的帧**，
    只能事后拿到完整正文。因此触发点必须由**服务端一侧**的事件给出（假的慢速检索就是那个事件）。
    ``settle`` 秒的余量用来保证「轮 1 的 `turn_end`（协议块解析点）已经被消费端处理」——
    它排在轮 2 的决策流之后、工具执行之前，余量给得比那两段都长一个量级。
    """
    stop_results: list[dict] = []

    async def _stop() -> None:
        await asyncio.wait_for(tool_started.wait(), timeout=30)
        await asyncio.sleep(settle)
        response = await client.post("/api/chat/stop", json={"request_id": request_id})
        stop_results.append(response.json())

    stopper = asyncio.create_task(_stop())
    async with client.stream(
        "POST",
        "/api/chat/stream",
        json={"message": MSG, "agent_mode": "react", "request_id": request_id},
    ) as response:
        assert response.status_code == 200
        raw = (await response.aread()).decode("utf-8")
    await stopper
    return parse_sse(raw), stop_results


# --------------------------------------------------------------------------
# A4 · Agent 自决澄清轮的三条副作用（D5）
# --------------------------------------------------------------------------


async def test_a4_agent_clarify_turn_side_effects(client, db, wire_env, monkeypatch):
    """A4：真澄清轮吃一次追问计数、记一次决策发送，零记忆抽取、零回答尝试。

    两条腿用**同一句短句**（`SHORT`）：Agent 腿（`react`，协议块 `action=clarify`）与
    `off` 腿（走既有澄清），`assistant/attempt` 条数必须**相同**（D5② 的「口径与 off 链一致」）。
    """
    assert SHORT.strip() and len(SHORT.strip()) < clarify.MIN_LENGTH

    # ⚠️ **记忆开关显式打开** + 把调度入口换成计数器：判的是**调用路径**，
    # 不让 `pin_memory_off` 或 `_enabled()` 的短路替被测代码作答（§5 第 7 条）。
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    extracts: list[dict] = []
    monkeypatch.setattr(memory_mod, "schedule_extract", lambda **kwargs: extracts.append(kwargs))

    plan_sink = _capture_plan(monkeypatch, "persist_turn")

    await db.rollback()
    usage_before = (await db.execute(select(func.count()).select_from(UsageEvent))).scalar_one()

    payload = json.dumps(
        {"action": "clarify", "use_candidates": False, "clarification": ASK},
        ensure_ascii=False,
    )
    wire = Wire(wire_sse_text(_fence(payload)))
    attach_wire(wire, monkeypatch)
    events = await run_stream(client, message=SHORT, agent_mode="react")
    done = _done(events)
    snapshot = dict(plan_sink["snapshot"])

    assert done["kind"] == "clarify", done
    assert ASK in _answer_text(events), _answer_text(events)
    # ⚠️ **前提自检**：这一轮必须真的**进了 Agent**（而不是退化成 off 链的澄清——那样
    # kind / rounds / usage / 记忆**全都一样**，本用例会变成一条「坏实现也绿」的纸判据）。
    # 三条一起看：① 唯一那次调用**带 tools[]**（= ReAct 决策，不是澄清文案那一腿）；
    # ② 轨迹的模式是 react；③ 排序回答那一格没被消费。
    assert (done.get("agent_trace") or {}).get("mode") == "react", done.get("agent_trace")
    assert wire.decision_bodies and wire.decision_bodies[0].get("tools"), wire.call_kinds()
    assert wire.call_kinds() == ["decision"], wire.call_kinds()
    assert wire.queue == []

    await db.rollback()
    from app.usage import recorder
    await recorder.drain_pending()
    usage_after = (await db.execute(select(func.count()).select_from(UsageEvent))).scalar_one()
    chat = await db.get(Chat, done["chat_id"])
    rounds = int(chat.clarify_rounds or 0)
    attempts = await event_log.list_events_by_type(db, done["chat_id"], "assistant/attempt")

    # off 链对照（同一条短句）
    off_wire = Wire(wire_sse_text(ASK))
    attach_wire(off_wire, monkeypatch)
    off_done = _done(await run_stream(client, message=SHORT, agent_mode="off"))
    assert off_done["kind"] == "clarify", off_done
    assert off_wire.call_kinds() == ["answer"], off_wire.call_kinds()
    assert off_wire.queue == []
    await db.rollback()
    off_attempts = await event_log.list_events_by_type(
        db, off_done["chat_id"], "assistant/attempt"
    )

    # ① kind 吃计数（真库读数）
    assert rounds == 1, f"澄清轮必须吃一次追问计数，实际 clarify_rounds={rounds}"
    # ② 决策实际发送一次，未发生回答请求，不得补造回答用量。
    assert usage_after == usage_before + 1, (
        f"实际决策发送必须恰好记录一次，不补造回答用量：{usage_before} → {usage_after}"
    )
    # ③ 零记忆抽取
    assert extracts == [], f"澄清轮不得抽记忆，实际被调度 {len(extracts)} 次"
    # ④ 尝试口径与 off 链一致，且两腿都是「不算一次尝试」
    assert len(attempts) == len(off_attempts), (len(attempts), len(off_attempts))
    assert len(attempts) == 0, [row.data for row in attempts]
    # 机制面（同一条判据的两侧）：预置正文 + 不铸尝试 id
    assert snapshot["needs_model"] is False, snapshot
    assert snapshot["attempt_id"] is None, snapshot


async def test_a4_mutation_proof_usage_and_memory_would_go_red(
    client, db, wire_env, monkeypatch
):
    """A4 的**变异自证**：把 D5 点名的那道守卫拿掉（`answer_succeeded` 恒真）⇒ 断言的鉴别力。

    只替 `answer_succeeded`，要求记忆调度被错误打开；真实发送仍只有决策一次。
    OPT5 的发送记账不依赖该守卫，不能凭这次变异虚构第二次回答发送。
    """
    monkeypatch.setattr(chat_routes, "answer_succeeded", lambda plan, answer: True)
    extracts: list[dict] = []
    monkeypatch.setattr(memory_mod, "schedule_extract", lambda **kwargs: extracts.append(kwargs))

    await db.rollback()
    usage_before = (await db.execute(select(func.count()).select_from(UsageEvent))).scalar_one()

    payload = json.dumps(
        {"action": "clarify", "use_candidates": False, "clarification": ASK},
        ensure_ascii=False,
    )
    wire = Wire(wire_sse_text(_fence(payload)))
    attach_wire(wire, monkeypatch)
    done = _done(await run_stream(client, message=SHORT, agent_mode="react"))
    assert done["kind"] == "clarify", done

    await db.rollback()
    from app.usage import recorder
    await recorder.drain_pending()
    usage_after = (await db.execute(select(func.count()).select_from(UsageEvent))).scalar_one()

    # OPT5 accounts actual sends; toggling closeout cannot fabricate a send.
    assert usage_after == usage_before + 1
    assert wire.call_kinds() == ["decision"]
    assert wire.queue == []
    # ⇒ A4 的「零记忆抽取」会红
    assert len(extracts) == 1, f"坏实现必须真的调度一次抽取，实际 {len(extracts)} 次"


# --------------------------------------------------------------------------
# A23 · 停轮不被污染
# --------------------------------------------------------------------------


async def _run_stop_leg(client, db, fake_mcp, monkeypatch, *, request_id: str, mutate=None) -> dict:
    """跑一次「带 closeout 的停止轮」（A23 的构造，两条用例共用）。

    构造（刻意让它**有鉴别力**）：

    · 前置候选**非空**（1 条）⇒ `plan.source_type == "internal_db"`、`kind == "normal"`；
    · 轮 1 的决策 = 协议块 `use_candidates=False`（**若被误当成收口，候选会被清空、
      `source_type` 翻成 `external`**）+ 一次 `fake_mcp` 工具调用（**快速**返回、MCP 结果恒无
      材料 ⇒ `turn.materials` 恒空，判据不受助手材料干扰）；这一轮的 `turn_end` 把协议块解析进
      `turn.closeout`（`cubeloop/agent/loop.py` 的 `turn_end` 排在工具执行**之后**）；
    · 轮 2 的决策再要一次 `knowledge_search`，而**假检索是慢的**（停在那儿，见
      `_stop_when_tool_runs`）⇒ 这一轮的 `turn_end` **永远不会到**，`turn.closeout` 停在轮 1 的值。

    ⚠️ 本构造**不使用** `wire_env`（它会把 `resolve_aux_model` 钉回真装配 ⇒ Agent 的**决策调用**
    走真 Provider）：照 `test_agent_stop_closeout._install` 的写法把**两个**模型入口都钉成
    同一个 Faux（`chat_routes.resolve_chat_model` + `resolve_aux_model`），零出网。
    """
    _route_internal(monkeypatch)
    await _install_candidates(db, monkeypatch)
    sink = _capture_plan(monkeypatch, "persist_stopped_turn", mutate=mutate)
    monkeypatch.setattr(settings, "WEB_ENABLE", False)

    from cubeloop.providers import faux_assistant_message, faux_text, faux_tool_call

    provider = _faux_provider(
        # 轮 1：协议块 + **快速**工具（材料恒空）
        faux_assistant_message(
            [
                faux_text(_fence(PAYLOAD_DROP_CANDIDATES)),
                faux_tool_call(FAKE_TOOL_CONTRACT, {"q": "探针"}),
            ]
        ),
        # 轮 2：还想再检索一次 —— 停止点就落在这个慢工具里
        faux_assistant_message(
            [
                faux_text("我再检索一次。" + "补" * 120),
                faux_tool_call("knowledge_search", {"query": "候选"}),
            ]
        ),
        tokens_per_second=30.0,
    )
    model = provider.model("faux-chat")
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: model)

    tool_started = asyncio.Event()
    state = {"entered": 0, "returned": 0}

    async def _slow_retrieve(session, question, scope, *, stats=None):  # noqa: ANN001
        state["entered"] += 1
        tool_started.set()
        await asyncio.sleep(5)
        state["returned"] += 1
        return [], "", None

    monkeypatch.setattr(knowledge_search, "_default_retrieve", lambda: _slow_retrieve)

    events, stop_results = await _stop_when_tool_runs(client, request_id, tool_started)

    kinds = [event["type"] for event in events]
    assert stop_results == [{"stopped": True}], (stop_results, kinds)
    assert kinds[-1] == "stopped" and "done" not in kinds and "error" not in kinds, kinds
    # 前提自检 ①：轮 1 的 decision 步（thought）真的下发过 ⇒ 协议块真的被解析过
    steps = _steps(events)
    assert any(step.get("kind") == "thought" for step in steps), steps
    assert all(step.get("title") != "结束决策无效" for step in steps), steps
    assert [step.get("tool") for step in steps if step.get("kind") == "tool"] == [
        FAKE_TOOL_CONTRACT
    ], steps
    # 前提自检 ②：停止点确实落在轮 2 的**慢工具途中**（工具进过、**从未返回**）
    # ⇒ 轮 2 的 `turn_end` 不可能发生 ⇒ `turn.closeout` 就是轮 1 那个 `use_candidates=False`。
    # ⚠️ 轮 2 的 `tool` 步**不会**出现在正文里：消费端是**先写事件再下发步骤**，而工具执行本体
    # 持有会话锁（`runner._build_execute` 的 `async with _session_lock(db_lock)`）⇒ 慢工具在飞的
    # 那几秒里消费端根本走不到 `_steps_for`。所以「工具进过」只能由**服务端一侧**的计数器证明。
    assert state == {"entered": 1, "returned": 0}, state

    return {
        "events": events,
        "snapshot": sink["snapshot"],
        "chat_id": events[-1]["chat_id"],
        "steps": steps,
    }


def _clear_the_plan_like_a_closeout(plan) -> None:  # noqa: ANN001
    """**变异自证专用**：把「收口落到停止轮」的后果就地注入（等价于 ②③ 的 bad case）。

    与 `chat_routes._apply_agent_closeout` 的 `use_candidates=False` 分支逐值等价
    （清三样材料 + 归 `direct` + `source_type` → `external`），另加「停止轮也把材料定案」这一半
    （`materials_finalized=True` ⇒ `turn/end` 会多出两键）。它**不改生产代码**，只在 spy 里
    动这一轮自己的 plan。
    """
    plan.materials = []
    plan.materials_text = ""
    plan.all_refs = []
    plan.kind = source_map.DIRECT_KIND
    plan.refs = []
    plan.source_type = "external"
    plan.materials_finalized = True


async def test_a23_stopped_turn_is_not_polluted_by_closeout(
    client, db, fake_mcp, monkeypatch
):
    """A23：带 closeout 的停止轮 —— 候选、`source_type`、`turn/end` 三样都不许被写坏。"""
    result = await _run_stop_leg(
        client, db, fake_mcp, monkeypatch, request_id=STOP_REQUEST_ID
    )
    snapshot = result["snapshot"]
    chat_id = result["chat_id"]

    # ③ 停止轮交给落库函数的那份 plan：候选**原样还在**（未被 _apply_agent_closeout 替换/清空）
    assert snapshot["materials"] == [CANDIDATE], snapshot
    assert CANDIDATE in snapshot["materials_text"], snapshot
    assert snapshot["kind"] == "normal", snapshot
    assert snapshot["source_type"] == "internal_db", snapshot
    assert snapshot["all_refs"] == 1, snapshot
    assert snapshot["materials_finalized"] is False, snapshot

    await db.rollback()
    rows = list(
        (
            await db.scalars(
                select(Message).where(Message.chat_id == chat_id).order_by(Message.id)
            )
        ).all()
    )
    # ① 落库 user 行的 source_type（零 token 的停止轮不落 assistant 行）
    assert [row.role for row in rows] == ["user"], [row.role for row in rows]
    assert rows[0].source_type == "internal_db", rows[0].source_type

    # ② turn/end 的两键**缺席**（`materials_finalized` 为假 ⇒ `final_material_state` 给 `{}`）
    ends = await event_log.list_events_by_type(db, chat_id, "turn/end")
    assert len(ends) == 1, [row.data for row in ends]
    assert ends[0].data.get("kind") == "stopped", ends[0].data
    assert "final_material_count" not in ends[0].data, ends[0].data
    assert "final_empty_reason" not in ends[0].data, ends[0].data


async def test_a23_mutation_proof_the_three_assertions_can_go_red(
    client, db, fake_mcp, monkeypatch
):
    """A23 的**变异自证**：把「收口落到停止轮」的后果注入同一条路径 ⇒ 三样断言都必须能红。

    判据（自证的是**鉴别力**，不是行为）：污染后的 `materials` / `source_type` /
    `materials_finalized` 与 A23 的期望值**逐项不同**，且 `turn/end` 真的多出那两键
    ⇒ 上面那条用例的 ①②③ 三处断言都会由绿转红（不是「永远绿」的纸判据）。
    """
    result = await _run_stop_leg(
        client,
        db,
        fake_mcp,
        monkeypatch,
        request_id=f"{STOP_REQUEST_ID}-mutated",
        mutate=_clear_the_plan_like_a_closeout,
    )
    snapshot = result["snapshot"]

    assert snapshot["materials"] == [] != [CANDIDATE], snapshot  # ③ 会红
    assert snapshot["materials_text"] == "", snapshot
    assert snapshot["all_refs"] == 0, snapshot
    assert snapshot["kind"] == "direct", snapshot
    assert snapshot["source_type"] == "external", snapshot  # ① 会红
    assert snapshot["materials_finalized"] is True, snapshot  # ② 的前提会红

    await db.rollback()
    rows = list(
        (
            await db.scalars(
                select(Message).where(Message.chat_id == result["chat_id"]).order_by(Message.id)
            )
        ).all()
    )
    assert rows[0].source_type == "external", rows[0].source_type
    ends = await event_log.list_events_by_type(db, result["chat_id"], "turn/end")
    assert "final_material_count" in ends[0].data, ends[0].data  # ② 会红
    assert "final_empty_reason" in ends[0].data, ends[0].data


# --------------------------------------------------------------------------
# A18① · 决策 / 收口抛错不炸穿，恰好一次收尾
# --------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["decision_step", "closeout_step"])
async def test_a18_injected_step_failure_does_not_blow_through(
    client, db, wire_env, monkeypatch, where
):
    """A18①：决策步（`parse_closeout_message`）或收口步（`_apply_agent_closeout`）抛错 ⇒
    不炸穿（HTTP 200、流走完）且**恰好一次** `done`（或 `error`）收尾。

    两条腿的收尾类型**不同且是实测的**：决策步抛错被 `stream_agent_turn` 的
    `except Exception` 隔离成一条「引擎异常」note ⇒ 本轮照常作答（`done`）；
    收口步抛错发生在**收尾回答之前**、且不在那个 except 的覆盖范围内 ⇒ `error`。
    两条腿都断言「提问没丢」（`persist_user_turn` 兜住）。

    ⚠️ **前提自检**（否则这是一条纸判据：注入点没被调用时，一轮正常对话同样「恰好一次 done」）：
    计数 `hits` 必须 ≥1；决策腿还必须出现那条「引擎异常」note（异常确实被就地隔离成了步骤）。
    """
    wire = Wire(wire_sse_text(_fence(PAYLOAD_KEEP_CANDIDATES)), wire_sse_text("照常作答。"))
    attach_wire(wire, monkeypatch)

    hits: list[str] = []

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        hits.append(where)
        raise _Boom(f"{where} 注入的异常")

    if where == "decision_step":
        monkeypatch.setattr(runner_mod, "parse_closeout_message", _boom)
    else:
        monkeypatch.setattr(chat_routes, "_apply_agent_closeout", _boom)

    events = await run_stream(client, message=MSG, agent_mode="react")
    kinds = [event["type"] for event in events]
    terminals = _terminal(events)

    assert hits == [where], f"注入点必须真的被调用（否则这条判据是纸的），实际 {hits}"
    assert len(terminals) == 1, kinds
    assert kinds[0] == "meta", kinds
    assert kinds[-1] == terminals[0]["type"], kinds
    assert "meta" in kinds and kinds.count("meta") == 1, kinds

    if where == "decision_step":
        assert terminals[0]["type"] == "done", kinds
        # 隔离的可见落点：异常被 `stream_agent_turn` 就地转成一条 note，本轮照常作答
        assert any(step.get("title") == "引擎异常" for step in _steps(events)), _steps(events)
        assert wire.call_kinds() == ["decision", "answer"], wire.call_kinds()
        assert wire.queue == []
    else:
        assert terminals[0]["type"] == "error", kinds
        assert wire.call_kinds() == ["decision"], wire.call_kinds()
        # 收口抛错发生在收尾回答**之前** ⇒ 排给回答的那一格不会被消费
        assert wire.queue, wire.call_kinds()

    chat_id = events[0]["chat_id"]
    await db.rollback()
    rows = list(
        (
            await db.scalars(
                select(Message).where(Message.chat_id == chat_id).order_by(Message.id)
            )
        ).all()
    )
    assert rows and rows[0].role == "user" and rows[0].content == MSG, [row.role for row in rows]


# --------------------------------------------------------------------------
# 窄路径定性（只报不改）：「前置候选为空 + 助手检到材料 + 协议块坏」
# --------------------------------------------------------------------------


async def _drive_narrow_path(client, db, fake_mcp, monkeypatch, *, fence: str):  # noqa: ARG001
    """跑一轮：空候选 → `fake_mcp` 工具 → `knowledge_search` 检到材料 → **给定的协议块** → 作答。

    返回 `(SSE 事件, persist_turn 的 plan 快照, Wire, 材料正文)`。
    """
    _route_internal(monkeypatch)

    # 前置候选 = **空**（窄路径的前提之一）
    async def _no_candidates(session, text, filters=None, *, stats=None):  # noqa: ANN001
        return [], "", None

    monkeypatch.setattr(chat_routes, "retrieve_materials", _no_candidates)

    # 助手侧材料：只能来自**带 materials 的工具结果**（MCP 恒无材料）
    materials = await _seed_material(db, AGENT_MATERIAL, "助手材料.md")
    rendered = render_materials(number(materials))

    async def _agent_retrieve(session, question, scope, *, stats=None):  # noqa: ANN001
        return materials, rendered, None

    monkeypatch.setattr(knowledge_search, "_default_retrieve", lambda: _agent_retrieve)

    sink = _capture_plan(monkeypatch, "persist_turn")
    wire = Wire(
        wire_sse_tool_call(FAKE_TOOL_CONTRACT, {"q": "探针"}, call_id="call_mcp"),
        wire_sse_tool_call("knowledge_search", {"query": "助手材料"}, call_id="call_ks"),
        wire_sse_text(f"说明\n{fence}"),
        wire_sse_text("[1] 照助手材料作答。"),
    )
    attach_wire(wire, monkeypatch)

    events = await run_stream(client, message=MSG, agent_mode="react")
    return events, sink, wire, materials


def _observation_texts(events: list[dict]) -> str:
    return "\n".join(
        str(step.get("detail") or "") for step in _steps(events) if step.get("kind") == "observation"
    )


async def test_narrow_invalid_closeout_keeps_agent_materials(
    client, db, wire_env, fake_mcp, monkeypatch
):
    """窄路径（**回归修复 的端到端回归钉**）：协议块坏 ⇒ 助手工具检到的材料**不该被丢掉**。

    口径（D-10 的「三条坏路径同向回退」+ §2.2 情形 2）：回退时**优先采候选**；而本轮
    没有候选可采，助手检到的材料就是唯一资料 ⇒ 必须接住它。
    改前实测：`plan.materials=[]`、`kind=direct`、**回答请求体里资料区整段消失**（红点落在
    用户可见面上）。修后本用例绿；对照腿 `test_narrow_control_valid_closeout_keeps_agent_materials`
    证明「红」不是夹具里压根没材料。
    """
    events, sink, wire, materials = await _drive_narrow_path(
        client, db, fake_mcp, monkeypatch, fence=_fence("{bad")
    )

    # 前提自检（三条，缺一条这个用例就没测到东西）：
    # ① 助手确实调了两次工具；② 观察帧里**真的有**那段材料正文；③ 协议块确实是坏的（有可见 note）
    steps = _steps(events)
    assert [step.get("kind") for step in steps].count("tool") == 2, steps
    assert AGENT_MATERIAL in _observation_texts(events), _observation_texts(events)
    assert len(materials) == 1 and materials[0].content == AGENT_MATERIAL
    assert any(step.get("title") == "结束决策无效" for step in steps), steps

    snapshot = sink["snapshot"]
    assert wire.answer_bodies, "本轮必须有收尾回答调用"
    # ⚠️ 先断 **wire 级**（D21 的硬口径：判据必须看最终回答请求体），再断内部字段——
    # 这样「材料被丢」的红点直接落在**用户可见面**上（资料区正文里没有那段材料）。
    assert AGENT_MATERIAL in wire.last_user_text(wire.answer_bodies[-1]), (
        "助手材料没有进最终回答请求体（资料区整段消失）："
        f"plan.materials={snapshot['materials']}，kind={snapshot['kind']}，"
        f"source_type={snapshot['source_type']}，"
        f"请求体尾部={wire.last_user_text(wire.answer_bodies[-1])[-200:]!r}"
    )
    assert snapshot["materials"] == [AGENT_MATERIAL], (
        f"协议块坏时助手材料被整个丢掉了：plan.materials={snapshot['materials']}，"
        f"kind={snapshot['kind']}，source_type={snapshot['source_type']}"
    )
    assert wire.queue == []


async def test_narrow_control_valid_closeout_keeps_agent_materials(
    client, db, wire_env, fake_mcp, monkeypatch
):
    """窄路径的**对照腿**：同一构造、协议块**合法**（`use_candidates=true`）⇒ 材料必须保留。

    它证明「红」不是因为夹具里压根没有材料（否则这条也会红），而是**invalid 分支**独有的后果。
    """
    events, sink, wire, _materials = await _drive_narrow_path(
        client, db, fake_mcp, monkeypatch, fence=_fence(PAYLOAD_KEEP_CANDIDATES)
    )

    steps = _steps(events)
    assert [step.get("kind") for step in steps].count("tool") == 2, steps
    assert AGENT_MATERIAL in _observation_texts(events), _observation_texts(events)
    assert all(step.get("title") != "结束决策无效" for step in steps), steps

    assert sink["snapshot"]["materials"] == [AGENT_MATERIAL], sink["snapshot"]
    assert AGENT_MATERIAL in wire.last_user_text(wire.answer_bodies[-1])
    assert wire.queue == []
