"""OPT4.3：在真实两种 Agent 链的三个边界停止，副作用/结果/审计可对账。"""

import asyncio

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.agent import runner
from app.agent.registry import ToolSpec
from app.agent.runner import AgentTurn
from app.agent.types import ToolObservation
from app.core.config import settings
from app.models import AgentRun, AgentStep, Chat, SessionEvent
from app.session import events
from tests.agent.test_runner import RecordingModel, collect, faux_model, plan_json, registry_with, says, tool_call


class ProbeArgs(BaseModel):
    value: str


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(settings, "WEB_ENABLE", False)
    monkeypatch.setattr(settings, "CHAT_EVENT_LOG_ENABLE", True)
    monkeypatch.setattr(settings, "AGENT_ENABLE_AUDIT", True)
    snapshot = events.failure_snapshot()
    yield
    events.restore_event_write_failures(snapshot)


async def _run(db, mode, handler, *, spaced_id=False):
    chat = Chat(title="OPT4 stopped tool")
    db.add(chat)
    await db.commit()
    chat_id = int(chat.id)
    spec = ToolSpec(
        name="opt4_probe", description="离线副作用计数",
        parameters_schema=ProbeArgs.model_json_schema(), parameters=ProbeArgs,
        handler=handler, needs_confirm=False,
    )
    turn = AgentTurn(mode=mode)
    model = (
        RecordingModel([plan_json({"tool": spec.name, "args": {"value": "done"}, "goal": "执行"})])
        if mode == "plan_execute" else faux_model([tool_call(spec.name, {"value": "done"}), says("完成")])
    )
    if spaced_id:
        from cubeloop.providers import faux_assistant_message, faux_tool_call
        model = faux_model([
            faux_assistant_message([faux_tool_call(spec.name, {"value": "done"}, id=" opt4-call ")]),
            says("完成"),
        ])
    task = asyncio.create_task(collect(
        turn, question="执行一次离线探针并结束", model=model, db=db,
        registry=registry_with(spec), chat_id=chat_id, turn_no=1, trace_id="opt4-stop",
    ))
    return task, turn, chat_id


async def _records(db, chat_id):
    await db.commit()
    run = (await db.execute(select(AgentRun).where(AgentRun.chat_id == chat_id))).scalar_one()
    logs = list((await db.execute(
        select(SessionEvent).where(SessionEvent.chat_id == chat_id).order_by(SessionEvent.seq)
    )).scalars())
    audits = list((await db.execute(select(AgentStep).where(AgentStep.run_id == run.id))).scalars())
    return run, [row for row in logs if row.type == "tool/call"], [
        row for row in logs if row.type == "tool/result"
    ], audits


@pytest.mark.parametrize("mode", ["react", "plan_execute"])
@pytest.mark.parametrize("boundary", ["before_effect", "after_effect_before_emit", "result_persist"])
@pytest.mark.asyncio
async def test_stop_keeps_actual_tool_truth_and_reports_missing_events(db, monkeypatch, mode, boundary):
    reached, release = asyncio.Event(), asyncio.Event()
    effects = []
    original_absorb = runner._absorb_tool
    original_append = events.append_event
    paused = False

    async def handler(args):
        if boundary == "before_effect":
            reached.set()
            await release.wait()
        effects.append(args.value)
        return ToolObservation(text="副作用已完成一次", ok=True)

    async def absorb(**kwargs):
        nonlocal paused
        result = await original_absorb(**kwargs)
        if boundary == "after_effect_before_emit" and not paused:
            paused = True
            reached.set()
            await release.wait()
        return result

    async def append(db_arg, chat_id, event_type, data, **kwargs):
        nonlocal paused
        if boundary == "result_persist" and event_type == "tool/result" and not paused:
            paused = True
            reached.set()
            await release.wait()
        return await original_append(db_arg, chat_id, event_type, data, **kwargs)

    monkeypatch.setattr(runner, "_absorb_tool", absorb)
    monkeypatch.setattr(events, "append_event", append)
    task, turn, chat_id = await _run(db, mode, handler)
    try:
        await asyncio.wait_for(reached.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert effects == ([] if boundary == "before_effect" else ["done"]), "停止不能重试已发生的副作用"
    run, calls, results, audits = await _records(db, chat_id)
    assert run.status == "stopped" and run.finished_at is not None
    assert len(calls) == 1, "工具已经进入执行侧，必须能审计到这次调用"
    assert len(results) <= 1
    assert all(row.tool_call_id == calls[0].tool_call_id for row in results)
    if boundary == "before_effect":
        assert not results and not audits
    else:
        assert len(audits) == 1, "已经成功完成的工具不能在停止时丢失审计"
        assert audits[0].tool_call_id == calls[0].tool_call_id
        assert audits[0].meta["ok"] is True
        assert all(row.data["ok"] is True and "副作用已完成一次" in row.data["content"] for row in results)
    if len(results) != len(calls):
        assert turn.events_incomplete is True, "缺失结果不能报告为日志完整"
        assert turn.events_incomplete_reason == "cancelled"


@pytest.mark.parametrize("mode", ["react", "plan_execute"])
@pytest.mark.asyncio
async def test_normal_tool_control_has_one_paired_result_and_successful_audit(db, mode):
    effects = []

    async def handler(args):
        effects.append(args.value)
        return ToolObservation(text="副作用已完成一次", ok=True)

    task, turn, chat_id = await _run(db, mode, handler)
    await asyncio.wait_for(task, timeout=5)
    run, calls, results, audits = await _records(db, chat_id)
    assert effects == ["done"] and run.status == "done"
    assert len(calls) == len(results) == len(audits) == 1
    assert calls[0].tool_call_id == results[0].tool_call_id == audits[0].tool_call_id
    assert results[0].data["ok"] is True and audits[0].meta["ok"] is True
    assert not turn.events_incomplete


@pytest.mark.asyncio
async def test_completed_spaced_id_does_not_mark_events_missing(db):
    effects = []

    async def handler(args):
        effects.append(args.value)
        return ToolObservation(text="完成", ok=True)

    task, turn, chat_id = await _run(db, "react", handler, spaced_id=True)
    await asyncio.wait_for(task, timeout=5)
    _, calls, results, audits = await _records(db, chat_id)
    assert effects == ["done"] and len(calls) == len(results) == len(audits) == 1
    assert calls[0].tool_call_id == results[0].tool_call_id == audits[0].tool_call_id == "opt4-call"
    assert not turn.events_incomplete
