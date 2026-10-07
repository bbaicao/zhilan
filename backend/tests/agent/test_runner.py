"""runner 单测（阶段 4 · 条目 4.1）：决策循环 / 材料编号 / 轮次上限 / 降级不炸穿。

**全部不连真模型**（统筹 §3.4）：用 CubeLoop 的 FauxProvider 驱动真解码路径，
工具用注入的假检索实现。联网工具一律不进清单（本文件不需要真出网）。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.agent import runner
from app.agent.registry import ToolRegistry, ToolSpec
from app.agent.runner import AgentTurn, ToolCollector, stream_agent_turn
from app.agent.tools import knowledge_search
from app.agent.types import ToolObservation
from app.retrieval.types import FUSE_LIMIT, Material
from pydantic import BaseModel


# --------------------------------------------------------------------------
# 夹具与助手
# --------------------------------------------------------------------------


def faux_model(responses: list) -> object:
    """FauxProvider 驱动的真链路模型；响应队列**按调用顺序消费**。"""
    from cubeloop.providers import FauxProvider

    provider = FauxProvider(provider_id="faux")
    provider.set_responses(list(responses))
    return provider.model("faux-chat")


def tool_call(name: str, args: dict):
    from cubeloop.providers import faux_assistant_message, faux_tool_call

    return faux_assistant_message([faux_tool_call(name, args)])


def says(text: str):
    from cubeloop.providers import faux_assistant_message

    return faux_assistant_message(text)


def faux_message(text: str, *, stop_reason: str = "stop"):
    """一条助手消息（P&E 的计划调用用的是 ``generate``，返回的就是这个形状）。"""
    from cubeloop.providers import faux_assistant_message

    return faux_assistant_message(text, stop_reason=stop_reason)


class RecordingModel:
    """能**记录调用**的假 bound model（P&E 的验收要看模型收到什么）。

    只实现 P&E 用到的 ``generate``：记录系统提示 / 提示词 / ``max_output_tokens`` /
    ``reasoning``，返回预设文本。响应队列**按调用顺序消费**，队列排空后返回空文本
    （而不是复用最后一条）——否则「步间是否又问了模型」这类计数断言会被掩盖。
    """

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def generate(
        self,
        messages,
        *,
        system_prompt="",
        max_output_tokens=None,
        reasoning=None,
        **_kwargs,
    ):  # noqa: ANN001
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "max_output_tokens": max_output_tokens,
                "reasoning": reasoning,
                "messages": list(messages),
            }
        )
        text = self.replies.pop(0) if self.replies else ""
        return faux_message(text)

    @property
    def count(self) -> int:
        return len(self.calls)

    @property
    def last_prompt(self) -> str:
        message = self.calls[-1]["messages"][-1]
        return "".join(
            getattr(block, "text", "")
            for block in (getattr(message, "content", None) or [])
            if getattr(block, "text", None)
        )


def plan_json(*steps: dict) -> str:
    """计划器该回的 JSON 文本（形状与 ``PLAN_SYSTEM`` 的要求一致）。"""
    import json as _json

    return _json.dumps({"steps": list(steps)}, ensure_ascii=False)


def material(index: int, *, filename: str = "", content: str = "正文") -> Material:
    return Material(
        chunk_id=index,
        document_id=100 + index,
        content=f"{content}{index}",
        filename=filename or f"doc{index}.md",
        block_ordinal=index,
    )


class CollectingRetrieval:
    """假检索实现：按调用次序返回预设材料，并记录收到的 (session, question, filters)。"""

    def __init__(self, batches: list[list[Material]]) -> None:
        self.batches = list(batches)
        self.calls: list[tuple] = []

    async def __call__(self, session, question, scope, *, stats=None):
        self.calls.append((session, question, scope))
        items = self.batches.pop(0) if self.batches else []
        return items, "", None


def registry_with(*specs: ToolSpec) -> ToolRegistry:
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return registry


async def collect(turn: AgentTurn, **kwargs) -> list[dict]:
    frames: list[dict] = []
    async for kind, payload in stream_agent_turn(turn, **kwargs):
        assert kind == "agent"
        frames.append(payload)
    return frames


def kinds_of(frames: list[dict]) -> list[str]:
    return [f["kind"] for f in frames]


def titles_of(frames: list[dict]) -> list[str]:
    return [f["title"] for f in frames]


# --------------------------------------------------------------------------
# 轮次上限与配置
# --------------------------------------------------------------------------


def test_agent_max_rounds_reads_setting_and_clamps(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENT_MAX_ROUNDS", 3)
    assert runner.agent_max_rounds() == 3
    monkeypatch.setattr(settings, "AGENT_MAX_ROUNDS", 12)
    assert runner.agent_max_rounds() == 12
    # 非法（越界 / 非 int）一律回落 6——老基线同口径
    for bad in (0, -1, 13, 99, "abc", None):
        monkeypatch.setattr(settings, "AGENT_MAX_ROUNDS", bad)
        assert runner.agent_max_rounds() == runner.DEFAULT_MAX_ROUNDS, bad


@pytest.mark.asyncio
async def test_cap_decision_payload_pins_max_tokens():
    """条目 4.1「决策 max_tokens=600」：请求体的 max_tokens 被钉成 600，且不改入参。"""
    original = {"model": "m", "max_tokens": 8192, "messages": []}
    capped = await runner._cap_decision_payload(original, None)
    assert capped["max_tokens"] == runner.DECISION_MAX_OUTPUT_TOKENS == 600
    assert capped["model"] == "m"
    assert original["max_tokens"] == 8192  # 入参未被就地改写


@pytest.mark.asyncio
async def test_agent_is_built_with_decision_discipline(monkeypatch):
    """决策纪律的装配事实：``on_payload`` 钉 max_tokens、``reasoning.mode == "off"``。"""
    import cubeloop

    captured: dict = {}
    real_agent = cubeloop.Agent

    class RecordingAgent(real_agent):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(cubeloop, "Agent", RecordingAgent)

    retrieval = CollectingRetrieval([[material(1)]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    turn = AgentTurn(mode="react")
    await collect(
        turn,
        question="问题",
        model=faux_model([tool_call("knowledge_search", {"query": "q"}), says("够了")]),
        registry=registry_with(spec),
    )

    assert captured["on_payload"] is runner._cap_decision_payload
    assert captured["reasoning"].mode == "off"
    assert captured["middleware"] == []  # 器官统一写 middleware=[]（坑清单）


# --------------------------------------------------------------------------
# 材料累积：去重 / 上限 / 快照编号（I3）
# --------------------------------------------------------------------------


def test_collector_assigns_snapshot_ordinals_and_dedupes():
    collector = ToolCollector()
    first = ToolObservation(text="", materials=[material(1), material(2)])
    collector.absorb("c1", first)
    assert [m.ordinal for m in collector.materials] == [1, 2]

    # 第二次：1 是重复（同一 chunk_key），3 是新的 ⇒ 新项拿 3 号，不是「本次的第 2 条」
    second = ToolObservation(text="", materials=[material(1), material(3)])
    text = collector.absorb("c2", second)
    assert [m.ordinal for m in collector.materials] == [1, 2, 3]
    assert text.startswith("[1] doc1.md")
    assert "[3] doc3.md" in text
    assert "[2] doc2.md" not in text  # 只渲染**本次**产出的材料


def test_collector_caps_at_fuse_limit():
    collector = ToolCollector()
    collector.absorb(
        "c1", ToolObservation(text="", materials=[material(i) for i in range(1, FUSE_LIMIT + 5)])
    )
    assert len(collector.materials) == FUSE_LIMIT
    assert [m.ordinal for m in collector.materials] == list(range(1, FUSE_LIMIT + 1))


def test_collector_falls_back_to_tool_text_when_no_materials():
    collector = ToolCollector()
    text = collector.absorb("c1", ToolObservation(text="MCP 返回的一段话"))
    assert text == "MCP 返回的一段话"
    assert collector.materials == []


def test_collector_keeps_warnings_unique_and_ordered():
    collector = ToolCollector()
    collector.absorb("c1", ToolObservation(text="", warnings=["甲", "", "乙"]))
    collector.absorb("c2", ToolObservation(text="", warnings=["乙", "丙"]))
    assert collector.warnings == ["甲", "乙", "丙"]


def test_render_observation_uses_snapshot_ordinals():
    items = [material(1), material(2)]
    for index, item in enumerate(items, start=1):
        item.ordinal = index + 5  # 假装它们在整轮快照里排第 6、7 位
    text = runner.render_observation(items)
    assert text.startswith("[6] doc1.md")
    assert "[7] doc2.md" in text


# --------------------------------------------------------------------------
# 对抗性核验抓到的三条（F1/F2/F3）：编号同源与事件载荷诚实性
# --------------------------------------------------------------------------


def test_full_snapshot_never_falls_back_to_local_numbering():
    """F1 回归钉：快照打满后**不得**回落到工具自渲染文本（那份从 1 重新编号）。

    实测触发：3 步以上、每步 5 条新材料的正常计划。回落会让 ``[1]`` 指向快照里的第 1 条材料，
    而原文其实是另一批（I3 破）。现在改为明说「未纳入引用快照」+ 记一条 warning。
    """
    collector = ToolCollector()
    first = collector.absorb(
        "c1", ToolObservation(text="", materials=[material(i) for i in range(1, FUSE_LIMIT + 1)])
    )
    assert len(collector.materials) == FUSE_LIMIT  # 快照已满

    later = collector.absorb(
        "c2", ToolObservation(text="[1] 工具自己的编号\n正文", materials=[material(99)])
    )
    assert later == runner.SNAPSHOT_FULL_TEXT
    assert "[1]" not in later  # 关键：不出现任何**看起来可引用**的编号
    assert [m.ordinal for m in collector.materials] == list(range(1, FUSE_LIMIT + 1))  # 快照未变
    assert any("上限" in w for w in collector.warnings)
    assert first.startswith("[1] doc1.md")  # 打满之前的行为逐字不变


def test_empty_observation_still_uses_tool_text():
    """F1 的修复**不得**波及「真的没有材料」那条分支（失败原因 / MCP 文本 / 空命中）。"""
    collector = ToolCollector()
    assert collector.absorb("c1", ToolObservation(text="MCP 返回的一段话")) == "MCP 返回的一段话"
    assert collector.absorb("c2", ToolObservation(text="")) == runner.NO_HIT_TEXT
    assert collector.warnings == []  # 没有材料 ≠ 被上限丢掉


# --------------------------------------------------------------------------
# 一轮循环
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_runs_tool_then_finishes_with_numbered_observation():
    retrieval = CollectingRetrieval([[material(1, filename="蓝鲸.md")]])
    spec = knowledge_search.build_spec(db=object(), filters={"kb_ids": [1]}, retrieve=retrieval)
    model = faux_model([tool_call("knowledge_search", {"query": "蓝鲸"}), says("可以作答了")])

    turn = AgentTurn(mode="react")
    frames = await collect(
        turn,
        question="蓝鲸是什么",
        history=[{"role": "user", "content": "上一问"}, {"role": "assistant", "content": "上一答"}],
        model=model,
        registry=registry_with(spec),
    )

    kinds = [f["kind"] for f in frames]
    assert kinds == ["tool", "observation", "thought"]
    assert frames[0]["title"] == "检索知识库：蓝鲸"
    assert frames[0]["args"] == {"query": "蓝鲸"}
    assert frames[0]["tool_call_id"]  # 与 observation 同一个 id（I4 的对账键）
    assert frames[1]["tool_call_id"] == frames[0]["tool_call_id"]
    assert frames[1]["ok"] is True
    assert "[1] 蓝鲸.md" in frames[1]["detail"]
    assert frames[2]["detail"] == "可以作答了"

    assert turn.rounds == 2
    assert turn.aborted is False
    assert turn.note == ""
    assert [m.filename for m in turn.materials] == ["蓝鲸.md"]
    assert [m.ordinal for m in turn.materials] == [1]
    # 范围下推（阶段 3 接线四条之 1）
    assert retrieval.calls[0][2] == {"kb_ids": [1]}
    assert set(turn.trace()) == {"mode", "steps", "events_incomplete"}


@pytest.mark.asyncio
async def test_second_search_observation_keeps_snapshot_ordinals():
    """I3 的回归钉：第二次检索的观察**不从 1 重新编号**。"""
    retrieval = CollectingRetrieval([[material(1)], [material(2)]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = faux_model(
        [
            tool_call("knowledge_search", {"query": "第一问"}),
            tool_call("knowledge_search", {"query": "第二问"}),
            says("够了"),
        ]
    )
    turn = AgentTurn(mode="react")
    frames = await collect(
        turn, question="问题", model=model, registry=registry_with(spec), max_rounds=5
    )
    observations = [f for f in frames if f["kind"] == "observation"]
    assert "[1] doc1.md" in observations[0]["detail"]
    assert "[2] doc2.md" in observations[1]["detail"]
    assert [m.ordinal for m in turn.materials] == [1, 2]


@pytest.mark.asyncio
async def test_turn_stops_at_max_rounds_with_note():
    """轮次上限：模型一直要工具时，跑到上限就停，并留一条可读 note。"""
    retrieval = CollectingRetrieval([[material(i)] for i in range(1, 6)])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = faux_model([tool_call("knowledge_search", {"query": f"q{i}"}) for i in range(1, 8)])

    turn = AgentTurn(mode="react")
    frames = await collect(
        turn, question="问题", model=model, registry=registry_with(spec), max_rounds=3
    )
    assert turn.rounds == 3
    notes = [f for f in frames if f["kind"] == "note"]
    assert [n["title"] for n in notes] == ["已达上限"]
    assert "3" in notes[0]["detail"]
    assert len(turn.materials) == 3


@pytest.mark.asyncio
async def test_natural_finish_at_round_limit_is_not_misreported():
    """``回归修复``：模型自然收尾恰好落在轮次上限 ⇒ 不补「已达上限」note。

    老判据 ``turn.rounds >= limit`` 分不清「被上限强制停」与「模型自主收手」：
    第 N 轮（N==limit）模型不再调工具、直接出收尾文本时循环本来就要结束，
    老代码仍把它报成「配额强制截断」。
    """
    retrieval = CollectingRetrieval([[material(1)]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    # 第 1 轮调工具（轮次 1 < 2 ⇒ 循环继续），第 2 轮（轮次 2 == 上限）不调工具自然收尾
    model = faux_model([tool_call("knowledge_search", {"query": "q"}), says("直接作答")])
    turn = AgentTurn(mode="react")
    frames = await collect(
        turn, question="问题", model=model, registry=registry_with(spec), max_rounds=2
    )
    assert turn.rounds == 2  # 自然收尾那轮真的跑到了上限处（不是提前退出）
    assert "已达上限" not in titles_of(frames)
    assert turn.note == ""
    assert turn.rounds_capped is False


@pytest.mark.asyncio
async def test_forced_stop_at_round_limit_still_notes():
    """对照腿（防过修）：真的被上限**强制**停（末轮有工具执行）⇒ note 照旧。

    ``rounds_capped`` 的判别式是「本轮真的跑过工具」（``tool_results`` 非空）——
    无工具轮循环本来就自然结束，只有前者才是「被上限强制停」。
    """
    retrieval = CollectingRetrieval([[material(i)] for i in range(1, 5)])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = faux_model(
        [
            tool_call("knowledge_search", {"query": "q1"}),
            tool_call("knowledge_search", {"query": "q2"}),
        ]
    )
    turn = AgentTurn(mode="react")
    frames = await collect(
        turn, question="问题", model=model, registry=registry_with(spec), max_rounds=2
    )
    notes = [f for f in frames if f["kind"] == "note"]
    assert [n["title"] for n in notes] == ["已达上限"]
    assert len(turn.materials) == 2  # 两轮工具都真跑了，然后被上限截停
    assert turn.rounds_capped is True


@pytest.mark.asyncio
async def test_drain_cancels_pending_getter_on_abort():
    """``回归修复``：_drain 被中止时，挂着的 ``queue.get()`` 任务必须被收掉。

    场景对齐真实路径：用户停止 / 客户端断开 / 任务取消 ⇒ ``CancelledError`` 在
    ``await asyncio.wait`` 处穿进 _drain——``aclose()`` 只关生成器本体，getter 是它内部
    另起的任务，修前原样遗留 pending（每轮中止漏一个，关机刷「Task was destroyed
    but it is pending」）；修后由 finally 统一 cancel。
    """
    import contextlib

    queue: asyncio.Queue = asyncio.Queue()

    async def _never_completes() -> None:
        await asyncio.sleep(60)

    prompt_task = asyncio.ensure_future(_never_completes())
    drain_gen = runner._drain(queue, prompt_task)

    consumer = asyncio.ensure_future(drain_gen.__anext__())
    await asyncio.sleep(0)
    await asyncio.sleep(0)  # 让 _drain 挂进 await asyncio.wait（getter pending）
    consumer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await consumer
    await asyncio.sleep(0)
    await asyncio.sleep(0)  # 让取消落地

    leaked = [
        t
        for t in asyncio.all_tasks()
        if not t.done() and t is not asyncio.current_task() and t is not prompt_task
    ]
    assert leaked == [], f"getter 泄漏：{leaked}"
    prompt_task.cancel()


@pytest.mark.asyncio
async def test_tool_db_scope_body_exception_propagates_verbatim(monkeypatch):
    """``回归修复``：逃出 ``async with`` 体的异常必须**原样**穿出，不得被换成 RuntimeError。

    旧形状（``except`` 包住 ``yield``）：体里异常经 ``__aexit__`` 的 ``athrow`` 注进
    ``_tool_db_scope`` 后命中 except 并**再次 yield** ⇒ contextlib 对「athrow 后未停」
    一律 ``RuntimeError``——原始异常被丢弃、日志还误报「独立工具会话不可用」。
    用 ``build_disclosure`` 构造失败做注入点（它位于 ``async with`` 体内、内部防线之外）。
    """
    import app.agent.disclosure as disclosure_mod

    def _boom(agent_tools):  # noqa: ANN001 —— build_disclosure 是同步函数，替身同形
        raise ValueError("装配炸了-x124")

    monkeypatch.setattr(disclosure_mod, "build_disclosure", _boom)
    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[]]))
    turn = AgentTurn(mode="react")
    with pytest.raises(ValueError, match="装配炸了-x124"):
        async for _frame in stream_agent_turn(
            turn, question="问题", model=faux_model([says("x")]), registry=registry_with(spec)
        ):
            pass


@pytest.mark.asyncio
async def test_tool_db_scope_falls_back_when_session_creation_fails(monkeypatch):
    """对照腿（防过修）：回落路径仍通——建独立会话失败 ⇒ 回落请求会话，本轮照常跑完。"""
    import app.db.session as session_mod

    def _boom(_bound=None):  # noqa: ANN001
        raise RuntimeError("引擎拿不到-x124")

    monkeypatch.setattr(session_mod, "new_independent_session", _boom)
    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[]]))
    turn = AgentTurn(mode="react")
    frames = await collect(
        turn, question="问题", model=faux_model([says("x")]), registry=registry_with(spec)
    )
    assert frames  # 本轮照常产出（回落语义 = 优化绝不降级功能）


@pytest.mark.asyncio
async def test_validation_failure_tool_call_leaves_reason_in_db(db):
    """``回归修复``（库面）：CubeLoop 层**参数校验失败** ⇒ tool/result 带原因 + 审计有行。

    ``_ImmediateOutcome`` 不调 handler ⇒ 修前 collector 零观察零审计：
    ``tool/result`` 落 ``ok=False, error="", content=""`` 的无因失败行、``agent_steps`` 零行，
    模型能看到上游英文校错文本而库里查无此因。修后照 P&E ``_run_plan_step`` 口径补账。
    """
    from app.models import AgentRun, AgentStep, Chat, SessionEvent

    chat = Chat(title="125 载体")
    db.add(chat)
    await db.flush()

    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[material(1)]]))
    # limit=0 违反 KnowledgeSearchArgs 的 ge=1 ⇒ pydantic 校验失败（CubeLoop 立即定案）
    model = faux_model(
        [tool_call("knowledge_search", {"query": "q", "limit": 0}), says("够了")]
    )
    turn = AgentTurn(mode="react")
    await collect(
        turn,
        question="问题",
        model=model,
        registry=registry_with(spec),
        db=db,
        chat_id=int(chat.id),
        turn_no=1,
    )

    events = list(
        (
            await db.execute(
                select(SessionEvent)
                .where(
                    SessionEvent.chat_id == int(chat.id),
                    SessionEvent.type == "tool/result",
                )
                .order_by(SessionEvent.seq)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    payload = events[0].data or {}
    assert payload["ok"] is False
    assert "参数校验失败" in str(payload.get("content", ""))
    assert payload.get("error", ""), "error 面也不能是无因空串"

    run = (
        await db.execute(
            select(AgentRun).where(AgentRun.chat_id == int(chat.id)).order_by(AgentRun.id.desc())
        )
    ).scalars().first()
    steps = list(
        (
            await db.execute(
                select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.step_no)
            )
        )
        .scalars()
        .all()
    )
    assert len(steps) == 1, "校验失败也必须落一行审计（修前为 0 行）"
    assert steps[0].meta["ok"] is False
    assert "参数校验失败" in steps[0].content
    # 参数摘要与 tool/call 同源（发起时的 args，经 _args_digest；标量按既有契约渲成字符串）
    assert steps[0].meta["args"] == {"query": "q", "limit": "0"}


@pytest.mark.asyncio
async def test_unknown_tool_call_leaves_reason_in_db(db):
    """``回归修复``（库面 · 未知工具腿）：点名不存在的工具同样带原因 + 审计有行。"""
    from app.models import AgentRun, AgentStep, Chat, SessionEvent

    chat = Chat(title="125 载体B")
    db.add(chat)
    await db.flush()

    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[]]))
    model = faux_model([tool_call("no_such_tool", {"x": 1}), says("换路")])
    turn = AgentTurn(mode="react")
    await collect(
        turn,
        question="问题",
        model=model,
        registry=registry_with(spec),
        db=db,
        chat_id=int(chat.id),
        turn_no=1,
    )

    events = list(
        (
            await db.execute(
                select(SessionEvent)
                .where(
                    SessionEvent.chat_id == int(chat.id),
                    SessionEvent.type == "tool/result",
                )
                .order_by(SessionEvent.seq)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    payload = events[0].data or {}
    assert payload["ok"] is False
    content = str(payload.get("content", ""))
    assert "工具不可用" in content
    assert "not found" in content  # 上游真因随文本保留

    run = (
        await db.execute(
            select(AgentRun).where(AgentRun.chat_id == int(chat.id)).order_by(AgentRun.id.desc())
        )
    ).scalars().first()
    steps = list(
        (
            await db.execute(
                select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.step_no)
            )
        )
        .scalars()
        .all()
    )
    assert len(steps) == 1
    assert "工具不可用" in steps[0].content


@pytest.mark.asyncio
async def test_tool_failure_becomes_observation_and_loop_continues():
    async def boom(session, question, scope, *, stats=None):
        raise RuntimeError("向量库炸了")

    spec = knowledge_search.build_spec(db=object(), retrieve=boom)
    model = faux_model([tool_call("knowledge_search", {"query": "q"}), says("换路")])
    turn = AgentTurn(mode="react")
    frames = await collect(turn, question="问题", model=model, registry=registry_with(spec))

    observation = [f for f in frames if f["kind"] == "observation"][0]
    assert observation["ok"] is False
    # 回归修复：帧里的观察正文同样只带固定形状，不带异常原文
    assert "向量库炸了" not in observation["detail"]
    assert "知识库检索 执行失败（RuntimeError）。" in observation["detail"]
    assert frames[-1]["kind"] == "thought"  # 失败**不终止**循环，模型继续自纠
    assert turn.materials == []


@pytest.mark.asyncio
async def test_unknown_tool_requested_by_model_is_reported_not_raised():
    """模型点名一个不存在的工具：CubeLoop 自己转错误结果，本轮不炸穿。"""
    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[]]))
    model = faux_model([tool_call("no_such_tool", {}), says("好")])
    turn = AgentTurn(mode="react")
    frames = await collect(turn, question="问题", model=model, registry=registry_with(spec))
    assert any(f["kind"] == "observation" and f["ok"] is False for f in frames)


@pytest.mark.asyncio
async def test_dangerous_tool_never_reaches_the_model():
    async def handler(args):  # noqa: ANN001
        return ToolObservation(text="不该被调用")

    class Args(BaseModel):
        pass

    dangerous = ToolSpec(
        name="mcp:evil:drop_table",
        description="危险",
        parameters_schema=Args.model_json_schema(),
        handler=handler,
        dangerous=True,
        parameters=Args,
        needs_confirm=False,
    )
    safe = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[]]))
    tools, _collector = runner.build_agent_tools(registry=registry_with(dangerous, safe))
    assert [t.name for t in tools] == ["knowledge_search"]


@pytest.mark.asyncio
async def test_web_tools_do_not_enter_checklist_when_disabled():
    """条目 4.1 明文：``web_enabled=False`` 时联网工具**不进清单**。"""
    from app.agent.bootstrap import build_registry

    off = build_registry(web_enabled=False)
    tools_off, _ = runner.build_agent_tools(registry=off)
    # D4：超集 + 点名（三条清单等式唯一允许的改判；新工具无条件注册）
    names_off = {t.name for t in tools_off}
    assert {"knowledge_search", "current_time", "list_documents", "read_document"} <= names_off

    on = build_registry(web_enabled=True)
    tools_on, _ = runner.build_agent_tools(registry=on, web_enabled=True)
    assert {"knowledge_search", "web_search", "fetch_page"} <= {t.name for t in tools_on}

    # 运行期兜底：注册表里有联网工具，但 web_enabled 关着 ⇒ for_model 仍过滤掉
    tools_filtered, _ = runner.build_agent_tools(registry=on, web_enabled=False)
    names_filtered = {t.name for t in tools_filtered}
    assert {"knowledge_search", "current_time", "list_documents", "read_document"} <= names_filtered
    assert not ({"web_search", "fetch_page"} & names_filtered)


@pytest.mark.asyncio
async def test_no_tools_yields_note_and_returns():
    turn = AgentTurn(mode="react")
    frames = await collect(
        turn, question="问题", model=faux_model([says("x")]), registry=registry_with()
    )
    assert [f["kind"] for f in frames] == ["note"]
    assert frames[0]["title"] == "没有可用工具"
    assert turn.note.startswith("没有可用工具")


@pytest.mark.asyncio
async def test_missing_model_yields_note_and_returns():
    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[]]))
    turn = AgentTurn(mode="react")
    frames = await collect(turn, question="问题", model=None, registry=registry_with(spec))
    assert [f["kind"] for f in frames] == ["note"]
    assert frames[0]["title"] == "模型不可用"


@pytest.mark.asyncio
async def test_provider_error_becomes_note_not_exception():
    """模型侧报错（Faux 队列排空 ⇒ stop_reason=error）：转 note，不抛。

    ⚠️ ``回归修复`` 升级：原断言只验「不抛」（零判别力——「第 1 轮就失败」与
    「跑到第 4 轮才失败、已收了材料」外观一致）。现在钉三件可判别的事：
    ① note 可见（标题「模型调用失败」+ 轮次号 + 已用现有资料收尾）；② ``turn.model_error``
    置位；③ run 行收口值 = ``error``（不再是 ``done``）。
    """
    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[]]))
    model = faux_model([])  # 一次响应都没有 ⇒ 模型立即返回 error
    turn = AgentTurn(mode="react")
    frames = await collect(turn, question="问题", model=model, registry=registry_with(spec))
    assert turn.aborted is False
    notes = [f for f in frames if f["kind"] == "note"]
    assert notes and notes[0]["title"] == "模型调用失败"
    assert notes[0]["detail"].startswith("第 1 轮模型调用失败")
    assert notes[0]["detail"].endswith("已用现有资料继续作答。")
    assert turn.note.startswith("模型调用失败：第 1 轮")
    assert turn.model_error is True
    assert runner._audit_run_status(turn) == "error"


@pytest.mark.asyncio
async def test_provider_error_run_row_is_error_not_done(db):
    """``回归修复``（库面）：决策调用失败过的 ReAct 轮，``agent_runs.status`` = ``error``。

    原先该形态记 ``done``——「模型调用失败」与「一切正常」在审计上不可分。
    对照腿：正常收尾的轮仍记 ``done``（防把域整体改坏的过修）。
    """
    from app.models import AgentRun, Chat

    chat = Chat(title="111 载体")
    db.add(chat)
    await db.flush()

    async def _latest_run() -> AgentRun | None:
        rows = await db.execute(
            select(AgentRun).where(AgentRun.chat_id == int(chat.id)).order_by(AgentRun.id.desc())
        )
        return rows.scalars().first()

    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[]]))
    turn = AgentTurn(mode="react")
    await collect(
        turn,
        question="问题",
        model=faux_model([]),
        registry=registry_with(spec),
        db=db,
        chat_id=int(chat.id),
        turn_no=1,
    )
    run = await _latest_run()
    assert run is not None
    assert run.status == "error"
    assert run.finished_at is not None

    turn2 = AgentTurn(mode="react")
    await collect(
        turn2,
        question="问题",
        model=faux_model([says("正常收尾")]),
        registry=registry_with(spec),
        db=db,
        chat_id=int(chat.id),
        turn_no=2,
    )
    run2 = await _latest_run()
    assert run2 is not None and run2.status == "done"


@pytest.mark.asyncio
async def test_truncated_decision_is_visible_as_note():
    """决策输出预算用尽（``stop_reason="length"``）⇒ **留一条 note**，不静默。

    600 只覆盖「决策 + 工具参数」这一步：被截断时模型可能正要调工具、或工具参数被切成半截，
    两者外观都像「Agent 什么都没干」。留痕之后才分得清「不需要工具」与「被切断了」。
    """
    from cubeloop.providers import faux_assistant_message

    retrieval = CollectingRetrieval([[material(1)]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = faux_model([faux_assistant_message("我要调用……", stop_reason="length")])

    turn = AgentTurn(mode="react")
    frames = await collect(turn, question="问题", model=model, registry=registry_with(spec))
    notes = [f for f in frames if f["kind"] == "note"]
    assert [n["title"] for n in notes] == ["决策被截断"]
    assert str(runner.DECISION_MAX_OUTPUT_TOKENS) in notes[0]["detail"]
    assert "决策被截断" in turn.note


@pytest.mark.asyncio
async def test_normal_finish_has_no_truncation_note():
    """正常收尾（``stop_reason="stop"``）不得冒出截断 note——否则这条留痕会变成噪音。"""
    retrieval = CollectingRetrieval([[material(1)]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = faux_model([says("可以作答了")])
    turn = AgentTurn(mode="react")
    frames = await collect(turn, question="问题", model=model, registry=registry_with(spec))
    assert [f["kind"] for f in frames] == ["thought"]
    assert turn.note == ""


@pytest.mark.asyncio
async def test_cancellation_marks_turn_aborted():
    """停止路径：消费方取消生成器 ⇒ turn.aborted 置位，CancelledError 照常传播。"""
    from cubeloop.providers import FauxProvider

    provider = FauxProvider(provider_id="faux", tokens_per_second=1.0)
    provider.set_responses([says("很长的一段话" * 40)])
    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[]]))
    turn = AgentTurn(mode="react")

    async def consume():
        async for _ in stream_agent_turn(
            turn, question="问题", model=provider.model("faux"), registry=registry_with(spec)
        ):
            pass

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert turn.aborted is True


# --------------------------------------------------------------------------
# P&E（条目 4.2）：纯函数——计划提示词 / 渲染 / 解析
# --------------------------------------------------------------------------


def test_plan_system_prompt_is_frozen_verbatim():
    """计划器 system 提示是**冻结文案**（老基线 ``agent.py:68-71`` 逐字），不许改写。"""
    assert runner.PLAN_SYSTEM == (
        "你是知澜的计划器，负责把问题拆成至多 5 步的可执行计划。"
        "只回一个 JSON 对象：不要输出 markdown 代码块、解释或任何多余文字。"
    )
    assert runner.PLAN_MAX_STEPS == 5


def test_plan_prompt_lists_real_tools_and_verbatim_tail():
    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[]]))
    tools, _ = runner.build_agent_tools(registry=registry_with(spec))
    prompt = runner.plan_prompt("蓝鲸是什么", tools, [{"role": "user", "content": "上一问"}])

    assert prompt.startswith("原始问题：蓝鲸是什么")
    assert "最近对话：\n用户：上一问" in prompt
    # 工具清单来自**真清单**（R2：不编造工具名），参数用 pydantic 必填键渲染（v2 的必要适配）
    assert "- knowledge_search：" in prompt
    assert '（参数：{"query": null})' in prompt
    # 括号混排是**冻结文案**（老基线 agent.py:233 就是「全角 （ 配半角 )」）——
    # 钉住它，免得后人「顺手改对」成 `）` 而无人察觉。
    assert "\uff09" not in prompt.split("可用工具：")[1].split("\n\n")[0]
    assert "web_search" not in prompt  # web 门控（R3）：关着时计划器也看不见联网工具
    # 末段逐字（含那个形状样例与「tool 为 null」的说明）
    assert prompt.endswith(
        "请把回答这个问题拆成至多 5 步，只回一个 JSON 对象，不要输出代码块围栏或其他文字：\n"
        '{"steps": [{"goal": "这一步要达成什么", "tool": "工具名或null", "args": {…}}]}\n'
        "tool 为 null 表示这一步只做推理整理，不需要工具。"
    )


def test_tools_section_is_empty_for_empty_registry():
    """空清单 ⇒ 空串（不是老基线那句 ``（没有可用工具）``）。

    运行器在调用前已用 ``note("没有可用工具")`` 早退，空清单到不了提示词渲染；保留老基线字面值
    没有收益，故**有意**不同——这条用例把「有意」与「忘了」区分开（对抗性核验的 §三-12）。
    """
    assert runner._tools_section([]) == ""


def test_plan_prompt_lists_web_tools_when_enabled():
    """R3 的反面：开了联网时计划器**看得见**联网工具（清单只渲染一次）。"""
    from app.agent.bootstrap import build_registry

    tools, _ = runner.build_agent_tools(registry=build_registry(web_enabled=True), web_enabled=True)
    prompt = runner.plan_prompt("今天天气", tools)
    assert "- web_search：" in prompt
    assert "- fetch_page：" in prompt


def test_plan_text_renders_goal_and_tool_per_line():
    text = runner.plan_text(
        [
            {"goal": "先查定义", "tool": "knowledge_search"},
            {"goal": "再整理", "tool": None},
        ]
    )
    assert text == "1. 先查定义（工具：knowledge_search）\n2. 再整理（无工具）"


def test_parse_plan_accepts_object_and_strips_fence():
    assert runner.parse_plan('{"steps": [{"goal": "g", "tool": null}]}') == [
        {"goal": "g", "tool": None}
    ]
    assert runner.parse_plan('```json\n{"steps": [{"goal": "g"}]}\n```') == [{"goal": "g"}]


def test_parse_plan_rejects_bad_shapes():
    for bad in (None, "", "不是 JSON", '{"steps": "不是列表"}', '{"steps": []}', '{"steps": [1, 2]}'):
        assert runner.parse_plan(bad) is None, bad


# --------------------------------------------------------------------------
# P&E：执行结构（口径 A）
# --------------------------------------------------------------------------


async def run_plan(turn: AgentTurn, **kwargs) -> list[dict]:
    """P&E 用例的入口壳：与 ``collect`` 同一件事（留个名字让用例读起来分得清两条路径）。"""
    return await collect(turn, **kwargs)


@pytest.mark.asyncio
async def test_plan_call_is_single_with_frozen_discipline():
    """断言 1：先有**一次**计划调用，system 逐字、600、不带 reasoning。"""
    retrieval = CollectingRetrieval([[material(1)]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = RecordingModel(
        [plan_json({"goal": "查资料", "tool": "knowledge_search", "args": {"query": "q1"}})]
    )
    turn = AgentTurn(mode="plan_execute")
    await run_plan(turn, question="问题", model=model, registry=registry_with(spec))

    assert model.count == 1  # 计划只问一次，**不在循环里**
    call = model.calls[0]
    assert call["system_prompt"] == runner.PLAN_SYSTEM
    assert call["max_output_tokens"] == runner.DECISION_MAX_OUTPUT_TOKENS == 600
    assert call["reasoning"].mode == "off"
    assert model.last_prompt.startswith("原始问题：问题")
    assert "可用工具：" in model.last_prompt


@pytest.mark.asyncio
async def test_plan_step_emits_plan_then_thought_tool_observation():
    """断言 2：首帧是 ``plan`` 步，且单步展开为 thought → tool → observation。"""
    retrieval = CollectingRetrieval([[material(1)]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = RecordingModel(
        [plan_json({"goal": "查定义", "tool": "knowledge_search", "args": {"query": "蓝鲸"}})]
    )
    turn = AgentTurn(mode="plan_execute")
    frames = await run_plan(turn, question="蓝鲸是什么", model=model, registry=registry_with(spec))

    assert kinds_of(frames) == ["plan", "thought", "tool", "observation"]
    assert frames[0]["title"] == "执行计划"
    assert frames[0]["detail"] == "1. 查定义（工具：knowledge_search）"
    assert frames[1]["title"] == "思考" and frames[1]["detail"] == "查定义"
    assert frames[2]["title"] == "检索知识库：蓝鲸"
    assert frames[2]["args"] == {"query": "蓝鲸"}
    assert frames[3]["title"] == "观察结果" and frames[3]["ok"] is True
    assert "[1] doc1.md" in frames[3]["detail"]
    assert retrieval.calls[0][1] == "蓝鲸"
    assert [m.ordinal for m in turn.materials] == [1]
    assert turn.note == ""


@pytest.mark.asyncio
async def test_plan_executes_every_step_without_asking_the_model_again():
    """断言 3：N 步计划 ⇒ 计划调用 **1** 次、步间 **0** 次模型调用。"""
    retrieval = CollectingRetrieval([[material(1)], [material(2)]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = RecordingModel(
        [
            plan_json(
                {"goal": "第一步", "tool": "knowledge_search", "args": {"query": "甲"}},
                {"goal": "第二步", "tool": "knowledge_search", "args": {"query": "乙"}},
            )
        ]
    )
    turn = AgentTurn(mode="plan_execute")
    frames = await run_plan(turn, question="两问", model=model, registry=registry_with(spec))

    assert kinds_of(frames) == [
        "plan",
        "thought",
        "tool",
        "observation",
        "thought",
        "tool",
        "observation",
    ]
    assert model.count == 1  # 步间一次都没再问模型
    assert [call[1] for call in retrieval.calls] == ["甲", "乙"]
    # 第二次检索的观察**不从 1 重新编号**（I3；快照编号由同一个 ToolCollector 给）
    assert "[1] doc1.md" in frames[3]["detail"]
    assert "[2] doc2.md" in frames[6]["detail"]
    assert [m.ordinal for m in turn.materials] == [1, 2]


@pytest.mark.asyncio
async def test_plan_longer_than_ceiling_is_truncated_with_note():
    """断言 4：计划 > 5 步 ⇒ 只执行前 5 步 + 一条 ``计划过长``（文案含 5）。"""
    retrieval = CollectingRetrieval([[material(i)] for i in range(1, 8)])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = RecordingModel(
        [
            plan_json(
                *[
                    {"goal": f"第{i}步", "tool": "knowledge_search", "args": {"query": f"q{i}"}}
                    for i in range(1, 8)
                ]
            )
        ]
    )
    turn = AgentTurn(mode="plan_execute")
    frames = await run_plan(turn, question="问题", model=model, registry=registry_with(spec))

    notes = [f for f in frames if f["kind"] == "note"]
    assert titles_of(notes) == ["计划过长"]
    assert "7" in notes[0]["detail"] and "5" in notes[0]["detail"]
    assert len([f for f in frames if f["kind"] == "tool"]) == runner.PLAN_MAX_STEPS
    assert [call[1] for call in retrieval.calls] == ["q1", "q2", "q3", "q4", "q5"]


@pytest.mark.asyncio
async def test_plan_parse_failure_falls_back_to_single_search():
    """断言 5：计划不可解析 ⇒ ``计划回退`` note **且真的执行了一次 knowledge_search**。"""
    retrieval = CollectingRetrieval([[material(1)]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = RecordingModel(["这不是 JSON"])
    turn = AgentTurn(mode="plan_execute")
    frames = await run_plan(turn, question="蓝鲸是什么", model=model, registry=registry_with(spec))

    assert kinds_of(frames) == ["note", "tool", "observation"]
    assert frames[0]["title"] == "计划回退"
    assert frames[0]["detail"] == "计划解析失败，已回退为只检索一次知识库。"
    assert frames[1]["tool"] == "knowledge_search"
    assert frames[1]["args"] == {"query": "蓝鲸是什么"}  # 原始问题当检索词
    assert len(frames) == 3  # **恰好一次**检索，没有第二步
    assert len(retrieval.calls) == 1
    assert [m.ordinal for m in turn.materials] == [1]


@pytest.mark.asyncio
async def test_plan_step_without_usable_tool_is_skipped_without_a_call():
    """断言 6：``tool`` 为 null / 不在清单 ⇒ ``跳过：无可用工具``，**不产生工具调用**。"""
    retrieval = CollectingRetrieval([[material(1)]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = RecordingModel(
        [
            plan_json(
                {"goal": "这一步只整理", "tool": None},
                # goal 也空 ⇒ 老基线的回落文案（细节见下）
                {"goal": "", "tool": "no_such_tool"},
                {"goal": "最后查一次", "tool": "knowledge_search", "args": {"query": "甲"}},
            )
        ]
    )
    turn = AgentTurn(mode="plan_execute")
    frames = await run_plan(turn, question="问题", model=model, registry=registry_with(spec))

    assert kinds_of(frames) == ["plan", "note", "note", "thought", "tool", "observation"]
    assert titles_of(frames)[1:3] == ["跳过：无可用工具"] * 2
    assert frames[1]["detail"] == "这一步只整理"  # goal 非空 ⇒ 用 goal
    assert frames[2]["detail"] == "工具不可用：no_such_tool"  # goal 空 ⇒ 用工具名
    # 两次跳过**都没有**产生工具调用（空转调用会让「没调」与「调了没用」分不清）
    assert len([f for f in frames if f["kind"] == "tool"]) == 1
    assert len(retrieval.calls) == 1


@pytest.mark.asyncio
async def test_plan_step_bad_args_becomes_failed_observation_not_skip():
    """断言 7（§2.2-1 的细化）：``args`` 校验失败 ⇒ 一次 ``ok=False`` 的观察，**不静默跳过**。"""
    retrieval = CollectingRetrieval([[material(1)]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = RecordingModel(
        [
            plan_json(
                # limit 有 ``le=FUSE_LIMIT`` 约束：999 必然校验不过
                {"goal": "越界参数", "tool": "knowledge_search", "args": {"query": "甲", "limit": 999}},
                {"goal": "这一步照常", "tool": "knowledge_search", "args": {"query": "乙"}},
            )
        ]
    )
    turn = AgentTurn(mode="plan_execute")
    frames = await run_plan(turn, question="问题", model=model, registry=registry_with(spec))

    assert kinds_of(frames) == [
        "plan",
        "thought",
        "tool",
        "observation",
        "thought",
        "tool",
        "observation",
    ]
    failed = frames[3]
    assert failed["title"] == "工具执行失败" and failed["ok"] is False
    assert "参数校验失败" in failed["detail"]
    assert failed["tool_call_id"] == frames[2]["tool_call_id"]  # 一次调用一个 id（I4）
    # 失败**不中断**计划：第二步照常执行
    assert frames[6]["ok"] is True
    assert len(retrieval.calls) == 1  # 校验失败的这一步没有真去检索


@pytest.mark.asyncio
async def test_plan_execute_ignores_agent_max_rounds(monkeypatch):
    """断言 8：P&E 全程 ``turn.rounds == 0``；``AGENT_MAX_ROUNDS=1`` 也照样跑完整个计划。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENT_MAX_ROUNDS", 1)
    retrieval = CollectingRetrieval([[material(i)] for i in range(1, 5)])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = RecordingModel(
        [
            plan_json(
                *[
                    {"goal": f"第{i}步", "tool": "knowledge_search", "args": {"query": f"q{i}"}}
                    for i in range(1, 4)
                ]
            )
        ]
    )
    turn = AgentTurn(mode="plan_execute")
    frames = await run_plan(turn, question="问题", model=model, registry=registry_with(spec))

    assert turn.rounds == 0  # P&E 的上限是 PLAN_MAX_STEPS，不是 AGENT_MAX_ROUNDS
    assert len([f for f in frames if f["kind"] == "tool"]) == 3
    assert "已达上限" not in titles_of(frames)


@pytest.mark.asyncio
async def test_plan_execute_trace_shape_and_no_degradation_note():
    """断言 9：``agent_trace.mode == "plan_execute"``，且轨迹里**没有**降级 note。"""
    retrieval = CollectingRetrieval([[material(1)]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = RecordingModel(
        [plan_json({"goal": "查定义", "tool": "knowledge_search", "args": {"query": "蓝鲸"}})]
    )
    turn = AgentTurn(mode="plan_execute")
    frames = await run_plan(turn, question="蓝鲸", model=model, registry=registry_with(spec))

    trace = turn.trace()
    assert trace["mode"] == "plan_execute"
    assert set(trace) == {"mode", "steps", "events_incomplete"}  # 只增不改义
    assert trace["events_incomplete"] is False
    assert titles_of(trace["steps"]) == ["执行计划", "思考", "检索知识库：蓝鲸", "观察结果"]
    assert "计划执行未实装" not in titles_of(frames)
    assert turn.rounds == 0


@pytest.mark.asyncio
async def test_plan_model_unavailable_note_without_exploding():
    """断言 12（I5）：计划调用抛错 ⇒ ``模型不可用`` note，**不炸穿**、正常收尾。"""

    class BoomModel:
        async def generate(self, *_args, **_kwargs):
            raise RuntimeError("上游 502")

    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[]]))
    turn = AgentTurn(mode="plan_execute")
    frames = await run_plan(turn, question="问题", model=BoomModel(), registry=registry_with(spec))

    assert kinds_of(frames) == ["note"]
    assert frames[0]["title"] == "模型不可用"
    assert "上游 502" in frames[0]["detail"]
    assert turn.note.startswith("模型不可用")
    assert turn.materials == []


@pytest.mark.asyncio
async def test_plan_execute_captures_truncated_plan_call():
    """计划调用被截断（``stop_reason="length"``）⇒ 留痕，而不是让人猜「模型没给计划」。"""
    retrieval = CollectingRetrieval([[material(1)]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)

    class TruncatingModel(RecordingModel):
        async def generate(self, messages, **kwargs):  # noqa: ANN003
            reply = await super().generate(messages, **kwargs)
            return faux_message("半截的", stop_reason="length")

    model = TruncatingModel([plan_json({"goal": "g", "tool": "knowledge_search"})])
    turn = AgentTurn(mode="plan_execute")
    frames = await run_plan(turn, question="问题", model=model, registry=registry_with(spec))

    # 截断留痕 + 解析失败回退（两条都在：既不掩盖截断，也不放弃检索）
    assert titles_of(frames)[:2] == ["决策被截断", "计划回退"]
    assert str(runner.DECISION_MAX_OUTPUT_TOKENS) in frames[0]["detail"]
    assert len(retrieval.calls) == 1


@pytest.mark.asyncio
async def test_plan_execute_without_tools_yields_note_and_returns():
    """没有可用工具时两条路径同一动作（P&E 也不会把计划器叫起来空跑）。"""
    model = RecordingModel([plan_json({"goal": "g", "tool": None})])
    turn = AgentTurn(mode="plan_execute")
    frames = await run_plan(turn, question="问题", model=model, registry=registry_with())
    assert kinds_of(frames) == ["note"]
    assert frames[0]["title"] == "没有可用工具"
    assert model.count == 0


@pytest.mark.asyncio
async def test_plan_execute_without_model_yields_note_and_returns():
    spec = knowledge_search.build_spec(db=object(), retrieve=CollectingRetrieval([[]]))
    turn = AgentTurn(mode="plan_execute")
    frames = await run_plan(turn, question="问题", model=None, registry=registry_with(spec))
    assert kinds_of(frames) == ["note"]
    assert frames[0]["title"] == "模型不可用"
