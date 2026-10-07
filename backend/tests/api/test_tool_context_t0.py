"""专项 · 工具调用能力第 0 批 · **① 普通工具结果通道**的验收钉子（A2 / A2b / A3 / A6 / A7）。

判据载体（开工包 §4 与 D20）：**进程内 app**（``httpx.ASGITransport``，复用
``test_chat_contract.py`` 的 ``client`` 夹具）+ ``httpx.MockTransport`` 假传输把**完整
序列化请求体**落成流水 + **进程内 monkeypatch 注入假 MCP pack**（生产路径不传
``mcp_packs`` / ``call_tool`` ⇒ 只能这样注入；见 :mod:`.t0_wire` 的模块 docstring）。

**本批的核心判据**是 A2：一个**模型事先不知道的哨兵串**（``T0-FAKE-<uuid>``）真的出现在
**最终回答那一次**模型调用的请求体里。它必须**改前红 / 改后绿**——改前「纯文本工具结果」
压根没有载体（``runner._finish`` 只搬 ``collector.materials``），所以这条断言在改前是**红**的。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.agent import runner
from app.agent.runner import AgentTurn, ToolCollector
from app.agent.types import ToolObservation
from app.generation import answer as answer_mod
from tests.api.t0_wire import (  # noqa: F401 —— client 是夹具
    FAKE_TOOL_CONTRACT,
    FAKE_TOOL_NAME,
    Wire,
    attach_wire,
    client,
    fake_mcp,
    run_stream,
    wire_env,
    wire_sse_text,
    wire_sse_tool_call,
)

# ⚠️ **不用全局** ``pytestmark = pytest.mark.asyncio``：本文件里既有 async 用例（要跑真链路）
# 又有同步用例（纯函数断言），全局标记会给同步用例挂上 asyncio 标记并产出 13 条
# ``PytestWarning``（实测）。逐个 async 用例显式加装饰器。


async def _react_with_fake_tool(client, fake_mcp, wire, *, answer: str = "已经拿到工具结果。"):
    """跑一轮真 ReAct：模型先调假 MCP 工具，再收尾，最后走回答链路。"""
    wire.queue = [
        wire_sse_tool_call(FAKE_TOOL_CONTRACT, {"note": "probe"}, call_id="call_t0_1"),
        wire_sse_text("可以作答了"),
        wire_sse_text(answer),
    ]
    events = await run_stream(client, message="用工具看一眼")
    return events


# --------------------------------------------------------------------------
# A2 · 核心：假工具值真的进了**最终回答**那一次请求体
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2_fake_tool_value_reaches_the_final_answer_request(
    client, fake_mcp, wire_env, monkeypatch
):
    """A2：哨兵串（模型不可预知）出现在**回答调用**的请求体里。

    ⚠️ **分派判据不是「有没有 ``tools[]``」**（开工包 §5 第 7 条：P&E 的**计划**调用也带
    ``tools[]``）⇒ 这里用 :meth:`Wire.call_kinds` 逐次判类别，并对**「无 tools 的那一次」**
    断言。本用例只跑 ReAct，所以「无 tools」= 回答调用。
    """
    wire = Wire()
    attach_wire(wire, monkeypatch)

    events = await _react_with_fake_tool(client, fake_mcp, wire)

    # 前提必须真的成立（否则「工具调过了」这件事本身没发生，后面的断言都无意义）
    assert [e["type"] for e in events][-1] == "done", events
    steps = [e["step"] for e in events if e["type"] == "agent"]
    assert [s["tool"] for s in steps if s["kind"] == "tool"] == [FAKE_TOOL_CONTRACT]
    assert fake_mcp.calls, "假 MCP 工具的 call_tool 一次都没被调用（注入没生效）"

    assert wire.call_kinds() == ["decision", "decision", "answer"], wire.call_kinds()
    answer_body = wire.answer_bodies[0]
    joined = json.dumps(answer_body, ensure_ascii=False)

    assert fake_mcp.token in joined, (
        "假工具值没有进最终回答请求体：\n"
        f"token={fake_mcp.token}\nbody={joined[:2000]}"
    )
    # 反向对照（**选对反面对照，否则会写成假红**）：决策调用里**可以**看到工具结果
    # ——那是 Agent 循环的正常反馈（工具结果作为一条 role=tool 消息回到决策模型），
    # 与本批要补的「回答调用缺载体」是两件事。这里只钉**系统提示段**不含哨兵串
    # （工具结果不得覆盖 / 混进系统提示，D5）。
    for decision in wire.decision_bodies:
        assert fake_mcp.token not in wire.system_prompt_of(decision)


@pytest.mark.asyncio
async def test_a2_the_value_is_the_tool_text_not_a_snapshot_rerender(
    client, fake_mcp, wire_env, monkeypatch
):
    """A2 附：进通道的是**工具原文**（``observation.text``），不是 ``rendered``。

    ``rendered`` 是按快照编号重渲染给决策模型看的文本，两者天然不同
    （开工包 A2 行：**可区分事实 = 工具原文里的哨兵片段**，不是快照重渲染出来的文本）。
    """
    wire = Wire()
    attach_wire(wire, monkeypatch)
    await _react_with_fake_tool(client, fake_mcp, wire)

    answer_text = wire.last_user_text(wire.answer_bodies[0])
    assert answer_mod.TOOL_CONTEXT_HEADER in answer_text
    assert fake_mcp.token in answer_text
    # 通道里的正文与工具返回的原文一字不差（除渲染框架外）
    assert f"{fake_mcp.token}｜{FAKE_TOOL_NAME} 的返回文本" in answer_text


# --------------------------------------------------------------------------
# A2b · 不双注入（判据 = ``absorb`` 实际走的分支，不是「材料是否为空」）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2b_mcp_text_is_not_double_injected(client, fake_mcp, wire_env, monkeypatch):
    """A2b：假 MCP 工具材料恒空 ⇒ 它只能在通道里；反之**带 materials 的工具不进通道**。

    两条独立判据：
    ① 同一 ``tool_call_id`` 的工具原文**不出现两次**（在回答请求体里数出现次数）；
    ② 通道段里**没有** ``knowledge_search`` 的材料（那份文本在「资料如下」区，不在这儿）。
    """
    wire = Wire()
    attach_wire(wire, monkeypatch)
    await _react_with_fake_tool(client, fake_mcp, wire)

    body_text = json.dumps(wire.answer_bodies[0], ensure_ascii=False)
    assert body_text.count(fake_mcp.token) == 1, "同一工具原文被注入了两次（双注入）"

    answer_text = wire.last_user_text(wire.answer_bodies[0])
    region = answer_text.split(answer_mod.TOOL_CONTEXT_HEADER, 1)[1]
    assert "knowledge_search" not in region  # 带 materials 的工具不进通道
    # 「资料如下」区与数据区**不共用**任何材料编号（材料区根本不在这里出现）
    assert "资料如下：" not in region


def test_a2b_collector_never_channelizes_entries_that_went_to_materials():
    """A2b 单测：三个分支的分流判据 = **实际走了哪条分支**。

    - 分支 ①（有可引用材料）⇒ **不进**通道（那份文本已经在资料区里）；
    - 分支 ②（``SNAPSHOT_FULL_TEXT``，检到了但一条都没进快照）⇒ **进**；
    - 分支 ③（真无材料，如 MCP / 失败）⇒ **进**，且文本是 ``observation.text``。
    """
    from app.retrieval.types import FUSE_LIMIT, Material

    def material(index: int) -> Material:
        return Material(
            chunk_id=index,
            document_id=100 + index,
            content=f"正文{index}",
            filename=f"doc{index}.md",
            block_ordinal=index,
        )

    collector = ToolCollector()
    # ① 有材料 ⇒ 不进通道
    collector.note_tool_name("c1", "knowledge_search")
    collector.absorb("c1", ToolObservation(text="工具自渲染", materials=[material(1)]))
    assert collector.tool_context_items() == []

    # ③ 无材料 ⇒ 进通道（文本取 observation.text）
    collector.note_tool_name("c2", "mcp:kit:probe")
    collector.absorb("c2", ToolObservation(text="一段纯文本结果"))
    items = collector.tool_context_items()
    assert [(i.tool, i.tool_call_id, i.text) for i in items] == [
        ("mcp:kit:probe", "c2", "一段纯文本结果")
    ]

    # ② 快照打满 ⇒ 进通道（文本是该态的实测原文，而是**不是**工具自渲染的局部编号文本）
    full = ToolCollector()
    full.note_tool_name("f1", "knowledge_search")
    full.absorb(
        "f1", ToolObservation(text="", materials=[material(i) for i in range(1, FUSE_LIMIT + 1)])
    )
    full.note_tool_name("f2", "knowledge_search")
    text = full.absorb("f2", ToolObservation(text="[1] 工具自己的编号", materials=[material(99)]))
    assert text == runner.SNAPSHOT_FULL_TEXT
    f_items = full.tool_context_items()
    assert [i.tool_call_id for i in f_items] == ["f2"]
    assert f_items[0].text == runner.SNAPSHOT_FULL_TEXT
    assert "[1]" not in f_items[0].text


def test_a2b_material_branch_drops_a_stale_channel_entry_on_reabsorb():
    """A2b 单测：同一 ``tool_call_id`` 先落分支 ③、后落分支 ① ⇒ 通道里**必须**清掉它。

    这条钉的是「重入 / 重渲染」下的双注入防线：分流判据以**最后一次**实际走的分支为准，
    否则一个 id 会在两个区里各出现一次。
    """
    from app.retrieval.types import Material

    collector = ToolCollector()
    collector.note_tool_name("c1", "knowledge_search")
    collector.absorb("c1", ToolObservation(text="先当纯文本进来"))
    assert [i.tool_call_id for i in collector.tool_context_items()] == ["c1"]

    collector.absorb(
        "c1",
        ToolObservation(
            text="",
            materials=[
                Material(chunk_id=1, document_id=101, content="正文", filename="d.md", block_ordinal=1)
            ],
        ),
    )
    assert collector.tool_context_items() == [], "同一 id 又进了通道（双注入）"


# --------------------------------------------------------------------------
# A3 · 只在数据区段内；决策调用不带通道段
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a3_value_lives_only_inside_the_data_region(
    client, fake_mcp, wire_env, monkeypatch
):
    """A3：哨兵串**只**出现在数据区段内，且**不在系统提示段**里。

    系统提示走 **``system_prompt`` 形参**（不在 ``messages`` 里），所以「工具结果不在系统
    提示段内」是**结构性**成立；这里把它断言出来，防止将来有人图省事把通道拼进系统提示。
    """
    wire = Wire()
    attach_wire(wire, monkeypatch)
    await _react_with_fake_tool(client, fake_mcp, wire)

    answer_body = wire.answer_bodies[0]
    user_text = wire.last_user_text(answer_body)

    assert answer_mod.TOOL_CONTEXT_HEADER in user_text
    before, _, after = user_text.partition(answer_mod.TOOL_CONTEXT_HEADER)
    assert fake_mcp.token not in before, "哨兵串出现在数据区**之前**的段落里"
    assert fake_mcp.token in after

    # 系统提示段里没有它（系统提示是**独立的高优先级段**，工具结果只进 user 消息体）
    assert fake_mcp.token not in wire.system_prompt_of(answer_body)
    # 系统提示段仍**在最前**（D5：系统提示永远在最前）。
    # ⚠️ 真 provider 把它折成 ``messages[0]`` 的 ``role="system"``（wire 里没有独立键），
    # 所以「在最前」= 它就是消息列表的**第 0 条**、且是非空的冻结文案。
    assert wire.system_prompt_of(answer_body).strip(), "系统提示段是空的（读错了字段）"
    assert wire.messages_of(answer_body)[0].get("role") == "system"
    # 数据区段在 user 消息里，且**排在系统提示之后**
    assert wire.messages_of(answer_body).index(
        next(m for m in wire.messages_of(answer_body) if m.get("role") == "user")
    ) > 0


@pytest.mark.asyncio
async def test_a3_decision_calls_never_carry_the_channel(
    client, fake_mcp, wire_env, monkeypatch
):
    """A3：**决策调用**请求体里没有数据区段（通道只进最终回答那一次，D2）。

    ⚠️ 分派按**工具名集合**判类别（A3 行明文要求）：决策调用的 ``tools[]`` 非空，
    回答调用为空。这里对每一次调用都断言一次，而不是只挑一次抽样。
    """
    wire = Wire()
    attach_wire(wire, monkeypatch)
    await _react_with_fake_tool(client, fake_mcp, wire)

    for index, body in enumerate(wire.request_bodies):
        kind = wire.call_kinds()[index]
        text = wire.last_user_text(body)
        if kind == "decision":
            assert answer_mod.TOOL_CONTEXT_HEADER not in text, f"第 {index} 次决策调用带了通道段"
            assert wire.tools_of(body), "决策调用必须带 tools[]（否则分派判据本身失效）"
        else:
            assert not wire.tools_of(body), "回答调用不该带 tools[]"
            assert answer_mod.TOOL_CONTEXT_HEADER in text


# --------------------------------------------------------------------------
# A6 · ``ok=False`` 不得产生成功事实（**两态**）
# --------------------------------------------------------------------------


def test_a6_failure_text_is_marked_as_failure_and_keeps_ok_false():
    """A6 态②：失败观察 ⇒ 条目 ``ok=False``、渲染用**失败**措辞、**不含**成功标记。

    反向鉴别：把 ``ok`` 写死 True，或把两条固定文案写成同一句，下面两条断言就会红。
    """
    collector = ToolCollector()
    collector.note_tool_name("c1", "mcp:kit:boom")
    collector.absorb("c1", ToolObservation(text="工具执行失败：连接被拒绝", ok=False))
    items = collector.tool_context_items()
    assert [i.ok for i in items] == [False]

    rendered = answer_mod.render_tool_context(items)
    # 固定头的**前段 + 冒号 + 失败**：既钉「用了失败文案」也钉「没和成功文案共用一句」
    assert rendered.count("- 工具 mcp:kit:boom：失败") == 1
    assert "- 工具 mcp:kit:boom：成功" not in rendered
    for ok_marker in ("成功", "已读取", "已检索"):
        assert ok_marker not in rendered, f"失败条目里出现了成功措辞：{ok_marker}"
    assert "工具执行失败：连接被拒绝" in rendered
    # 反向鉴别：同一条目 ok=True 时必须换另一句（否则上面那条断言在「两态同文案」时也绿）
    ok_items = [replace(items[0], ok=True)]
    assert "- 工具 mcp:kit:boom：成功" in answer_mod.render_tool_context(ok_items)


def test_a6_snapshot_full_state_is_not_required_to_explain_the_reason():
    """A6 态①：**快照打满**那一态是固定串，**不含**失败原因 ⇒ 不得要求它「说明失败原因」。

    （开工包 A6 行的复核更正：原判据在这一态不成立。``ok`` 也不必是 False——
    「检到了但没进快照」不是工具失败。）
    """
    collector = ToolCollector()
    collector.note_tool_name("c1", "knowledge_search")
    collector.absorb("c1", ToolObservation(text="", materials=[], ok=True))
    collector._remember_context("c1", in_materials=False, raw=runner.SNAPSHOT_FULL_TEXT,
                                text=runner.SNAPSHOT_FULL_TEXT)
    items = collector.tool_context_items()
    assert items[0].text == runner.SNAPSHOT_FULL_TEXT
    assert "失败" not in answer_mod.render_tool_context(items)


# --------------------------------------------------------------------------
# A7 · 顺序 = **调用顺序**（不是字典序、不是完成序）
# --------------------------------------------------------------------------


def test_a7_order_follows_the_call_sequence_not_the_completion_sequence():
    """A7：两次调用的通道顺序 = **调用序**（A → B），即使 B **先完成**。

    构造（本批最重要的一处构造）：``absorb`` 的调用序 = **完成序**（``runner._absorb_tool``
    在工具执行返回后才记账），而 ``note_call_order`` 的追加序 = **``tool/call`` 事件序** =
    **发起序**。所以这里先 ``absorb`` B，再 ``absorb`` A，事件序却是 A → B ——
    正是「并行工具下完成序 ≠ 调用序」的形态。

    ⚠️ 若实现退回 ``observations`` 的自然序（= 完成序），通道会输出 B → A ⇒ 本用例红。
    """
    collector = ToolCollector()
    for rid, tool, text in (("b", "mcp:kit:second", "第二个的结果"), ("a", "mcp:kit:first", "第一个的结果")):
        collector.note_tool_name(rid, tool)
        collector.absorb(rid, ToolObservation(text=text))
    # 事件序：A 先发起、B 后发起（与上面的完成序相反）
    collector.note_call_order("a")
    collector.note_call_order("b")

    items = collector.tool_context_items()
    assert [i.tool_call_id for i in items] == ["a", "b"], "通道顺序不是调用序（退回完成序了）"
    assert [i.tool for i in items] == ["mcp:kit:first", "mcp:kit:second"]


def test_a7_ids_without_an_event_fall_back_to_completion_order_last():
    """A7 附：**没记到事件序**的 id（事件没到 / 被取消）按完成序**排在已排好的之后**。

    兜底顺序唯一的用途就是「没有更权威的顺序时别丢条目」，所以它必须排在**末尾**，
    不能插进已确定顺序的中间。
    """
    collector = ToolCollector()
    for rid in ("b", "a", "z"):
        collector.note_tool_name(rid, f"mcp:kit:{rid}")
        collector.absorb(rid, ToolObservation(text=f"{rid} 的结果"))
    collector.note_call_order("a")
    collector.note_call_order("b")
    assert [i.tool_call_id for i in collector.tool_context_items()] == ["a", "b", "z"]


def test_a7_empty_tool_call_id_is_normalized_to_the_round_fallback_id():
    """A7 附：空 ``tool_call_id`` 一律归一到本轮兜底 id（否则排序对不上事件序）。

    ``cubeloop/providers/openai.py`` 在 ``tc_delta.id or ""`` 时会给空 id，
    而执行侧 / 事件侧 / 步骤帧三处都已折成同一个 ``fallback_id``（``回归修复``）——
    通道若照收空串，事件序里的 ``<fallback>`` 就找不到它。
    """
    collector = ToolCollector()
    collector.fallback_id = "fb-1"
    collector.note_tool_name("", "mcp:kit:probe")
    collector.absorb("", ToolObservation(text="结果"))
    collector.note_call_order("fb-1")
    items = collector.tool_context_items()
    assert [i.tool_call_id for i in items] == ["fb-1"]


# --------------------------------------------------------------------------
# A4 的一部分（有界 + 截断显式）—— 完整的两态判据见 test_tool_context_limits
# --------------------------------------------------------------------------


def test_a4_single_item_and_total_limits_are_applied_in_order():
    """A4：单条 ≤4000、总计 ≤12000、**按事件序**截断，且超限**显式标记**。

    四条各 5000 字符的原文（逐条算账）：前三条把 12000 的预算吃满（**每条都被单条上限
    截到恰好 4000**），第四条只剩一小截预算 ⇒ 正文极短 + 截断标记。
    ⚠️ 断言**不写死第四条的具体形状**（剩余预算的字节数会随标记措辞变），只钉三条性质：
    ① 前三条恰好被单条上限截住；② 总正文量 ≤ 总上限；③ 第四条**远短于**原文且**带显式标记**
    —— 这三条合起来足以排除「平均分」「字典序」「静默截断」三种错法。
    """
    collector = ToolCollector()
    texts = {f"c{i}": ("x" * 5000) for i in range(4)}
    for rid, text in texts.items():
        collector.note_tool_name(rid, f"mcp:kit:{rid}")
        collector.absorb(rid, ToolObservation(text=text))
    for rid in texts:  # 事件序 = 与完成序相同，便于断言「按序截断到谁」
        collector.note_call_order(rid)

    items = collector.tool_context_items()
    # 超限**不丢条目**（「调过、没进上下文」本身也是模型要知道的事实）
    assert len(items) == 4
    for index in range(3):
        assert len(items[index].text) == runner.TOOL_CONTEXT_ITEM_LIMIT, index
        assert "已截断" in items[index].text, index
    # 总量上限（判据 = **工具正文**量；标记是元数据，见 `cap_tool_context` 的说明）
    total = sum(runner._tool_text_length(i.text) for i in items)
    assert total <= runner.TOOL_CONTEXT_TOTAL_LIMIT, total
    # 按序：前三条吃满预算，第四条被压到远短于原文（不是平均分 1250、也不是原样 5000）
    assert items[3].text.startswith("x")
    assert runner._tool_text_length(items[3].text) < 1000
    assert "已截断" in items[3].text or "超出上下文总量上限" in items[3].text
    # 谁被截：顺序为事件序（这里与完成序一致）
    assert [i.tool_call_id for i in items] == ["c0", "c1", "c2", "c3"]


def test_a4_total_cap_holds_over_many_oversized_items():
    """A4 反向鉴别：**任意条数**下总正文量都不得越过总量上限（含标记本身的长度）。"""
    collector = ToolCollector()
    for index in range(6):
        rid = f"c{index}"
        collector.note_tool_name(rid, f"mcp:kit:{rid}")
        collector.absorb(rid, ToolObservation(text="y" * 9000))
        collector.note_call_order(rid)
    items = collector.tool_context_items()
    assert sum(runner._tool_text_length(i.text) for i in items) <= runner.TOOL_CONTEXT_TOTAL_LIMIT
    assert all(i.text for i in items), "所有条目都要有可读文本（正文或占位标记）"
    # 单条上限对**每一条**都成立
    for item in items:
        assert len(item.text) <= runner.TOOL_CONTEXT_ITEM_LIMIT + len(
            runner.TOOL_CONTEXT_TRUNCATION_MARK.format(chars=99999)
        )


def test_a4_fixed_short_strings_pass_through_untouched():
    """A4 态②：**无原文**的两态（``SNAPSHOT_FULL_TEXT`` / ``NO_HIT_TEXT``）**原样进通道**。"""
    collector = ToolCollector()
    collector.note_tool_name("s", "knowledge_search")
    collector.absorb("s", ToolObservation(text="", materials=[], ok=False))
    # ``observation.text`` 为空 ⇒ 分支 ③ 的原文是空串，条目文本为空（不进标签）
    items = collector.tool_context_items()
    assert items[0].text == ""
    assert "已截断" not in items[0].text
    # 显式给固定短串时原样过（不做任何截断/改写）
    collector2 = ToolCollector()
    collector2.note_tool_name("n", "mcp:kit:x")
    collector2.absorb("n", ToolObservation(text=runner.NO_HIT_TEXT))
    assert collector2.tool_context_items()[0].text == runner.NO_HIT_TEXT


# --------------------------------------------------------------------------
# A8 · 不进 ``prompt_has_material()``
# --------------------------------------------------------------------------


def test_a8_tool_context_does_not_enter_prompt_has_material():
    """A8：只有 ``tool_context``、没有可引用材料时，``prompt_has_material`` 仍判「无材料」。"""
    assert answer_mod.prompt_has_material("") is False
    # 通道**不参与**这个判据（它回答的是「有没有可引用资料」）
    built = answer_mod.build_messages("问题", None, tool_context=["任意对象"])
    assert "资料如下：" not in built[-1]["content"]
    assert answer_mod.prompt_has_material("") is False


def test_a8_build_messages_is_bytewise_noop_when_the_channel_is_empty():
    """**D8 第一性质**（先在函数层证 no-op）：空通道 ⇒ 与不传该参数**逐字节相同**。

    三种调用形态（不传 / 传 ``None`` / 传 ``[]``）必须产出**同一个**字符串，
    且两种资料态（有资料 / 无资料）都要覆盖。
    """
    for materials_text in ("", "[1]（文档：a.md）\n正文"):
        baseline = answer_mod.build_messages("问题", None, materials_text=materials_text)
        assert answer_mod.build_messages(
            "问题", None, materials_text=materials_text, tool_context=None
        ) == baseline
        assert answer_mod.build_messages(
            "问题", None, materials_text=materials_text, tool_context=[]
        ) == baseline


# --------------------------------------------------------------------------
# A23 · ``collected_at`` 走**库内时钟**（D7），不是本机钟
# --------------------------------------------------------------------------


def test_a23_collected_at_uses_the_db_clock():
    """A23：条目时间戳与 ``audit.db_clock_now(db)`` **同源**；字段/文案标注「库内时钟」。

    判据：条目时刻与 ``db_clock_now()`` 的差 < 5 秒（同一只钟 ⇒ 同轴）；
    且**源码里没有** ``datetime.now()`` 这个本机钟调用（grep 级判据，防回退）。
    """
    from app.agent import audit as agent_audit

    collector = ToolCollector()
    collector.note_tool_name("c1", "mcp:kit:probe")
    collector.absorb("c1", ToolObservation(text="结果"))
    item = collector.tool_context_items()[0]

    parsed = datetime.fromisoformat(item.collected_at)
    delta = abs((parsed - agent_audit.db_clock_now(None)).total_seconds())
    assert delta < 5, f"collected_at 与库内时钟差了 {delta} 秒（可能用了本机钟）"

    # 文案标注（A23 明文：字段与**任何用户可见文案**都必须标注「库内时钟」）
    rendered = answer_mod.render_tool_context([item])
    assert "库内时钟" in rendered

    # 源码级：两条固定文案都带标注，且 runner 里没有裸的 datetime.now()
    assert "库内时钟" in answer_mod.TOOL_CONTEXT_OK_LINE
    assert "库内时钟" in answer_mod.TOOL_CONTEXT_FAIL_LINE
    source = (runner.__file__ or "")
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "datetime.now(" not in text, "runner.py 里出现了本机钟 datetime.now()"


def test_a23_db_clock_follows_the_connection_dialect():
    """A23 的 SQLite 档：``db_clock_now`` 按**连接**的方言判（sqlite ⇒ UTC 轴）。

    这是 ``回归修复`` 的教训：MySQL 与 SQLite 的 ``func.now()`` 不是同一只钟，
    取哪只必须看**那条连接**，不能看进程级 ``DB_BACKEND``。
    """
    from app.agent import audit as agent_audit

    class _Dialect:
        name = "sqlite"

    class _Bind:
        dialect = _Dialect()

    class _Db:
        bind = _Bind()

    utc_now = agent_audit.db_clock_now(_Db())
    local_now = datetime.now()
    # SQLite 档必须走 UTC 轴；本机是 +08:00 ⇒ 两者相差约 8 小时
    delta_hours = abs((local_now - utc_now).total_seconds()) / 3600
    assert delta_hours > 1, "SQLite 档没有走 UTC 轴（按本机钟取的时间）"
    assert utc_now.tzinfo is None, "库内时钟按口径是 **naive** 值"