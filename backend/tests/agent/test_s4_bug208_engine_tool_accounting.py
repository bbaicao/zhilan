"""S4 · 回归修复 判据载体：引擎自执行工具（load_tools）的**成功腿**也要补账。

## 规格（交接件 §2 · 208）

「load_tools」这类由引擎自执行的工具不经过模型的 tool_call 通道：
事件里根本没有 tool_execution_start，于是运行器的 collector 没有该 id 的任何记录。
- 当且仅当 event.result 非空**且** collector.rendered 里没有该 id 时：
  step_detail / tool_result.content 用 **event.result 的文本**；
- 落库 content 同源；
- 反向保护：is_error 腿文案**逐字不变**（工具不可用 / 参数校验失败）；
  collector.rendered 已有值时必须优先用它（不得被事件文本顶掉）；
  result 为空时仍回落 NO_HIT_TEXT。

## 缺陷面

_steps_for 的观察文本 = rendered or observation.text or NO_HIT_TEXT；_write_tool_events
的 content = clip(rendered.get(id, ""), STEP_CLIP) ⇒ 成功腿两处都落到「没有检索到相关资料。」
或空串：用户与模型看到的是「什么都没发生」，而引擎明明返回了内容。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agent import runner
from app.agent.runner import AgentTurn, ToolCollector
from app.models import Chat, SessionEvent
from tests.agent.test_runner import material
from app.agent.types import ToolObservation

ENGINE_TEXT = "引擎自执行工具 load_tools 的返回：可选工具甲、工具乙。"
FALLBACK_ID = "s4-208-fallback"


def _end_event(
    call_id: str,
    *,
    result_text: str = "",
    is_error: bool = False,
    tool_name: str = "load_tools",
) -> SimpleNamespace:
    content = [SimpleNamespace(type="text", text=result_text)] if result_text else []
    return SimpleNamespace(
        type="tool_execution_end",
        tool_call_id=call_id,
        tool_name=tool_name,
        is_error=is_error,
        result=SimpleNamespace(content=content),
        block_reason="",
    )


def _observation_step(event, collector: ToolCollector, turn: AgentTurn | None = None) -> dict:
    turn = turn if turn is not None else AgentTurn(mode="react")
    steps = runner._steps_for(
        event, turn=turn, collector=collector, started_at={}, fallback_id=FALLBACK_ID
    )
    observations = [s for s in steps if s["kind"] == "observation"]
    assert len(observations) == 1, "本件只喂一条 end 事件"
    return observations[0]


async def _only_result_payload(db, chat_id: int) -> dict:
    rows = await db.execute(
        select(SessionEvent)
        .where(SessionEvent.chat_id == chat_id, SessionEvent.type == "tool/result")
        .order_by(SessionEvent.seq)
    )
    events = list(rows.scalars().all())
    assert len(events) == 1, "本腿只落一条 tool/result"
    return dict(events[0].data or {})


async def _new_chat(db) -> int:
    chat = Chat(title="S4-208 载体")
    db.add(chat)
    await db.flush()
    return int(chat.id)


# ── 正向 discriminator ───────────────────────────────────────────────────────

def test_bug208_engine_tool_success_result_fills_observation_step():
    event = _end_event("s4-208-call", result_text=ENGINE_TEXT)

    obs = _observation_step(event, ToolCollector())

    assert obs["ok"] is True
    assert obs["detail"] == ENGINE_TEXT, "引擎自执行工具的成功结果必须进步骤帧：" + repr(obs["detail"])
    assert obs["detail"] != runner.NO_HIT_TEXT


@pytest.mark.asyncio
async def test_bug208_engine_tool_success_result_fills_tool_result_event(db):
    chat_id = await _new_chat(db)
    event = _end_event("s4-208-db", result_text=ENGINE_TEXT)

    await runner._write_tool_events(
        [event],
        db=db,
        chat_id=chat_id,
        turn_no=1,
        round_no=1,
        trace_id=None,
        collector=ToolCollector(),
        fallback_id=FALLBACK_ID,
    )

    payload = await _only_result_payload(db, chat_id)
    assert payload["ok"] is True
    assert payload["content"] == ENGINE_TEXT, "tool/result 的 content 必须同源：" + repr(payload["content"])
    assert payload["chars"] == len(ENGINE_TEXT)


# ── 反向保护腿：既有口径不得破 ───────────────────────────────────────────────

def test_bug208_reverse_rendered_text_still_wins():
    collector = ToolCollector()
    collector.absorb(
        "s4-208-rendered", ToolObservation(text="观察原文", materials=[material(1)])
    )
    event = _end_event(
        "s4-208-rendered", result_text="事件里那份不该赢", tool_name="knowledge_search"
    )

    obs = _observation_step(event, collector)

    assert obs["detail"] == collector.rendered["s4-208-rendered"]
    assert "不该赢" not in obs["detail"], "rendered 已有值时不得被事件文本顶掉"


def test_bug208_reverse_empty_result_still_falls_back_to_no_hit():
    event = _end_event("s4-208-empty", result_text="")

    obs = _observation_step(event, ToolCollector())

    assert obs["detail"] == runner.NO_HIT_TEXT, "result 为空时仍回落既有兜底文案"


@pytest.mark.asyncio
async def test_bug208_reverse_error_leg_text_is_unchanged(db):
    """is_error 腿：补救文案逐字不变（工具不可用 / 参数校验失败）。"""
    tool_missing = _end_event(
        "s4-208-unknown",
        result_text="Tool ghost_tool not found",
        is_error=True,
        tool_name="ghost_tool",
    )
    bad_args = _end_event(
        "s4-208-badargs",
        result_text="Input should be a valid integer",
        is_error=True,
        tool_name="knowledge_search",
    )
    collector = ToolCollector()

    await runner._absorb_immediate_failure(
        tool_missing, collector=collector, spec_map={}, fallback_id=FALLBACK_ID, args={}
    )
    await runner._absorb_immediate_failure(
        bad_args, collector=collector, spec_map={}, fallback_id=FALLBACK_ID, args={}
    )

    expected_missing = "ghost_tool 工具不可用：Tool ghost_tool not found"
    expected_badargs = "knowledge_search 参数校验失败：Input should be a valid integer"
    assert collector.rendered["s4-208-unknown"] == expected_missing
    assert collector.rendered["s4-208-badargs"] == expected_badargs
    assert _observation_step(tool_missing, collector)["detail"] == expected_missing
    assert _observation_step(bad_args, collector)["detail"] == expected_badargs

    chat_id = await _new_chat(db)
    await runner._write_tool_events(
        [tool_missing],
        db=db,
        chat_id=chat_id,
        turn_no=1,
        round_no=1,
        trace_id=None,
        collector=collector,
        fallback_id=FALLBACK_ID,
    )
    payload = await _only_result_payload(db, chat_id)
    assert payload["ok"] is False
    assert payload["content"] == expected_missing, "失败腿的落库文案也必须逐字不变"
