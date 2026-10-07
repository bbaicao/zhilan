"""第 1 批结束决策的 HTTP / wire 判据。

## 一处**改判**（开工包 §4 的 A10b①）

A10b① 逐字写着「清候选后 `kind != "direct"`」，但**同一行**的 A10b② 又要求
「若 `kind == "normal"` 就必须 `answer_source == "kb"` 且有 refs」。

在 A11 的构造（清候选 ⇒ `plan.materials` / `materials_text` / `all_refs` 三者同清）下，
这两条**互斥**：既不许落 `direct`，落 `normal` 又必须有 refs ⇒ 只剩 `refuse` / `clarify`，
而两者都与「模型明确不用候选、照常作答」的语义不符。**复核方已判定改判**
（见交接单正文），故本文件按**具体期望值**钉现实现：

    `plan.kind == "direct"` 且 `plan.answer_source == "general"`

依据 = `chat_routes._apply_agent_closeout` 的清候选分支：`not plan.materials` 时落
`source_map.DIRECT_KIND`，而 `source_map.ANSWER_SOURCE_BY_KIND["direct"] == "general"`。
（A11⓪ 要的是「取具体期望值」，不是「不许是 direct」——后者正是「顺手把 kind 归成
direct」能全绿的成因。）
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import chat_routes
from app.api.chat_routes import RERANK_EMPTY_NOTICE
from app.generation import answer as answer_mod
from app.models import Chunk
from app.retrieval.types import Material, number, render_materials
from tests.api.t0_wire import (  # noqa: F401
    Wire,
    attach_wire,
    client,
    run_stream,
    wire_env,
    wire_sse_text,
)
from tests.retrieval.conftest import seed_document

pytestmark = pytest.mark.asyncio

CANDIDATE = "候选句子甲乙丙。"
ASK = "你指的是哪一份资料？"


def _fence(payload: str) -> str:
    return f"说明\n<<<CLOSEOUT>>>\n{payload}\n<<<END>>>\n"


def _done(events: list[dict]) -> dict:
    done = [event for event in events if event.get("type") == "done"]
    assert len(done) == 1, events
    return done[0]


def _answer_text(events: list[dict]) -> str:
    return "".join(event.get("text", "") for event in events if event.get("type") == "token")


def _notes(events: list[dict]) -> list[dict]:
    return [
        event["step"]
        for event in events
        if event.get("type") == "agent" and event.get("step", {}).get("kind") == "note"
    ]


async def _install_candidate(db, monkeypatch):
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


async def test_absent_fence_does_not_add_a_note(client, db, wire_env, monkeypatch):
    wire = Wire(wire_sse_text("可以作答了"), wire_sse_text("直接回答。"))
    attach_wire(wire, monkeypatch)
    events = await run_stream(client, message="帮我看一下资料", agent_mode="react")
    assert _done(events)["kind"] != "clarify"
    assert all(step.get("title") != "结束决策无效" for step in _notes(events))
    assert wire.queue == []


async def test_clarify_closeout_becomes_the_answer(client, db, wire_env, monkeypatch):
    payload = (
        '{"action":"clarify","use_candidates":false,"clarification":"%s"}' % ASK
    )
    wire = Wire(wire_sse_text(_fence(payload)))
    attach_wire(wire, monkeypatch)
    events = await run_stream(client, message="帮我看一下资料", agent_mode="react")
    done = _done(events)
    assert done["kind"] == "clarify"
    assert done["refs"] == []
    assert ASK in _answer_text(events)
    assert wire.call_kinds() == ["decision"], wire.call_kinds()
    await db.rollback()
    from app.models import Chat

    chat = await db.get(Chat, done["chat_id"])
    assert int(chat.clarify_rounds or 0) == 1
    assert wire.queue == []


async def test_invalid_closeout_notes_and_clears_when_empty_handed(
    client, db, wire_env, monkeypatch
):
    """E 路 + external 路由：note 可见、不落澄清；私有候选不进直答。"""
    await _install_candidate(db, monkeypatch)
    wire = Wire(wire_sse_text(_fence("{bad")), wire_sse_text("照候选回答。"))
    attach_wire(wire, monkeypatch)
    events = await run_stream(client, message="帮我看一下资料", agent_mode="react")
    done = _done(events)
    assert done["kind"] == "direct"
    assert done["kind"] != "clarify"
    assert any(step.get("title") == "结束决策无效" for step in _notes(events))
    answer = wire.answer_bodies[-1]
    assert CANDIDATE not in wire.last_user_text(answer)
    assert wire.queue == []


async def test_use_candidates_false_drops_candidate_text(client, db, wire_env, monkeypatch):
    await _install_candidate(db, monkeypatch)
    payload = '{"action":"answer","use_candidates":false,"clarification":""}'
    wire = Wire(wire_sse_text(_fence(payload)), wire_sse_text("不用候选。"))
    attach_wire(wire, monkeypatch)
    events = await run_stream(client, message="帮我看一下资料", agent_mode="react")
    assert RERANK_EMPTY_NOTICE not in _answer_text(events)
    answer = wire.answer_bodies[-1]
    assert CANDIDATE not in wire.last_user_text(answer)
    assert wire.queue == []


async def test_plan_execute_never_clarifies(client, db, wire_env, monkeypatch):
    plan = '{"steps":[{"goal":"直接整理","tool":null,"args":{}}]}'
    wire = Wire(wire_sse_text(plan), wire_sse_text("计划后的回答。"))
    attach_wire(wire, monkeypatch)
    events = await run_stream(client, message="好", agent_mode="plan_execute")
    done = _done(events)
    assert done["kind"] != "clarify"
    assert done.get("agent_trace", {}).get("mode") == "plan_execute"
    assert ASK not in _answer_text(events)
    assert wire.queue == []


# ==========================================================================
# ②③ 的 wire 级补强（A10 / A10b / A10c / A11 / A12 / A13 / A14 / A21）
# ==========================================================================
#
# 本段共用的三条装配纪律：
#   1. **D21**：每条核心断言至少有一条 **wire 级**判据（`Wire.answer_bodies` +
#      `last_user_text()`），不拿 `plan.*` 这类内部字段当唯一证据；
#   2. `wire_env` 把分类器钉成 `external`，而 `source_map.resolve_kind` 的**第一行**就是
#      「external → direct」（与有没有材料**无关**）⇒ 凡要「库内命中 / 候选」语义的用例
#      自己再钉 `internal_db`（见 `_pin_internal_db`）；
#   3. 每条用例末尾 `assert wire.queue == []`（少调一次模型不会被 `Wire.handler` 发现）。


def _fence_after(prose: str, payload: str) -> str:
    """``_fence`` 的同形变体：块**前**的正文可换（A14 要在正文里写与载荷相反的话）。"""
    return f"{prose}\n<<<CLOSEOUT>>>\n{payload}\n<<<END>>>\n"


def _payload(action: str = "answer", use: bool = True, clarification: str = "") -> str:
    return json.dumps(
        {"action": action, "use_candidates": use, "clarification": clarification},
        ensure_ascii=False,
    )


def _pin_internal_db(monkeypatch) -> None:
    """把路由分类器**再钉一次**成 ``internal_db``（理由见本段第 2 条装配纪律）。"""
    from tests.api.test_chat_contract import _StubClassifier

    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )


def _capture_plan(monkeypatch) -> list:
    """把 ``persist_turn`` 换成「先存 plan 再调原函数」的 spy（plan 级判据的取数口）。

    ⚠️ 必须**转发**给原函数：不转发就换掉了被测行为（落库 / 事件 / 用量都不再发生）。
    """
    captured: list = []
    real = chat_routes.persist_turn

    async def spy(*args, **kwargs):
        captured.append(args[1] if len(args) > 1 else kwargs.get("plan"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(chat_routes, "persist_turn", spy)
    return captured


def _material(seq: int, content: str, *, filename: str = "材料.md") -> Material:
    """函数级用例的合成材料（``chunk_key`` 由 document_id + block_ordinal 决定 ⇒ 逐条不同）。"""
    return Material(
        chunk_id=seq,
        document_id=seq,
        content=content,
        filename=filename,
        block_ordinal=seq,
    )


def _plan(**overrides) -> SimpleNamespace:
    """驱动 ``chat_routes._apply_agent_closeout`` 的最小 plan 替身。"""
    base = dict(
        materials=[],
        materials_text="",
        all_refs=[],
        refs=[],
        kind="normal",
        answer_source="kb",
        source_type="internal_db",
        retrieval_id="rl-t1",
        preset_answer="",
        needs_model=True,
        attempt_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _turn(materials, *, closeout=None, invalid: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        materials=list(materials), closeout=closeout, closeout_invalid=invalid
    )


# --------------------------------------------------------------------------
# A10 · 澄清正文逐字（+ 反例：clarification 为空 ⇒ 不算澄清）
# --------------------------------------------------------------------------


async def test_a10_clarify_closeout_answer_is_verbatim(client, db, wire_env, monkeypatch):
    """A10：澄清轮的最终正文**逐字**等于 ``clarification``，且 wire 级**零 answer 调用**。

    两条都是「硬」的：①「正文里含追问文本」的子串断言挡不住**追加尾巴**的实现
    （`preset_answer + 一句废话` 照样绿）；② 少了 `kind="clarify"` 的短路，收尾回答那一腿
    会**多花一次模型调用**（`call_kinds()` 里会多出 `answer`）。
    """
    wire = Wire(wire_sse_text(_fence(_payload("clarify", False, ASK))))
    attach_wire(wire, monkeypatch)

    events = await run_stream(client, message="帮我看一下资料", agent_mode="react")
    done = _done(events)

    assert done["kind"] == "clarify"
    assert done["refs"] == []
    assert _answer_text(events) == ASK, _answer_text(events)
    # 协议块的「块前正文」不得混进回答（D-1 第 3 条：块前/块后的文本一律忽略）
    assert "说明" not in _answer_text(events)
    assert "<<<CLOSEOUT>>>" not in _answer_text(events)
    assert wire.call_kinds() == ["decision"], wire.call_kinds()
    assert wire.answer_bodies == []
    assert wire.queue == []


async def test_a10_empty_clarification_is_not_a_clarify(client, db, wire_env, monkeypatch):
    """A10 反例：``clarification`` 为空 ⇒ **不算澄清**（走回退 + 可见 note），候选仍在。

    「伪造澄清」与「静默吞掉」是这一腿要挡的两种坏实现：把空追问也落成 `kind="clarify"`
    （用户看到一条**空回答**且 `clarify_rounds` 被白烧一次），或不给任何 note（用户不知道
    模型没遵守协议）。
    """
    await _install_candidate(db, monkeypatch)
    _pin_internal_db(monkeypatch)
    wire = Wire(
        wire_sse_text(_fence(_payload("clarify", True, "   "))),
        wire_sse_text("照候选回答。"),
    )
    attach_wire(wire, monkeypatch)

    events = await run_stream(client, message="帮我看一下资料", agent_mode="react")
    done = _done(events)

    assert done["kind"] != "clarify"
    assert ASK not in _answer_text(events)
    assert [step["title"] for step in _notes(events)] == ["结束决策无效"]
    assert done["kind"] == "normal" and done["answer_source"] == "kb"
    assert CANDIDATE in wire.last_user_text(wire.answer_bodies[-1])
    assert wire.queue == []


# --------------------------------------------------------------------------
# A11 · use_candidates=False 不得反塞（清候选三处同步 + Agent 材料保留）
# --------------------------------------------------------------------------


async def test_a11_use_candidates_false_syncs_clear_across_plan_and_wire(
    client, db, wire_env, monkeypatch
):
    """A11（变异点②）：清候选后候选**不进**最终回答请求体，且三处**同步**清空。

   ⓪ 取具体期望值（改判，见文件头注释）；① wire 级；② `materials` / `materials_text` /
    `all_refs` 三者同步清空 —— 这正是 **D21 点名的最严重盲区**：只清 `plan.materials`
    而不同步清 `materials_text`，候选**照样**进请求体，而只读内部字段的断言全绿；
    ③ `refs == []`；⑤ 正文不含 `RERANK_EMPTY_NOTICE`。
    """
    await _install_candidate(db, monkeypatch)
    _pin_internal_db(monkeypatch)
    captured = _capture_plan(monkeypatch)
    wire = Wire(
        wire_sse_text(_fence(_payload("answer", False, ""))),
        wire_sse_text("不用候选。"),
    )
    attach_wire(wire, monkeypatch)

    events = await run_stream(client, message="帮我看一下资料", agent_mode="react")
    done = _done(events)

    assert len(captured) == 1, captured
    plan = captured[0]
    # ⓪ 具体期望值
    assert plan.kind == "direct", plan.kind
    assert plan.answer_source == "general", plan.answer_source
    assert done["kind"] == "direct"
    # ② 三者同步清空
    assert plan.materials == []
    assert plan.materials_text == ""
    assert plan.all_refs == []
    # ③
    assert done["refs"] == []

    # ① wire 级：**最终回答**请求体里没有候选正文（决策请求体里本来就有候选，不作判据）
    assert wire.answer_bodies, wire.call_kinds()
    for body in wire.answer_bodies:
        assert CANDIDATE not in wire.last_user_text(body)
    # 提示词侧也不得**因候选**说有资料（本构造下 tool_context 与 Agent 材料同时为空）
    system = wire.system_prompt_of(wire.answer_bodies[-1])
    assert answer_mod.NO_MATERIAL_SYSTEM_PROMPT in system, system
    assert "下面是本地知识库检索到的资料" not in system
    # ⑤
    assert RERANK_EMPTY_NOTICE not in _answer_text(events)
    assert wire.queue == []


async def test_a11_agent_materials_survive_the_candidate_clear():
    """A11④（函数级）：清的是**候选**；助手自己检到的材料必须**保留**。

    坏实现：清候选时把 `plan.materials` 整个置空（「顺手一起清」）⇒ 这一腿红。

    写成 ``async`` 只是让本文件模块级的 ``pytestmark = pytest.mark.asyncio`` 名实相符
    （本用例自身不 await 任何东西）。
    """
    candidates = [_material(101, "候选正文甲"), _material(102, "候选正文乙")]
    agent_items = [_material(201, "助手正文甲"), _material(202, "助手正文乙")]
    plan = _plan(
        materials=list(candidates),
        materials_text=render_materials(number(list(candidates))),
        all_refs=[{"ordinal": 1}, {"ordinal": 2}],
    )

    chat_routes._apply_agent_closeout(
        plan,
        _turn(
            agent_items,
            closeout={"action": "answer", "use_candidates": False, "clarification": ""},
        ),
    )

    assert plan.materials == agent_items
    assert [item.ordinal for item in plan.materials] == [1, 2]
    assert "候选正文甲" not in plan.materials_text
    assert "助手正文甲" in plan.materials_text and "助手正文乙" in plan.materials_text
    assert [ref["ordinal"] for ref in plan.all_refs] == [1, 2]
    assert plan.kind == "normal"
    assert plan.answer_source == "kb"
    assert plan.source_type == "internal_db"


# --------------------------------------------------------------------------
# A12 · use_candidates=True ⇒ 采用候选且编号同源（D15 / I3）
# --------------------------------------------------------------------------


async def test_a12_no_tool_use_candidates_true_keeps_accepted_candidates(
    client, db, wire_env, monkeypatch
):
    """A12①：无工具调用 + 已接受库内候选 + true ⇒ 材料进回答，引用编号同源。"""
    await _install_candidate(db, monkeypatch)
    _pin_internal_db(monkeypatch)
    captured = _capture_plan(monkeypatch)
    wire = Wire(
        wire_sse_text(_fence(_payload("answer", True, ""))),
        wire_sse_text("依据 [1] 作答。"),
    )
    attach_wire(wire, monkeypatch)

    events = await run_stream(client, message="帮我看一下资料", agent_mode="react")
    done = _done(events)

    assert CANDIDATE in wire.last_user_text(wire.answer_bodies[-1])
    plan = captured[0]
    assert len(plan.materials) == 1
    assert plan.kind == "normal"
    assert plan.answer_source == "kb"
    assert done["kind"] == "normal"
    assert [ref["ordinal"] for ref in done["refs"]] == [1]
    assert done["refs"][0]["chunk_id"] == plan.materials[0].chunk_key
    assert wire.queue == []


async def test_a12_merged_snapshot_numbers_once():
    """A12②（函数级）：候选 2 条 + 助手材料 2 条 ⇒ 编号 1..4 **连续不重复**、`[1]` 只出现一次。

    坏实现 = 「候选追加进 Agent 快照、两组各从 `[1]` 起」⇒ `ordinal` 出现两个 `1`、
    `materials_text` 里 `[1]` 出现两次、模型看到两组同号引用（I3 断裂）。

    写法同 A11④：``async`` 只为匹配模块级 ``pytestmark``。
    """
    candidates = [_material(11, "候选正文甲"), _material(12, "候选正文乙")]
    agent_items = [_material(21, "助手正文甲"), _material(22, "助手正文乙")]
    plan = _plan(
        materials=list(candidates),
        materials_text=render_materials(number(list(candidates))),
    )

    chat_routes._apply_agent_closeout(
        plan,
        _turn(
            agent_items,
            closeout={"action": "answer", "use_candidates": True, "clarification": ""},
        ),
    )

    ordinals = [item.ordinal for item in plan.materials]
    assert ordinals == [1, 2, 3, 4], ordinals
    assert len(set(ordinals)) == 4
    assert plan.materials_text.count("[1]") == 1
    assert plan.materials_text.count("[4]") == 1
    assert "候选正文甲" in plan.materials_text and "助手正文乙" in plan.materials_text
    assert [ref["ordinal"] for ref in plan.all_refs] == [1, 2, 3, 4]
    assert plan.kind == "normal"
    assert plan.answer_source == "kb"
    assert plan.source_type == "internal_db"


# --------------------------------------------------------------------------
# A13 · 决策失败 ⇒ 回退经典路径 + **帧级**可见 note（变异点③）
# --------------------------------------------------------------------------


async def test_a13_invalid_closeout_keeps_accepted_candidates_with_one_frame_note(
    client, db, wire_env, monkeypatch
):
    """A13：块内类型错 + 无工具调用 ⇒ 采用已有库内候选，note 在 SSE 帧与trace同源。"""
    await _install_candidate(db, monkeypatch)
    _pin_internal_db(monkeypatch)
    captured = _capture_plan(monkeypatch)
    wire = Wire(
        wire_sse_text(_fence('{"action":"answer","use_candidates":"false","clarification":""}')),
        wire_sse_text("照候选回答。"),
    )
    attach_wire(wire, monkeypatch)

    events = await run_stream(client, message="帮我看一下资料", agent_mode="react")
    done = _done(events)

    plan = captured[0]
    assert len(plan.materials) == 1
    assert plan.kind == "normal"
    assert plan.answer_source == "kb"
    assert done["kind"] == "normal"
    assert len(done["refs"]) == 1
    assert done["kind"] != "clarify"
    assert RERANK_EMPTY_NOTICE not in _answer_text(events)

    notes = _notes(events)
    assert len(notes) == 1, notes
    trace_notes = [
        step for step in done["agent_trace"]["steps"] if step.get("kind") == "note"
    ]
    assert len(trace_notes) == 1, trace_notes
    assert notes[0]["title"] == "结束决策无效"
    assert (notes[0]["title"], notes[0]["detail"]) == (
        trace_notes[0]["title"],
        trace_notes[0]["detail"],
    )

    assert CANDIDATE in wire.last_user_text(wire.answer_bodies[-1])
    assert wire.queue == []


# --------------------------------------------------------------------------
# A21 / A10c · use_candidates 四态（两套期望值）
# --------------------------------------------------------------------------


_A21_STATES = (
    ("缺字段", '{"action":"answer","clarification":""}', True),
    ("类型错", '{"action":"answer","use_candidates":"false","clarification":""}', True),
    ("块内不可解析", "{bad", True),
    ("显式 false", '{"action":"answer","use_candidates":false,"clarification":""}', False),
)


@pytest.mark.parametrize(
    "label,payload,keeps",
    _A21_STATES,
    ids=[state[0] for state in _A21_STATES],
)
async def test_a21_use_candidates_field_states(
    client, db, wire_env, monkeypatch, label, payload, keeps
):
    """A21 / D-4：三条坏路径**同向回退**（采用候选 + **恰一条**可见 note），显式 `False` 才清。

    ⓐ 缺字段若被静默当成 `False` ⇒ 候选被清（用户看不到本该看到的资料）；
    ⓑ 坏路径若「采用候选但**不加** note」⇒ 用户与验收者都看不出模型没遵守协议
    （A10c 点名：只断「候选仍在」会**同绿**，故本用例对同一条腿**同时**断 note）；
    ⓒ 显式 `False` 这条腿反过来必须**零 note**（否则「明确说不用」被当成失败回退）。
    """
    await _install_candidate(db, monkeypatch)
    _pin_internal_db(monkeypatch)
    captured = _capture_plan(monkeypatch)
    wire = Wire(wire_sse_text(_fence(payload)), wire_sse_text("照候选回答。"))
    attach_wire(wire, monkeypatch)

    events = await run_stream(client, message="帮我看一下资料", agent_mode="react")
    done = _done(events)
    notes = _notes(events)
    plan = captured[0]
    answer_text = _answer_text(events)

    if keeps:
        assert CANDIDATE in wire.last_user_text(wire.answer_bodies[-1]), label
        assert len(plan.materials) == 1, label
        assert plan.kind == "normal", label
        assert plan.answer_source == "kb", label
        assert done["kind"] == "normal", label
        assert len(done["refs"]) == 1, label
        assert done["kind"] != "clarify", label
        assert len(notes) == 1, (label, notes)
        assert notes[0]["title"] == "结束决策无效", label
        assert RERANK_EMPTY_NOTICE not in answer_text, label
    else:
        assert CANDIDATE not in wire.last_user_text(wire.answer_bodies[-1]), label
        assert plan.materials == [], label
        assert plan.materials_text == "", label
        assert plan.all_refs == [], label
        assert plan.kind == "direct", label
        assert plan.answer_source == "general", label
        assert done["refs"] == [], label
        assert notes == [], label
    assert wire.queue == []


# --------------------------------------------------------------------------
# A14 · 载荷优先于正文（双向）
# --------------------------------------------------------------------------


async def test_a14_payload_clarify_beats_contradicting_prose(
    client, db, wire_env, monkeypatch
):
    """A14①：载荷 `clarify` + 正文写「资料已采用，可以作答了」⇒ **跟载荷**，与正文无关。

    坏实现 = 正则扫 UI 摘要那句文案（D12 明禁）⇒ 会把澄清轮落成一次正常作答
    （`kind` 变 normal、多花一次收尾回答调用）。
    """
    prose = "资料已采用，可以作答了。"
    wire = Wire(wire_sse_text(_fence_after(prose, _payload("clarify", False, ASK))))
    attach_wire(wire, monkeypatch)

    events = await run_stream(client, message="帮我看一下资料", agent_mode="react")
    done = _done(events)

    assert done["kind"] == "clarify"
    assert _answer_text(events) == ASK
    assert prose not in _answer_text(events)
    assert wire.call_kinds() == ["decision"], wire.call_kinds()
    assert wire.queue == []


async def test_a14_payload_answer_beats_contradicting_prose(
    client, db, wire_env, monkeypatch
):
    """A14②：answer/true + 散文「不采用」+ 无工具 ⇒ 按载荷采用已接受库内候选。"""
    await _install_candidate(db, monkeypatch)
    _pin_internal_db(monkeypatch)
    wire = Wire(
        wire_sse_text(_fence_after("我不采用任何资料。", _payload("answer", True, ""))),
        wire_sse_text("照资料回答。"),
    )
    attach_wire(wire, monkeypatch)

    events = await run_stream(client, message="帮我看一下资料", agent_mode="react")
    done = _done(events)

    assert done["kind"] == "normal" and done["answer_source"] == "kb"
    assert done["kind"] != "clarify"
    assert CANDIDATE in wire.last_user_text(wire.answer_bodies[-1])
    assert wire.queue == []


# ---------------------------------------------------------------------------
# 回归修复 · 坏协议块回退时不得把助手已检到的材料一起丢掉
# ---------------------------------------------------------------------------


def _agent_materials(*ids: int) -> list[Material]:
    return [
        Material(
            chunk_id=i,
            document_id=900 + i,
            content=f"助手材料{i}。",
            filename=f"助手{i}.md",
            block_ordinal=i,
        )
        for i in ids
    ]


def _bare_plan(materials: list | None = None, *, kind: str = "direct") -> SimpleNamespace:
    numbered = number(list(materials or []))
    return SimpleNamespace(
        materials=numbered,
        materials_text=render_materials(numbered) if numbered else "",
        all_refs=[],
        refs=[],
        retrieval_id="trace-bug058",
        kind=kind,
        answer_source="kb" if numbered else "general",
        source_type="internal_db" if numbered else "external",
        preset_answer="",
        needs_model=True,
        attempt_id="a-058",
    )


async def test_bug058_invalid_closeout_keeps_agent_materials_when_no_candidates():
    """**回归修复 回归钉**：坏协议块走 D-10 回退时，若经典路径**没有候选可采**，
    不得把助手已经检到的材料也丢掉。

    改前实测：`plan.materials` 归零、`kind` 停在 `direct` / `answer_source="general"`
    ⇒ 最终回答请求体里**一段资料都没有**（系统提示也会说「没有资料」），
    而那一轮工具确实检到了材料 —— 丢材料**静默**（不报错、note 还写着「已改用已检索到的资料」）。
    对照：同一条路径在「没有协议块」（D-3）与「协议块合法」时都会装上助手材料。
    """
    turn = SimpleNamespace(
        closeout=None, closeout_invalid=True, materials=number(_agent_materials(3, 4))
    )
    plan = _bare_plan()
    chat_routes._apply_agent_closeout(plan, turn)

    assert plan.materials, "坏块回退把助手材料丢了"
    assert (plan.kind, plan.answer_source) == ("normal", "kb"), (plan.kind, plan.answer_source)
    assert plan.materials_text, "装了材料但正文没同步（D21 的盲区）"
    assert len(plan.all_refs) == 2
    assert [m.ordinal for m in plan.materials] == [1, 2]


async def test_bug058_invalid_closeout_still_prefers_candidates():
    """对照腿（D-10 字面）：**有候选可采**时回退采候选，不被助手快照替换。"""
    turn = SimpleNamespace(
        closeout=None, closeout_invalid=True, materials=number(_agent_materials(3, 4))
    )
    plan = _bare_plan(_agent_materials(1, 2), kind="normal")
    chat_routes._apply_agent_closeout(plan, turn)

    assert [m.chunk_id for m in plan.materials] == [1, 2], "D-10 回退必须采候选"
    assert plan.kind == "normal" and plan.answer_source == "kb"
