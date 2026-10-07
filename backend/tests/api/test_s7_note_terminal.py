"""S7/174: drive the real note SSE iterator, including transport disconnects."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.api import document_routes
from app.core.errors import RagError
from app.generation.note_gen import GeneratedNote


FALLBACK = {"type": "error", "detail": "笔记生成流程异常结束，请重试。"}


@pytest.fixture
def note_endpoint(monkeypatch):
    doc = SimpleNamespace(id=174, filename="s7-note.md", category="", tags=[])

    async def get_doc(*_):
        return doc

    async def get_chunks(*_):
        return "可控笔记材料"

    monkeypatch.setattr(document_routes, "_get_live_document", get_doc)
    monkeypatch.setattr(document_routes, "_chunks_text", get_chunks)
    monkeypatch.setattr(document_routes, "_require_note_model", lambda: None)
    monkeypatch.setattr(document_routes, "_resolve_note_model", lambda: object())

    async def make_response(generator):
        monkeypatch.setattr(document_routes.note_gen_mod, "generate_note", generator)
        return await document_routes.generate_note_stream_endpoint(doc.id, object())

    return make_response


def frame(raw):
    return json.loads(raw.decode().removeprefix("data: "))


async def collect(response):
    return [frame(raw) async for raw in response.body_iterator]


def terminal(frames):
    return [event for event in frames if event["type"] in {"done", "error"}]


async def success(**kwargs):
    await kwargs["on_status"]("准备中")
    await kwargs["on_token"]("笔记正文")
    return GeneratedNote(
        title="草稿", content="笔记正文", category="", tags=[],
        source_document_id=kwargs["document_id"], source_filename=kwargs["filename"],
        completeness={"complete": True, "checker": "regex", "checked": 0, "missing": []},
    )


@pytest.mark.asyncio
async def test_success_has_exactly_one_done(note_endpoint):
    frames = await collect(await note_endpoint(success))
    assert [event["type"] for event in frames] == ["status", "token", "done"]
    assert len(terminal(frames)) == 1
    assert frames[-1]["content"] == "笔记正文"
    assert set(frames[-1]) == {
        "type", "title", "content", "category", "tags", "source_document_id",
        "source_filename", "completeness",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "detail"),
    [
        (TimeoutError("private provider text"), "笔记生成失败，请稍后重试。（TimeoutError）"),
        (RagError("可读失败原因"), "可读失败原因"),
    ],
)
async def test_failure_keeps_existing_wording(note_endpoint, exc, detail):
    async def broken(**kwargs):
        await kwargs["on_token"]("半截草稿")
        raise exc

    frames = await collect(await note_endpoint(broken))
    assert frames[0] == {"type": "token", "text": "半截草稿"}
    assert terminal(frames) == [{"type": "error", "detail": detail}]


@pytest.mark.asyncio
@pytest.mark.parametrize("emit_token", [False, True])
async def test_producer_cancel_without_terminal_emits_fallback(note_endpoint, emit_token):
    async def cancelled(**kwargs):
        if emit_token:
            await kwargs["on_token"]("半截草稿")
        raise asyncio.CancelledError()

    frames = await collect(await note_endpoint(cancelled))
    assert terminal(frames) == [FALLBACK]
    assert frames[-1] == FALLBACK


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_kind", ["status", "done", "error"])
async def test_serializer_failure_emits_one_fallback(note_endpoint, monkeypatch, bad_kind):
    real_sse = document_routes.sse

    def bad_sse(event):
        if event["type"] == bad_kind and event != FALLBACK:
            raise TypeError("injected serialization failure")
        return real_sse(event)

    monkeypatch.setattr(document_routes, "sse", bad_sse)

    async def broken(**kwargs):
        await kwargs["on_token"]("半截草稿")
        raise RuntimeError("test error event serialization")

    response = await note_endpoint(broken if bad_kind == "error" else success)
    frames = await collect(response)
    assert terminal(frames) == [FALLBACK]


@pytest.mark.asyncio
async def test_consumer_failure_emits_one_fallback_and_awaits_producer(
    note_endpoint, monkeypatch,
):
    real_wait_for = asyncio.wait_for
    cleaned = asyncio.Event()
    producer = []

    async def pending(**kwargs):
        producer.append(asyncio.current_task())
        try:
            await kwargs["on_token"]("半截草稿")
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    injected = False

    async def fail_consumer(awaitable, timeout):
        nonlocal injected
        if not injected:
            injected = True
            awaitable.close()
            await asyncio.sleep(0)  # producer has entered its cleanup-protected body
            raise RuntimeError("injected queue consumer failure")
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(document_routes.asyncio, "wait_for", fail_consumer)
    frames = await collect(await note_endpoint(pending))
    assert terminal(frames) == [FALLBACK]
    assert cleaned.is_set() and producer[0].done()


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect", ["aclose", "cancel", "asgi23", "asgi24"])
async def test_disconnect_cancels_and_awaits_producer_without_terminal(
    note_endpoint, disconnect,
):
    cleaned = asyncio.Event()
    token_sent = asyncio.Event()
    producer = []
    sent = []

    async def pending(**kwargs):
        producer.append(asyncio.current_task())
        try:
            await kwargs["on_token"]("半截草稿")
            await asyncio.Event().wait()
        finally:
            # Cleanup must be awaited even inside Starlette's cancelled task group.
            await asyncio.sleep(0.01)
            cleaned.set()

    response = await note_endpoint(pending)
    if disconnect in {"aclose", "cancel"}:
        sent.append(frame(await anext(response.body_iterator)))
        if disconnect == "aclose":
            await response.body_iterator.aclose()
        else:
            getter = asyncio.create_task(anext(response.body_iterator))
            await asyncio.sleep(0)
            getter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await getter
    else:
        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                sent.append(frame(message["body"]))
                token_sent.set()
                if disconnect == "asgi24":
                    raise OSError("socket is disconnected")

        async def receive():
            await token_sent.wait()
            return {"type": "http.disconnect"}

        scope = {"type": "http", "asgi": {"spec_version": "2.4" if disconnect == "asgi24" else "2.3"}}
        if disconnect == "asgi24":
            from starlette.requests import ClientDisconnect

            with pytest.raises(ClientDisconnect):
                await response(scope, receive, send)
        else:
            await response(scope, receive, send)

    assert cleaned.is_set(), "response must finish the producer cleanup before returning"
    assert producer[0].done(), "no orphan producer after client disconnect"
    assert terminal(sent) == []
