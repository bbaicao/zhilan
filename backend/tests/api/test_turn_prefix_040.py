"""Prefix delivery, raw-result classification, and interruption accounting."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.api import chat_routes, chat_stop
from app.api.chat_schemas import ChatRequest
from app.core.config import settings
from app.core.errors import AUTH_HINT, NOT_CONFIGURED_HINT, TIMEOUT_HINT
from app.generation import clarify
from app.generation.answer import EMPTY_ANSWER_HINT
from app.models import UsageEvent
from app.session import attempt, events, repository
from tests.api.test_chat_contract import client, faux_model, parse_sse  # noqa: F401
from tests.api.test_chat_stream_events import _StreamModel, _messages
from tests.api.test_bug153_stream_error_semantics import _FakeStream, _FailingModel as StreamFailure
from tests.api.test_bug154_sync_error_semantics import _FailingModel as SyncFailure, _error_result
from tests.api.t0_wire import Wire, _sse_chunk, attach_wire, wire_env, wire_sse_text  # noqa: F401

QUESTION = "请介绍一般的视频拍摄方法"
PREFIX = clarify.assume_prefix(True)


async def prepare(db, monkeypatch, model, *, assume=True):
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", True)
    monkeypatch.setattr(settings, "QUERY_CLARIFY_MAX_ROUNDS", 2)
    monkeypatch.setattr(settings, "ROUTING_MODE", "off")
    monkeypatch.setattr(settings, "AGENT_MODE", "off")
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", True)
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    chat = await repository.create_chat(db, title="prefix regression")
    chat.clarify_rounds = 2 if assume else 0
    await db.commit()
    return chat


async def chat_usage(db):
    return await db.scalar(
        select(func.count()).select_from(UsageEvent).where(UsageEvent.kind == "chat")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("assume", [False, True])
async def test_sync_stream_storage_and_history_match(client, db, monkeypatch, wire_env, assume):
    raw = "先确定主题。再规划镜头。"
    wire = Wire(
        wire_sse_text(raw),
        _sse_chunk({"role": "assistant", "content": "先确定主题。"})
        + _sse_chunk({"content": "再规划镜头。"})
        + _sse_chunk({}, finish="stop") + "data: [DONE]\n\n",
    )
    bound = attach_wire(wire, monkeypatch).chat
    chat = await prepare(db, monkeypatch, bound, assume=assume)
    response = await client.post(
        "/api/chat", json={"chat_id": chat.id, "message": QUESTION}
    )
    assert response.status_code == 200
    expected = (PREFIX if assume else "") + raw
    assert response.json()["message"] == expected
    stream_chat = await prepare(
        db, monkeypatch, bound, assume=assume
    )
    streamed = await client.post(
        "/api/chat/stream", json={"chat_id": stream_chat.id, "message": QUESTION}
    )
    frames = parse_sse(streamed.text)
    tokens = [frame["text"] for frame in frames if frame["type"] == "token"]
    assert tokens == [(PREFIX if assume else "") + "先确定主题。", "再规划镜头。"]
    assert "".join(tokens) == expected
    for chat_id in (chat.id, stream_chat.id):
        assert (await _messages(db, chat_id))[-1].content == expected
        for _ in range(2):
            history = await client.get(f"/api/chats/{chat_id}")
            assert history.json()["messages"][-1]["content"] == expected
    assert await chat_usage(db) == 2
    assert len(wire.request_bodies) == 2 and wire.queue == []


@pytest.mark.asyncio
@pytest.mark.parametrize("assume", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "raw,hint",
    [
        (TIMEOUT_HINT, TIMEOUT_HINT),
        (AUTH_HINT, AUTH_HINT),
        ("尚未配置大模型，请配置后重试。", NOT_CONFIGURED_HINT),
        ("回答生成失败，请稍后重试。（TestError）", None),
    ],
)
async def test_prefix_does_not_change_error_class_or_usage(
    client, db, monkeypatch, assume, stream, raw, hint
):
    final = _error_result(error_type="ProviderUnavailable", error_message=raw)
    model = StreamFailure(_FakeStream([], final)) if stream else SyncFailure(final=final)
    chat = await prepare(db, monkeypatch, model, assume=assume)
    response = await client.post(
        "/api/chat/stream" if stream else "/api/chat",
        json={"chat_id": chat.id, "message": QUESTION},
    )
    assert response.status_code == (
        200 if stream or raw not in {TIMEOUT_HINT, AUTH_HINT}
        else 504 if raw == TIMEOUT_HINT else 401
    )
    if stream:
        frames = parse_sse(response.text)
        terminals = [f for f in frames if f["type"] in {"error", "done", "stopped"}]
        assert len(terminals) == 1
        assert terminals[0] == {
            "type": "error", "detail": "生成失败，请稍后重试。（ProviderUnavailable）"
        }
        assert not [f for f in frames if f["type"] == "token"]
    else:
        if raw in {TIMEOUT_HINT, AUTH_HINT}:
            assert response.json() == {"detail": hint}
        else:
            assert response.json()["message"] == f"回答生成失败，请稍后重试。（{raw}）"
    assert [r.role for r in await _messages(db, chat.id)] == ["user"]
    assert await chat_usage(db) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("assume", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("raw", [
    TIMEOUT_HINT, AUTH_HINT, "尚未配置大模型，请配置后重试。",
    "回答生成失败，请稍后重试。（TestError）",
])
async def test_completed_error_text_is_an_answer(client, db, monkeypatch, assume, stream, raw):
    """与结构化失败腿同文；成功正文仍带前缀、落 assistant 并正常收尾。"""
    model = _StreamModel([raw]) if stream else faux_model(raw)
    chat = await prepare(db, monkeypatch, model, assume=assume)
    response = await client.post(
        "/api/chat/stream" if stream else "/api/chat",
        json={"chat_id": chat.id, "message": QUESTION},
    )
    assert response.status_code == 200
    expected = (PREFIX if assume else "") + raw
    if stream:
        frames = parse_sse(response.text)
        assert frames[-1]["type"] == "done"
        assert not [f for f in frames if f["type"] == "error"]
        assert "".join(f["text"] for f in frames if f["type"] == "token") == expected
    else:
        assert response.json()["message"] == expected
    rows = await _messages(db, chat.id)
    assert [r.role for r in rows] == ["user", "assistant"]
    assert rows[-1].content == expected
    assert await chat_usage(db) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_no_body_does_not_create_a_prefix_only_answer(client, db, monkeypatch, stream):
    model = (
        _StreamModel(["only reasoning"], kinds=["thinking_delta"])
        if stream
        else faux_model("")
    )
    chat = await prepare(db, monkeypatch, model)
    response = await client.post(
        "/api/chat/stream" if stream else "/api/chat",
        json={"chat_id": chat.id, "message": QUESTION},
    )
    assert response.status_code == 200
    if stream:
        frames = parse_sse(response.text)
        assert not [f for f in frames if f["type"] == "token"]
        assert frames[-1] == {"type": "error", "detail": EMPTY_ANSWER_HINT}
    else:
        assert response.json()["message"] == EMPTY_ANSWER_HINT
    assert [r.role for r in await _messages(db, chat.id)] == ["user"]
    assert await chat_usage(db) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("after_body", [False, True])
async def test_stop_accounts_for_exact_delivered_text(client, db, monkeypatch, after_body):
    chat = await prepare(
        db,
        monkeypatch,
        _StreamModel(["正文", "未交付"], delay=10, first_delay=0 if after_body else 10),
    )
    request_id = f"prefix-stop-{after_body}"
    response = await chat_routes.chat_stream(
        ChatRequest(chat_id=chat.id, message=QUESTION, request_id=request_id), db
    )
    frames = []
    try:
        async for data in response.body_iterator:
            frame = parse_sse(data.decode() if isinstance(data, bytes) else data)[0]
            frames.append(frame)
            if frame["type"] == ("token" if after_body else "meta"):
                stopped = await client.post(
                    "/api/chat/stop", json={"request_id": request_id}
                )
                assert stopped.json() == {"stopped": True}
    finally:
        await response.body_iterator.aclose()
    assert [f["type"] for f in frames if f["type"] in {"stopped", "done", "error"}] == [
        "stopped"
    ]
    delivered = "".join(f["text"] for f in frames if f["type"] == "token")
    if after_body:
        assert delivered == PREFIX + "正文"
        rows = await _messages(db, chat.id)
        assert rows[-1].kind == "stopped"
        assert rows[-1].content == delivered
        history = await client.get(f"/api/chats/{chat.id}")
        assert history.json()["messages"][-1]["content"] == delivered
    else:
        assert delivered == ""
        assert [r.role for r in await _messages(db, chat.id)] == ["user"]
    interrupted = await events.list_events_by_type(db, chat.id, "stream/interrupted")
    assert len(interrupted) == 1
    for key, value in attempt.delivered_fingerprint(delivered).items():
        assert interrupted[0].data[key] == value
    assert await chat_usage(db) == 0
    assert not chat_stop.is_registered(request_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_body", [False, True])
async def test_exception_before_or_after_first_body(client, db, monkeypatch, after_body):
    chat = await prepare(db, monkeypatch, _StreamModel([]))

    # ⚠️ 形参必须与生产端点的调用点同形（`..., aux_model=aux`）——推理等级回流给
    # `stream_tokens` 加了 keyword-only 的 `aux_model`。窄签名会让本替身**抛 TypeError**，
    # 端点的流内兜底把它渲染成 `error` 帧，于是「一个 token 都没收到」——
    # 症状与真正要验的「前缀丢了」**长得一模一样**（2026-09-20 实测：本用例 `[True]`
    # 参数化因此假红，修法就是把这个形参补上）。
    async def broken_stream(plan, model, db, *, aux_model=None):
        yield "reasoning", "thinking"
        if after_body:
            yield "token", "正文"
        raise RuntimeError("injected stream failure")

    monkeypatch.setattr(chat_routes, "stream_tokens", broken_stream)
    response = await client.post(
        "/api/chat/stream", json={"chat_id": chat.id, "message": QUESTION}
    )
    frames = parse_sse(response.text)
    tokens = [f["text"] for f in frames if f["type"] == "token"]
    assert tokens == ([PREFIX + "正文"] if after_body else [])
    assert frames[-1]["type"] == "error"
    assert not await events.list_events_by_type(db, chat.id, "turn/end")
    assert await chat_usage(db) == 0
