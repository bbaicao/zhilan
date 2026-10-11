"""Cancellation closeout uses real SQLite; only the generation source is controlled."""
import asyncio
import hashlib
import json

import pytest
from sqlalchemy import select

from app.api import chat_routes, chat_stop
from app.api.chat_schemas import ChatRequest
from app.models import AgentRun, Message
from app.session import events
from tests.api.test_bug153_stream_error_semantics import _install
from tests.api.test_opt2_answer_closeout import ReplyModel


def decode(frame):
    return json.loads(frame.decode().removeprefix("data: "))


@pytest.fixture
def controlled(monkeypatch):
    _install(monkeypatch, ReplyModel("unused"))
    chat_stop.reset()
    pending = asyncio.Event()
    closed = asyncio.Event()
    side_effects = []
    monkeypatch.setattr(chat_routes, "maybe_store_cache", lambda *a, **k: side_effects.append("cache"))
    monkeypatch.setattr(chat_routes.memory_mod, "schedule_extract", lambda **k: side_effects.append("memory"))

    def install(parts):
        async def stream(plan, model, db, **kwargs):
            try:
                for part in parts:
                    yield part
                pending.set()
                await asyncio.Event().wait()
            finally:
                closed.set()
        monkeypatch.setattr(chat_routes, "stream_tokens", stream)

    yield install, pending, closed, side_effects
    chat_stop.reset()


async def assert_saved(fresh_db, chat_id, frames):
    rows = list((await fresh_db.execute(
        select(Message).where(Message.chat_id == chat_id).order_by(Message.id)
    )).scalars())
    body = "".join(f["text"] for f in frames if f["type"] == "token")
    reasoning = "".join(f["text"] for f in frames if f["type"] == "reasoning")
    assert [m.role for m in rows] == (["user", "assistant"] if body.strip() or reasoning.strip() else ["user"])
    assert rows[0].content == "Explain cancellation closeout."
    if len(rows) == 2:
        message = rows[1]
        assert message.content == body
        assert message.kind == "stopped" and message.refs == [] and message.answer_source is None
        assert message.thinking["generation_status"] == "stopped"
        assert message.thinking["finish_reason"] == "aborted"
        assert (message.reasoning or "") == reasoning
    history = await events.list_events(fresh_db, chat_id)
    interrupted = [e for e in history if e.type == "stream/interrupted"]
    attempts = [e for e in history if e.type == "assistant/attempt"]
    ends = [e for e in history if e.type == "turn/end"]
    assert len(interrupted) == len(attempts) == len(ends) == 1
    assert interrupted[0].data["delivered_chars"] == len(body)
    assert interrupted[0].data["delivered_text_hash"] == "sha256:" + hashlib.sha256(body.encode()).hexdigest()[:16]
    assert ends[0].data["generation_status"] == "stopped"
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "close", "stop"])
@pytest.mark.parametrize("parts", [
    [],
    [("text", "first "), ("text", "second")],
    [("reasoning", "thinking only")],
    [("agent", {"tool": "knowledge_search"}), ("reasoning", "reason"), ("text", "answer")],
])
async def test_interruption_saves_only_delivered_parts(db, fresh_db, controlled, mode, parts):
    install, pending, closed, side_effects = controlled
    install(parts)
    response = await chat_routes.chat_stream(ChatRequest(
        message="Explain cancellation closeout.", request_id="s3-r1-test",
    ), db)
    frames = [decode(await anext(response.body_iterator))]
    for _ in parts:
        frames.append(decode(await anext(response.body_iterator)))
    if mode == "close":
        await response.body_iterator.aclose()
    else:
        task = asyncio.create_task(anext(response.body_iterator))
        await asyncio.wait_for(pending.wait(), 3)
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert chat_stop.request_stop("s3-r1-test")
            frames.append(decode(await task))
            assert frames[-1]["type"] == "stopped"
        await response.body_iterator.aclose()
    await assert_saved(fresh_db, frames[0]["chat_id"], frames)
    if parts or mode != "close":
        assert closed.is_set()
    assert not side_effects and not chat_stop.request_stop("s3-r1-test")
    assert not any(f["type"] in ("done", "error") for f in frames)
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_one_stopped_commit(db, fresh_db, controlled, monkeypatch):
    install, pending, closed, side_effects = controlled
    install([("text", "partial")])
    real_save = chat_routes.persist_stopped_turn
    saving, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def delayed(*args, **kwargs):
        calls.append(1)
        saving.set()
        await release.wait()
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(chat_routes, "persist_stopped_turn", delayed)
    response = await chat_routes.chat_stream(ChatRequest(
        message="Explain cancellation closeout.", request_id="s3-r1-repeat",
    ), db)
    frames = [decode(await anext(response.body_iterator)), decode(await anext(response.body_iterator))]
    task = asyncio.create_task(anext(response.body_iterator))
    await asyncio.wait_for(pending.wait(), 3)
    task.cancel()
    try:
        await asyncio.wait_for(saving.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        await response.body_iterator.aclose()
    assert isinstance(result, asyncio.CancelledError)
    assert calls == [1] and closed.is_set() and not side_effects
    await assert_saved(fresh_db, frames[0]["chat_id"], frames)


@pytest.mark.asyncio
async def test_cancel_during_confirmed_stop_commit_does_not_save_twice(db, fresh_db, controlled, monkeypatch):
    install, pending, closed, side_effects = controlled
    install([("text", "partial")])
    real_save = chat_routes.persist_stopped_turn
    saving, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def delayed(*args, **kwargs):
        calls.append(1)
        saving.set()
        await release.wait()
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(chat_routes, "persist_stopped_turn", delayed)
    response = await chat_routes.chat_stream(ChatRequest(
        message="Explain cancellation closeout.", request_id="s3-r1-stop-save",
    ), db)
    frames = [decode(await anext(response.body_iterator)), decode(await anext(response.body_iterator))]
    task = asyncio.create_task(anext(response.body_iterator))
    await asyncio.wait_for(pending.wait(), 3)
    assert chat_stop.request_stop("s3-r1-stop-save")
    await asyncio.wait_for(saving.wait(), 3)
    task.cancel()
    release.set()
    result = (await asyncio.gather(task, return_exceptions=True))[0]
    await response.body_iterator.aclose()
    assert isinstance(result, asyncio.CancelledError)
    assert calls == [1] and closed.is_set() and not side_effects
    await assert_saved(fresh_db, frames[0]["chat_id"], frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", ["receive", "send"])
async def test_asgi_response_disconnect_closes_before_session_exit(db, fresh_db, controlled, disconnect):
    from starlette.requests import ClientDisconnect

    install, pending, closed, side_effects = controlled
    install([("text", "partial")])
    response = await chat_routes.chat_stream(ChatRequest(
        message="Explain cancellation closeout.", request_id="s3-r1-asgi",
    ), db)
    frames = []
    delivered = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            frames.append(decode(message["body"]))
            if frames[-1]["type"] == "token":
                delivered.set()
                if disconnect == "send":
                    raise OSError("controlled ASGI send disconnect")

    async def receive():
        await delivered.wait()
        await pending.wait()
        return {"type": "http.disconnect"}

    scope = {"type": "http", "asgi": {"spec_version": "2.0" if disconnect == "receive" else "2.4"}}
    if disconnect == "send":
        with pytest.raises(ClientDisconnect):
            await asyncio.wait_for(response(scope, receive, send), 3)
    else:
        await asyncio.wait_for(response(scope, receive, send), 3)
    await assert_saved(fresh_db, frames[0]["chat_id"], frames)
    assert closed.is_set() and not side_effects and not chat_stop.request_stop("s3-r1-asgi")
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


@pytest.mark.asyncio
async def test_save_exception_keeps_stop_semantics_and_next_request_can_save(db, fresh_db, controlled, monkeypatch, caplog):
    install, pending, closed, side_effects = controlled
    install([("text", "partial")])
    real_save = chat_routes.persist_stopped_turn
    calls = []

    async def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("controlled stopped persistence failure")

    monkeypatch.setattr(chat_routes, "persist_stopped_turn", fail)
    response = await chat_routes.chat_stream(ChatRequest(
        message="Explain cancellation closeout.", request_id="s3-r1-failed-save",
    ), db)
    meta = decode(await anext(response.body_iterator))
    await anext(response.body_iterator)
    await response.body_iterator.aclose()
    assert calls == [1] and closed.is_set() and not side_effects
    assert "停止轮落库失败" in caplog.text and "RuntimeError" in caplog.text
    assert not list((await fresh_db.execute(select(Message).where(Message.chat_id == meta["chat_id"]))).scalars())
    assert not chat_stop.request_stop("s3-r1-failed-save")

    monkeypatch.setattr(chat_routes, "persist_stopped_turn", real_save)
    response = await chat_routes.chat_stream(ChatRequest(
        message="Explain cancellation closeout.", request_id="s3-r1-retry",
    ), db)
    frames = [decode(await anext(response.body_iterator)), decode(await anext(response.body_iterator))]
    await response.body_iterator.aclose()
    await assert_saved(fresh_db, frames[0]["chat_id"], frames)


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_generation_cleanup(db, fresh_db, controlled, monkeypatch):
    install, pending, closed, side_effects = controlled
    cleaning, release = asyncio.Event(), asyncio.Event()

    async def stream(*args, **kwargs):
        try:
            yield "text", "partial"
            pending.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            closed.set()

    monkeypatch.setattr(chat_routes, "stream_tokens", stream)
    response = await chat_routes.chat_stream(ChatRequest(
        message="Explain cancellation closeout.", request_id="s3-r1-cleanup",
    ), db)
    frames = [decode(await anext(response.body_iterator)), decode(await anext(response.body_iterator))]
    task = asyncio.create_task(anext(response.body_iterator))
    await asyncio.wait_for(pending.wait(), 3)
    task.cancel()
    try:
        await asyncio.wait_for(cleaning.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        await response.body_iterator.aclose()
    assert isinstance(result, asyncio.CancelledError) and closed.is_set() and not side_effects
    await assert_saved(fresh_db, frames[0]["chat_id"], frames)
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["close", "cancel"])
async def test_real_agent_audit_closes_without_gc(db, fresh_db, monkeypatch, mode):
    from app.core.config import settings
    from tests.api.test_agent_stop_closeout import _install as agent_install, _slow_provider

    agent_install(monkeypatch, _slow_provider())
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: None)
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    response = await chat_routes.chat_stream(ChatRequest(
        message="Explain cancellation closeout.", request_id="s3-r1-audit", agent_mode="react",
    ), db)
    frames = [decode(await anext(response.body_iterator))]
    while not any(f["type"] == "agent" for f in frames):
        frames.append(decode(await asyncio.wait_for(anext(response.body_iterator), 5)))
    if mode == "cancel":
        task = asyncio.create_task(anext(response.body_iterator))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await response.body_iterator.aclose()
    await assert_saved(fresh_db, frames[0]["chat_id"], frames)
    runs = list((await fresh_db.execute(select(AgentRun).where(AgentRun.chat_id == frames[0]["chat_id"]))).scalars())
    assert len(runs) == 1 and runs[0].status == "stopped" and runs[0].finished_at is not None
    assert not chat_stop.request_stop("s3-r1-audit")
