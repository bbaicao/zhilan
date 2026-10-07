"""专项 · 工具调用能力第 1 批 · ① 短句不再因字数硬短路（HTTP / wire）。

装配照 ``t0_wire``：进程内 ASGI、假上游、真库。请求体都显式带 ``agent_mode``
（``conftest.pin_agent_mode_off`` 会把缺省钉成 off）。
ReAct 的决策调用在 wire 上是 ``stream: true``，响应用 ``wire_sse_*`` 排队，不用 ``wire_json``。

``plan_execute`` 的计划调用不传 ``tools[]``（工具写在计划提示词里）。
这条腿用「进了助手、不是澄清、计划调用和回答调用各一次」来防「只对 react 生效」。
``react`` 仍必须看到带 ``tools[]`` 的 decision。
"""

from __future__ import annotations

import pytest

from app.api import chat_routes
from app.core.config import settings
from app.generation import answer as answer_mod
from app.generation import clarify
from app.models import Chat
from app.routing import decisions as routing_decisions
from app.session import events as event_log
from app.session import repository
from tests.api.t0_wire import (  # noqa: F401 —— client / wire_env 是夹具
    Wire,
    attach_wire,
    client,
    run_stream,
    wire_env,
    wire_sse_text,
)

pytestmark = pytest.mark.asyncio

#: 短于 ``clarify.MIN_LENGTH``（现为 4）的非空中文。
SHORT = "好"
#: 正常长度、可缓存的 off 问法（与 ``test_semantic_cache_http`` 的命中问句同一句，不另放宽）。
CACHEABLE = "向量库用什么存储？"
_NO_TOOL_PLAN = '{"steps":[{"goal":"直接整理","tool":null,"args":{}}]}'


def _check_short() -> None:
    assert SHORT.strip(), "message 必须非空"
    assert len(SHORT.strip()) < clarify.MIN_LENGTH


def _react_wire() -> Wire:
    """决策直接收尾、不调工具，然后走回答。两次都是流式。"""
    return Wire(wire_sse_text("可以作答了"), wire_sse_text("短句也作答。"))


def _plan_wire() -> Wire:
    """计划返回「无工具」的一步（空计划会回退成 knowledge_search）。两次都是流式。"""
    return Wire(wire_sse_text(_NO_TOOL_PLAN), wire_sse_text("短句也作答。"))


def _clarify_wire() -> Wire:
    """off 短句的那一次模型调用是澄清文案，不是 Agent 决策。"""
    return Wire(wire_sse_text("你指的是哪一份？"))


def _done(events: list[dict]) -> dict:
    done = [event for event in events if event.get("type") == "done"]
    assert len(done) == 1, events
    return done[0]


async def _rollback_rounds(db, chat_id: int) -> int:
    await db.rollback()
    chat = await db.get(Chat, chat_id)
    assert chat is not None
    return int(chat.clarify_rounds or 0)


async def _turn_ends(db, chat_id: int):
    await db.rollback()
    return await event_log.list_events_by_type(db, chat_id, "turn/end")


async def _decisions(db, chat_id: int):
    await db.rollback()
    return await routing_decisions.load_decisions(db, chat_id)


# --------------------------------------------------------------------------
# A1 · 短句进 Agent（react / plan_execute）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["react", "plan_execute"])
async def test_a1_short_query_enters_agent_without_burning_clarify(
    client, db, wire_env, monkeypatch, mode
):
    """A1：短句出现带 tools 的 decision，kind / turn/end 都不是 clarify，追问计数不 +1。"""
    _check_short()
    wire = _plan_wire() if mode == "plan_execute" else _react_wire()
    attach_wire(wire, monkeypatch)

    events = await run_stream(client, message=SHORT, agent_mode=mode)
    done = _done(events)
    assert done["kind"] != "clarify"
    assert (done.get("agent_trace") or {}).get("mode") == mode

    ends = await _turn_ends(db, done["chat_id"])
    assert len(ends) == 1, [row.type for row in ends]
    assert ends[0].data.get("kind") != "clarify"
    assert await _rollback_rounds(db, done["chat_id"]) == 0

    assert wire.bodies, "模型一次都没被调用"
    assert wire.queue == []
    if mode == "plan_execute":
        assert wire.call_kinds() == ["answer", "answer"], wire.call_kinds()
    else:
        assert "decision" in wire.call_kinds(), wire.call_kinds()
        assert wire.decision_bodies[0].get("tools")
    assert wire.queue == []


# --------------------------------------------------------------------------
# A2 · off 短句仍走澄清
# --------------------------------------------------------------------------


async def test_a2_off_short_query_stays_clarify(client, db, wire_env, monkeypatch):
    """A2：同一短句、agent_mode=off。澄清那一腿，不是 Agent。"""
    _check_short()
    wire = _clarify_wire()
    attach_wire(wire, monkeypatch)

    events = await run_stream(client, message=SHORT, agent_mode="off")
    done = _done(events)
    assert done["kind"] == "clarify"

    ends = await _turn_ends(db, done["chat_id"])
    assert len(ends) == 1
    assert ends[0].data.get("kind") == "clarify"
    assert await _rollback_rounds(db, done["chat_id"]) == 1
    assert wire.call_kinds() == ["answer"], wire.call_kinds()
    assert wire.queue == []


# --------------------------------------------------------------------------
# A3 · 上限边界成对（真库里的 clarify_rounds）
# --------------------------------------------------------------------------


async def test_a3_assume_boundary_still_enters_agent(client, db, wire_env, monkeypatch):
    """A3：rounds == max 带假设前缀；rounds == max-1 不带。两条腿都仍是 Agent 决策。"""
    _check_short()
    limit = clarify.max_rounds()
    if limit < 1:
        pytest.fail(
            f"夹具缺口：clarify.max_rounds()={limit}，没有 max_rounds()-1 这条腿可设"
        )

    captured: list[str] = []
    real_prefix = chat_routes.get_turn_prefix

    def _spy(plan, raw_text: str) -> str:
        prefix = real_prefix(plan, raw_text)
        captured.append(prefix)
        return prefix

    monkeypatch.setattr(chat_routes, "get_turn_prefix", _spy)

    async def _leg(rounds: int, *, assume: bool) -> None:
        captured.clear()
        chat = await repository.create_chat(db, title="a3")
        chat.clarify_rounds = rounds
        await db.commit()
        wire = _react_wire()
        attach_wire(wire, monkeypatch)
        events = await run_stream(
            client, message=SHORT, agent_mode="react", chat_id=chat.id
        )
        done = _done(events)
        assert done["chat_id"] == chat.id
        assert done["kind"] != "clarify"
        assert "decision" in wire.call_kinds(), wire.call_kinds()
        assert wire.decision_bodies[0].get("tools")
        assert captured, "get_turn_prefix 没被调用"
        if assume:
            assert clarify.assume_prefix(True) in captured[-1]
        else:
            assert answer_mod.ASSUME_PREFIX not in captured[-1]
        assert wire.queue == []

    await _leg(limit, assume=True)
    await _leg(limit - 1, assume=False)


# --------------------------------------------------------------------------
# A5 · judge_query 成对计数
# --------------------------------------------------------------------------


async def test_a5_judge_query_skipped_for_agent_called_for_off(
    client, wire_env, monkeypatch
):
    """A5：react 时 judge_query 0 次；off 时至少 1 次。"""
    _check_short()
    calls = {"n": 0}
    real = clarify.judge_query

    async def _spy(message, history=None, **kwargs):
        calls["n"] += 1
        return await real(message, history, **kwargs)

    monkeypatch.setattr(clarify, "judge_query", _spy)

    wire = _react_wire()
    attach_wire(wire, monkeypatch)
    await run_stream(client, message=SHORT, agent_mode="react")
    assert calls["n"] == 0
    assert wire.queue == []

    before = calls["n"]
    off = _clarify_wire()
    attach_wire(off, monkeypatch)
    done = _done(await run_stream(client, message=SHORT, agent_mode="off"))
    assert done["kind"] == "clarify"
    assert calls["n"] - before >= 1
    assert off.queue == []


# --------------------------------------------------------------------------
# A6 · routing/decided 仍恰好一条
# --------------------------------------------------------------------------


async def test_a6_agent_short_turn_has_one_downgraded_routing_decision(
    client, db, wire_env, monkeypatch
):
    """A6：Agent 短句轮恰好一条 routing/decided，reason 含降级话；off 来源仍是三元组之一。"""
    _check_short()
    wire = _react_wire()
    attach_wire(wire, monkeypatch)
    done = _done(await run_stream(client, message=SHORT, agent_mode="react"))
    decisions = await _decisions(db, done["chat_id"])
    assert len(decisions) == 1, decisions
    assert "规则短路已降级" in decisions[0].reason
    assert wire.queue == []

    off = _clarify_wire()
    attach_wire(off, monkeypatch)
    off_done = _done(await run_stream(client, message=SHORT, agent_mode="off"))
    off_decisions = await _decisions(db, off_done["chat_id"])
    assert len(off_decisions) == 1, off_decisions
    assert off_decisions[0].source in {"llm", "clarify", "regex"}
    assert "规则短路已降级" not in off_decisions[0].reason
    assert off.queue == []


# --------------------------------------------------------------------------
# A19 · off 链缓存写回不因本批漂移
# --------------------------------------------------------------------------


async def test_a19_off_cache_write_count_matches_off_only(client, db, monkeypatch):
    """A19：正常 off 问句连问两次，写回 1 条且第二次命中；短句 off 是 clarify，不进缓存。

    计数口径与 ``test_semantic_cache_http.test_a4_a19_second_identical_question_skips_retrieval_and_model``
    相同（检索 1、模型 1、kind=normal）。本用例不走 Wire：缓存命中轮不再打上游。
    """
    from app.cache import store as cache_store
    from tests.api.test_semantic_cache_http import (
        _RetrievalCounter,
        _flush_cache_writes,
        _install,
        _materials_from,
    )

    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)
    # 短句追问走 aux；本例自钉到同一假模型，不依赖跨文件 collect 的 API autouse。
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: model)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 86400)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 256)
    cache_store.reset_cache()
    cache_store.reset_failure_counts()

    first = await client.post(
        "/api/chat", json={"message": CACHEABLE, "agent_mode": "off"}
    )
    assert first.status_code == 200, first.text
    await _flush_cache_writes()
    assert first.json()["kind"] == "normal"
    assert cache_store.failure_counts()["stored"] == 1
    assert counter.count == 1
    assert model.count == 1

    second = await client.post(
        "/api/chat", json={"message": CACHEABLE, "agent_mode": "off"}
    )
    assert second.status_code == 200, second.text
    await _flush_cache_writes()
    assert second.json()["kind"] == "normal"
    assert second.json()["message"] == first.json()["message"]
    counts = cache_store.failure_counts()
    assert counts["stored"] == 1, counts
    assert counts["hit"] == 1, counts
    assert counter.count == 1
    assert model.count == 1

    short = await client.post("/api/chat", json={"message": SHORT, "agent_mode": "off"})
    assert short.status_code == 200, short.text
    await _flush_cache_writes()
    assert short.json()["kind"] == "clarify"
    assert cache_store.failure_counts()["stored"] == 1
    assert cache_store.cache_size() == 1


# --------------------------------------------------------------------------
# A20 · Agent 短句轮的 retrieval/* 还在，且早于 tool/call
# --------------------------------------------------------------------------


async def test_a20_agent_short_turn_keeps_retrieval_pair_before_tools(
    client, db, wire_env, monkeypatch
):
    """A20：react 短句轮恰好一对 retrieval/start + retrieval/result，start 早于任何 tool/call。"""
    _check_short()
    wire = _react_wire()
    attach_wire(wire, monkeypatch)
    done = _done(await run_stream(client, message=SHORT, agent_mode="react"))

    await db.rollback()
    rows = await event_log.list_events(db, done["chat_id"])
    types = [row.type for row in rows]
    assert types.count("retrieval/start") == 1, types
    assert types.count("retrieval/result") == 1, types
    start_at = types.index("retrieval/start")
    assert "tool/call" not in types[:start_at], types
    assert wire.queue == []


# ==========================================================================
# 追加段（2026-09-22 · 第 1 批**验收判据补齐**）：A7 / A20 / A1 / A19
#
# 这四条都是「既有判据有洞」的补强，**既有 8 例的断言一字未动**（只在本段之后追加）：
#
# | 用例 | 旧判据的洞 |
# |---|---|
# | ``test_a7_*`` | 只读 ``plan.materials`` 这类**内部字段** ⇒ 「只清 ``materials`` 不同步 ``materials_text``」照样绿（D21 点名的最严重盲区） |
# | ``test_a20_*`` | 旧 A20 用的是**零工具调用**的 wire ⇒ ``"tool/call" not in types[:start_at]`` **恒真**，lazy candidates 的坏实现照样绿 |
# | ``test_a1_*`` | 只断 ``call_kinds()`` + ``mode`` —— ``mode`` 是**回声**（进过 Agent 就会带上）⇒ 分不出「真跑 P&E」与「多发一次无工具调用」 |
# | ``test_a19_*`` | 只钉 ``stored == 1`` 这类**现状数字** ⇒ 判据本身（``is_cacheable_plan``）漂了也可能绿 |
# ==========================================================================

from sqlalchemy import select

from app.agent import runner as agent_runner
from app.cache.store import is_cacheable_plan
from app.models import Chunk
from app.retrieval.types import Material, number, render_materials
from tests.api.test_chat_contract import _StubClassifier  # noqa: F401 —— 本段自己钉分类器
from tests.api.test_semantic_cache_http import (  # noqa: F401
    _RetrievalCounter,
    _install,
    _materials_from,
)
from tests.api.t0_wire import (  # noqa: F401 —— fake_mcp 是夹具
    FAKE_TOOL_CONTRACT,
    FAKE_TOOL_NAME,
    fake_mcp,
    wire_sse_tool_call,
)
from tests.retrieval.conftest import seed_document


#: 本段 A7 的候选文本（与 ``test_agent_closeout_http_t1`` 同串）。
CANDIDATE = "候选句子甲乙丙。"
#: 本段 A7 的问句（与 ``test_agent_closeout_http_t1`` 同句：够长，不进澄清）。
A7_ASK = "帮我看一下资料"
#: 本段 A20 的问句（与 ``test_tool_context_t0._react_with_fake_tool`` 同句）。
A20_ASK = "用工具看一眼"


async def _install_candidate(db, monkeypatch):
    """装一份**库内候选**：假 ``retrieve_materials`` + 真库文档。

    这是 ``tests/api/test_agent_closeout_http_t1.py::_install_candidate`` 的**逐字副本**
    （开工指令：「候选注入**照抄**」）。⚠️ **有意照抄而不是 import**：那个文件属于**并发会话**的
    在改面（同仓多会话共用一份工作树）⇒ 跨文件 import 私有助手会把我这份用例吊在别人的编辑上。
    副本与原件同形，只改「落在哪个模块里」。
    """
    document = await seed_document(db, [CANDIDATE], filename="候选.md")
    rows = await db.execute(
        select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ordinal)
    )
    materials = [
        Material(
            chunk_id=chunk.id,
            document_id=document.id,
            content=chunk.content,
            filename="候选.md",
            block_ordinal=chunk.ordinal,
        )
        for chunk in rows.scalars().all()
    ]
    rendered = render_materials(number(materials))

    async def fake_retrieve(session, text, filters=None, *, stats=None):  # noqa: ANN001
        return materials, rendered, None

    monkeypatch.setattr(chat_routes, "retrieve_materials", fake_retrieve)
    return rendered


def _capture_plans(monkeypatch) -> list:
    """把每一轮的 ``TurnPlan`` 截下来（取本轮 plan 的官方办法：``persist_turn`` 是唯一收口点）。

    ⚠️ **只存引用、不改行为**：spy 里照原样调真函数（不调的话落库 / 事件全断，判据也没得看）。
    """
    captured: list = []
    real = chat_routes.persist_turn

    async def _spy(db, plan, answer, **kwargs):  # noqa: ANN001
        captured.append(plan)
        return await real(db, plan, answer, **kwargs)

    monkeypatch.setattr(chat_routes, "persist_turn", _spy)
    return captured


def _plan_prompt_tail() -> str:
    """``runner.plan_prompt`` 的**末段**（冻结文案：JSON 形状样例 + 「tool 为 null」说明）。

    按**符号**现取（``plan_prompt(SHORT, [])`` 的最后一个空行分段）而不是硬编字符串：
    上游改文案时判据跟着走，不会退化成「断言一段已经没人产出的文本」。
    """
    return agent_runner.plan_prompt(SHORT, []).split("\n\n")[-1].strip()


def _a7_expect_zero_tool_round_kept(wire, plan, done, rendered: str) -> None:
    """A7 三件判据：无工具且有准入库内候选，快照、最终请求与来源一致。"""
    assert plan.materials
    assert plan.materials_text == rendered == render_materials(plan.materials)
    assert plan.all_refs and plan.refs
    answer_body = wire.answer_bodies[-1]
    assert CANDIDATE in wire.last_user_text(answer_body)
    assert (plan.kind, plan.answer_source, plan.source_type) == ("normal", "kb", "internal_db")
    assert done["kind"] == "normal" and done["answer_source"] == "kb"
    assert done["refs"]


async def test_a7_zero_tool_round_keeps_accepted_candidates(
    client, db, wire_env, monkeypatch
):
    """A7：零工具步且未拒用已有库内候选，保留快照并送入最终回答。"""
    rendered = await _install_candidate(db, monkeypatch)
    # ⚠️ ``wire_env`` 把分类器钉成 ``external`` ⇒ ``plan.kind`` 恒 ``direct``（`external` 恒定走 direct）
    # ⇒ 「库内命中」这条语义必须自己**再钉一次**内部分类器（夹具坑 ①）。
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )
    plans = _capture_plans(monkeypatch)

    wire = Wire(wire_sse_text("可以作答了"), wire_sse_text("照候选回答。"))
    attach_wire(wire, monkeypatch)
    events = await run_stream(client, message=A7_ASK, agent_mode="react")
    done = _done(events)

    # 前提：这一轮**真的**是「零工具步 + 决策 answer」（否则压根不是情形 1）
    assert wire.call_kinds() == ["decision", "answer"], wire.call_kinds()
    assert len(plans) == 1, plans

    _a7_expect_zero_tool_round_kept(wire, plans[0], done, rendered)
    assert wire.queue == []


def _a20_expect_retrieval_pair_before_first_tool(types: list[str]) -> None:
    """A20 的排序判据本体（抽成函数：**变异自证**要用同一份判据 —— mutant 用例临时加、跑完即删）。"""
    assert types.count("retrieval/start") == 1, types
    assert types.count("retrieval/result") == 1, types
    assert types.count("tool/call") == 1, f"本轮没有（或不止一次）tool/call：{types}"
    start_at = types.index("retrieval/start")
    result_at = types.index("retrieval/result")
    first_tool_at = types.index("tool/call")
    assert start_at < result_at, types
    assert start_at < first_tool_at, (
        f"retrieval/start 不早于第一个 tool/call（lazy candidates？）：{types}"
    )
    # ⚠️ 这一条比开工包 A20 的字面要求**再多钉一处**：「半 lazy」（start 早、result 晚）
    # 能骗过「start < tool/call」——但那时候选**还没拿到**，决策调用压根看不到候选
    # （D8 的「前置」二字就没了）。两条事件同在一处写（build_turn_plan）⇒ 同侧。
    assert result_at < first_tool_at, (
        f"retrieval/result 晚于第一个 tool/call（半 lazy：候选没在 Agent 之前拿到）：{types}"
    )


async def test_a20_retrieval_pair_precedes_a_real_tool_call(
    client, db, fake_mcp, wire_env, monkeypatch
):
    """A20 · 用**真带 ``tool/call``** 的一轮钉排序（旧写法对 lazy candidates 恒真 ⇒ 假绿）。

    构造：决策调假 MCP 工具（``mcp:fauxkit:probe``，返回**纯文本、不带材料**）→ 观察 →
    决策收尾 → 回答。判据：事件表里**恰一对** ``retrieval/start`` + ``retrieval/result``，
    且 ``retrieval/start`` 的下标**严格小于**第一个 ``tool/call`` 的下标。

    ⚠️ 前提断言不能省：本轮若没真发生 ``tool/call``，排序判据就**空转**（这正是旧写法的洞）。
    """
    wire = Wire(
        wire_sse_tool_call(FAKE_TOOL_CONTRACT, {"note": "probe"}, call_id="call_a20_1"),
        wire_sse_text("可以作答了"),
        wire_sse_text("工具结果已拿到。"),
    )
    attach_wire(wire, monkeypatch)
    events = await run_stream(client, message=A20_ASK, agent_mode="react")
    done = _done(events)

    # 前提 ①：这一轮是「决策 → 工具 → 决策 → 回答」四段（工具**真的**被调了）
    assert wire.call_kinds() == ["decision", "decision", "answer"], wire.call_kinds()
    # 前提 ②：假 MCP 工具的 ``call_tool`` 真被执行（不是「模型喊了一声」而已）
    assert [name for name, _ in fake_mcp.calls] == [FAKE_TOOL_NAME], fake_mcp.calls

    await db.rollback()
    rows = await event_log.list_events(db, done["chat_id"])
    types = [row.type for row in rows]

    _a20_expect_retrieval_pair_before_first_tool(types)
    assert wire.queue == []


def _a1_expect_first_call_is_a_plan_call(wire) -> None:
    """A1 ``plan_execute`` 腿的 **wire 级**判据本体（抽成函数：**变异自证**要用同一份判据 —— mutant 用例临时加、跑完即删）。"""
    assert wire.call_kinds() == ["answer", "answer"], wire.call_kinds()
    assert len(wire.bodies) == 2, wire.call_kinds()

    plan_body = wire.bodies[0]
    assert agent_runner.PLAN_SYSTEM in wire.system_prompt_of(plan_body), (
        "第一次调用的 system 段不是计划器文案：\n"
        + wire.system_prompt_of(plan_body)[:1000]
    )
    plan_user = wire.last_user_text(plan_body)
    assert f"原始问题：{SHORT}" in plan_user, plan_user[:1000]
    assert "可用工具：" in plan_user, plan_user[:1000]
    assert _plan_prompt_tail() in plan_user, plan_user[:1000]

    # 反向对照：回答那一次**不带**计划提示词特征串
    answer_body = wire.bodies[-1]
    assert agent_runner.PLAN_SYSTEM not in wire.system_prompt_of(answer_body)
    assert _plan_prompt_tail() not in wire.last_user_text(answer_body)


async def test_a1_plan_execute_first_call_carries_the_plan_prompt(
    client, db, wire_env, monkeypatch
):
    """A1 · ``plan_execute`` 腿的 **wire 级**判据：第一次调用真的是**计划调用**。

    旧写法只断 ``call_kinds() == ["answer", "answer"]`` + ``mode`` —— 而 ``mode`` 是**回声**
    （``plan_execute`` 只要进过一次 Agent 就会带上）⇒ 「真跑 P&E」与「多发一次无工具调用」
    **长得一模一样**。这里断言第一次调用的请求体里含**计划提示词的特征串**：

    - system 段 = ``runner.PLAN_SYSTEM`` 逐字（计划器冻结文案）；
    - user 段 = ``runner.plan_prompt`` 的末段（含 JSON 形状样例）+ ``原始问题：<message>`` + ``可用工具：``。

    并对**回答那一次**做反向对照（否则「两次都是计划调用」也会绿）。
    """
    _check_short()
    wire = _plan_wire()
    attach_wire(wire, monkeypatch)
    events = await run_stream(client, message=SHORT, agent_mode="plan_execute")
    done = _done(events)
    assert done["kind"] != "clarify"

    _a1_expect_first_call_is_a_plan_call(wire)
    assert wire.queue == []


def _a19_expect(plan, *, agent_mode: str, kind: str, cacheable: bool) -> None:
    """A19 的单腿判据本体（抽成函数：**变异自证**要用同一份判据 —— mutant 用例临时加、跑完即删）。

    先钉「这一腿的输入长什么样」（``agent_mode`` / ``kind``），再钉判据的输出
    ——否则「False 是因为哪一条条件」不可归因。
    """
    assert (plan.agent_mode, plan.kind) == (agent_mode, kind), (
        plan.agent_mode,
        plan.kind,
    )
    assert is_cacheable_plan(plan) is cacheable, (
        f"is_cacheable_plan(agent_mode={agent_mode}, kind={kind}) 应为 {cacheable}"
    )


async def test_a19_is_cacheable_plan_judgement_itself(
    client, db, wire_env, monkeypatch
):
    """A19 · 直接断**判据本身**（不再是 ``stored == 1`` 这类现状数字）：三条腿一起才关死。

    · 腿 1：**Agent 轮**的 plan ⇒ ``False``（只因 ``agent_mode`` 不是 ``off``）；
      零工具步且有准入候选时仍为 ``normal``；与腿 2 在缓存判据输入上只差
      ``agent_mode``，同为 ``normal`` 且无图。
    · 腿 2：``off`` 轮的 plan，``kind == "normal"`` ⇒ ``True``；
    · 腿 3：``off`` 轮的 plan，``kind == "direct"`` ⇒ ``False``（``STORED_KINDS`` 只管 ``normal``）。
    """
    plans = _capture_plans(monkeypatch)

    # --- 腿 1：Agent 轮（真 wire + 真库候选，零工具步保留 normal）------------
    await _install_candidate(db, monkeypatch)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )
    wire = Wire(wire_sse_text("可以作答了"), wire_sse_text("照候选回答。"))
    attach_wire(wire, monkeypatch)
    await run_stream(client, message=A7_ASK, agent_mode="react")
    assert len(plans) == 1, plans
    # 与腿 2 的缓存判据输入只差 agent_mode（同为 normal 且无图）。
    _a19_expect(plans[0], agent_mode="react", kind="normal", cacheable=False)
    assert wire.queue == []

    # --- 腿 2：off 链，库内命中（⇒ normal）------------------------------------
    materials, text = await _materials_from(db)
    monkeypatch.setattr(chat_routes, "retrieve_materials", _RetrievalCounter(materials, text))
    model = _install(monkeypatch)  # 假模型 + internal_db 分类器（不连真上游）
    # ⚠️ 腿 1 的 ``attach_wire`` 把 aux 也钉到那条 wire 上了 ⇒ 这里重钉回假模型，
    # 否则 off 链的任一次辅助调用会去打空队列。
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: model)

    second = await client.post("/api/chat", json={"message": CACHEABLE, "agent_mode": "off"})
    assert second.status_code == 200, second.text
    assert len(plans) == 2, plans
    assert plans[0].images == plans[1].images == []
    _a19_expect(plans[1], agent_mode="off", kind="normal", cacheable=True)

    # --- 腿 3：off 链，库内未命中（⇒ direct）⇒ 同样不进缓存 --------------------
    async def _no_materials(session, text, filters=None, *, stats=None):  # noqa: ANN001
        return [], "", None

    monkeypatch.setattr(chat_routes, "retrieve_materials", _no_materials)
    # 钉死直答开关，让这条腿的 kind 只由「有没有材料」决定（不受本机 .env 影响）
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", True)

    third = await client.post("/api/chat", json={"message": CACHEABLE, "agent_mode": "off"})
    assert third.status_code == 200, third.text
    assert len(plans) == 3, plans
    _a19_expect(plans[2], agent_mode="off", kind="direct", cacheable=False)
