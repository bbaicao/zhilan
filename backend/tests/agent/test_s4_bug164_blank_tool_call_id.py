"""S4 · 回归修复 判据载体：同一轮两次**空 tool_call_id** 的结果必须各自成套。

## 规格（交接件 §2 · 164）

厂商流式增量里 tool_call_id 可以是空串；运行器对空 id 用兜底 id，但兜底 id 是
**一轮一个**（runner.py:2072 fallback_id = new_tool_call_id() 每次调用只铸一枚）
⇒ 同一轮里两次空 id 调用**共用同一个键**。实测（探针
_s4-scratch/carrier/probe_164_blank_tool_call_id.py，HEAD 字节）：

    step tool        | id= '4ace9a508691'
    step observation | id= '4ace9a508691' | detail= '[2] doc2.md ...'
    step tool        | id= '4ace9a508691'
    step observation | id= '4ace9a508691' | detail= '[2] doc2.md ...'   ← 后写顶掉先写

⇒ 两条步骤帧 id 相同（I4 的对账键失效）、先调用者的观察结果被顶掉、
落库的两条 tool/call 与两条 tool/result 共用 id（事件账无法与步骤对账）。

本条判据：事件 1:1（tool/call 与 tool/result 各 2 条、id 各自不重复且一一对应）、
两条步骤帧 id 不重复、两次结果各自成套（各自的观察都在、先调用者的结果没被顶掉、
先调用者的材料仍参与最终回答）。

## 判别构造（关键坑，别再踩）

faux_tool_call 的 id 参数实现是 'id or _random_id("tool")' ⇒ 传 "" / None **都拿不到
空 id**（空串 falsy ⇒ 回落随机 id，腿退化成「两条随机 id 的正常调用」而**假绿** ——
本件初版就踩过）。必须直接构造 ToolCall(id="", ...) 再交给 faux_assistant_message
（一条助手消息里两个块 = 同轮两次空 id 调用）。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.agent.registry import ToolSpec
from app.agent.runner import AgentTurn
from app.agent.tools import knowledge_search
from app.agent.types import ToolObservation
from app.models import Chat, SessionEvent
from cubeloop.providers import faux_assistant_message
from cubeloop.providers.base import ToolCall
from tests.agent.test_runner import (
    CollectingRetrieval,
    collect,
    faux_model,
    material,
    registry_with,
    says,
)


def _two_blank_id_calls():
    """一条助手消息里两个**空 tool_call_id** 的调用（同轮并行形态）。"""
    return faux_assistant_message(
        [
            ToolCall(id="", name="knowledge_search", arguments={"query": "甲问"}),
            ToolCall(id="", name="knowledge_search", arguments={"query": "乙问"}),
        ]
    )


async def _events(db, chat_id: int, event_type: str) -> list[SessionEvent]:
    rows = await db.execute(
        select(SessionEvent)
        .where(SessionEvent.chat_id == chat_id, SessionEvent.type == event_type)
        .order_by(SessionEvent.seq)
    )
    return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_bug164_two_blank_ids_are_kept_apart(db):
    chat = Chat(title="S4-164 空 id")
    db.add(chat)
    await db.flush()
    chat_id = int(chat.id)

    retrieval = CollectingRetrieval([[material(1)], [material(2)]])
    spec = knowledge_search.build_spec(db=object(), retrieve=retrieval)
    model = faux_model([_two_blank_id_calls(), says("够了")])
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

    assert len(retrieval.calls) == 2, "前提：两次调用都要真的执行"

    # ① 对账键：两条工具步骤帧的 id 必须都非空且互不重复
    tool_steps = [s for s in turn.steps if s["kind"] == "tool"]
    assert len(tool_steps) == 2, "前提：两条空 id 调用都要进步骤帧"
    assert all(str(s["tool_call_id"] or "").strip() for s in tool_steps), "步骤帧 id 不得为空"
    assert len({s["tool_call_id"] for s in tool_steps}) == 2, (
        "同轮两条空 id 调用共用了同一枚兜底 id：步骤帧无法与事件 1:1 对账"
    )

    # ② 两次结果各自成套：两条观察帧 id 不同、内容各自成套，先调用者没被顶掉
    obs_steps = [s for s in turn.steps if s["kind"] == "observation"]
    assert len(obs_steps) == 2, "前提：两条调用各有一条观察帧"
    assert len({s["tool_call_id"] for s in obs_steps}) == 2, "两条观察帧共用一个 id"
    details = [str(s["detail"]) for s in obs_steps]
    assert len(set(details)) == 2, "两条观察帧文本相同（后写覆盖先写）：" + repr(details)
    assert any("doc1.md" in d for d in details), "先调用者（doc1.md）的结果不在任何观察帧里"
    assert any("doc2.md" in d for d in details)

    # ③ 先调用者的材料仍参与最终回答（快照两条都在）
    filenames = {m.filename for m in turn.materials}
    assert filenames == {"doc1.md", "doc2.md"}, "先调用者的检索结果没有进最终引用材料：" + repr(sorted(filenames))

    # ④ 事件 1:1：tool/call 与 tool/result 各 2 条，id 各自不重复且一一对应
    calls = await _events(db, chat_id, "tool/call")
    results = await _events(db, chat_id, "tool/result")
    assert len(calls) == 2 and len(results) == 2
    call_ids = [e.tool_call_id for e in calls]
    result_ids = [e.tool_call_id for e in results]
    assert all(call_ids) and all(result_ids), "事件 id 不得为空"
    assert len(set(call_ids)) == 2, "两条 tool/call 共用一个 id：" + repr(call_ids)
    assert len(set(result_ids)) == 2, "两条 tool/result 共用一个 id：" + repr(result_ids)
    assert sorted(call_ids) == sorted(result_ids), "call 与 result 不是一一对应"
    contents = [str((e.data or {}).get("content", "")) for e in results]
    assert all(contents), "tool/result 的 content 不得为空（兜底覆盖后先写者会丢）"
    assert len(set(contents)) == 2, "两条 tool/result 的 content 相同（结果被顶掉）：" + repr(contents)

def _channel_spec():
    """造一条**材料恒空**的替身工具：观察正文只由入参决定 ⇒ 通道正文可判别。

    为什么必须材料恒空：有材料时 ``absorb`` 分支 ① 会把观察文本渲成快照编号，并按
    ``D2′``（防双注入）**不写进** ``tool_context`` ⇒ 通道面恒空，测不到通道正文
    （既有那条腿用 knowledge_search 正是如此，故它在 M3 半修下仍全绿 = 判别力盲区）。

    两条构造前提：``parameters`` 必须给（CubeLoop 靠它校验入参并把模型实例交给 handler）、
    ``needs_confirm=False`` 必须**显式**写（``None`` 是「未声明」哨兵）。
    """

    class ChannelArgs(BaseModel):
        """替身工具入参（材料恒空，正文 = 入参推出）。"""

        query: str = ""

    seen: list[str] = []

    async def handler(args) -> ToolObservation:
        seen.append(str(args.query))
        return ToolObservation(text=f"通道正文-{args.query}", ok=True, materials=[])

    spec = ToolSpec(
        name="channel_probe",
        description="通道正文判据替身（材料恒空）",
        parameters_schema=ChannelArgs.model_json_schema(),
        handler=handler,
        dangerous=False,
        parameters=ChannelArgs,
        needs_web=False,
        needs_confirm=False,
    )
    return spec, seen


@pytest.mark.asyncio
async def test_bug164_both_blank_ids_reach_tool_context_channel(db):
    """通道正文腿：同轮两次空 id 调用的**正文**必须都在最终回答的工具通道里。

    钉的结构 = ``AgentTurn.tool_context``（``_finish`` 收口 →
    ``collector.tool_context_items()``）。为什么它代表「通道正文」：它是**最终回答请求体**
    用的那份工具上下文（cap/截断之后的收口结果），``generation/answer.py`` 与
    ``chat_routes.py`` 取的就是它；通道里没有某一次调用的条目 = 那次结果**没有进模型上下文**。
    它既不是 ``collector.rendered``（决策侧观察文本），也不是步骤帧 ``detail``。

    M3 半修形态（铸了 id 但 ``claim_blank_call`` 失效）：两次调用都回落**同一个**
    ``fallback_id`` ⇒ 通道键相同 ⇒ 后写顶先写，**只有后调用者的正文进通道**；而步骤帧 /
    落库事件 / ``turn.materials`` 三面因事件桥照旧改写 id 而各自成套 ⇒ 既有那条腿全绿。
    本条补的就是这个盲区（修复后两次调用各自认领自己的 id，两条正文都在）。
    """
    chat = Chat(title="S4-164 通道正文")
    db.add(chat)
    await db.flush()
    chat_id = int(chat.id)

    spec, seen = _channel_spec()
    model = faux_model(
        [
            faux_assistant_message(
                [
                    ToolCall(id="", name="channel_probe", arguments={"query": "FIRST-CHANNEL-A"}),
                    ToolCall(id="", name="channel_probe", arguments={"query": "SECOND-CHANNEL-B"}),
                ]
            ),
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

    assert seen == ["FIRST-CHANNEL-A", "SECOND-CHANNEL-B"], (
        "前提：两次空 id 调用都要真的执行（执行序 = 发起序）"
    )

    channel = list(turn.tool_context or [])
    ids = [str(item.tool_call_id or "").strip() for item in channel]
    texts = sorted(str(item.text) for item in channel)

    # ① 通道里必须**两条**、键互不重复：空 id 已被事件桥各铸一枚独立 id，认领侧必须认到各自的
    assert len(channel) == 2, (
        "通道只有 %d 条：两次空 id 调用共用了同一个通道键（后写顶先写）；实际=%r"
        % (len(channel), [str(item.text) for item in channel])
    )
    assert all(ids) and len(set(ids)) == 2, "通道条目的 tool_call_id 必须非空且互不重复：" + repr(ids)

    # ② 取值集合：两次调用的正文**都在**通道里（先调用者不得被顶掉）
    assert texts == sorted(["通道正文-FIRST-CHANNEL-A", "通道正文-SECOND-CHANNEL-B"]), (
        "通道正文丢调用者：期望两次调用的正文都在，实际=" + repr(texts)
    )
