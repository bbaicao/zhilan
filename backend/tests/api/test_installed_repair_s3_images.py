"""S3: current-image short questions, with real routes and offline SDK requests."""
import httpx
import httpx2
import pytest
from sqlalchemy import select

from app.api import chat_cache, chat_routes
from app.core.config import settings
from app.generation import answer
from app.models import Message
from app.session import events
from tests.api.test_bug153_stream_error_semantics import _install
from tests.api.test_chat_contract import client, parse_sse  # noqa: F401
from tests.api.test_opt2_answer_closeout import ReplyModel
from tests.generation.test_installed_repair_s2_models import no_external_network, wire  # noqa: F401


IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jkZkAAAAASUVORK5CYII="


@pytest.fixture(autouse=True)
def offline_http_boundary(monkeypatch, no_external_network):
    denied = []

    def require_loopback(request):
        if request.url.host not in ("127.0.0.1", "::1"):
            denied.append(request.url.host)
            raise AssertionError("S3 forbids real HTTP transports")

    # ponytail: patch both installed SDK transports; retain ASGI/MockTransport paths.
    for module in (httpx, httpx2):
        def guarded_sync(self, request, original=module.HTTPTransport.handle_request):
            require_loopback(request)
            return original(self, request)

        async def guarded_async(self, request, original=module.AsyncHTTPTransport.handle_async_request):
            require_loopback(request)
            return await original(self, request)

        monkeypatch.setattr(module.HTTPTransport, "handle_request", guarded_sync)
        monkeypatch.setattr(module.AsyncHTTPTransport, "handle_async_request", guarded_async)

    yield denied
    assert not denied, f"Unexpected external HTTP requests: {denied}"


class ClarifyModel(ReplyModel):
    async def generate(self, messages, system_prompt=None, max_output_tokens=None, temperature=None):
        return await super().generate(messages, system_prompt, max_output_tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("module,asynchronous", [(httpx, False), (httpx, True), (httpx2, False), (httpx2, True)])
async def test_transport_guard_blocks_both_sdk_clients(offline_http_boundary, module, asynchronous):
    if asynchronous:
        async with module.AsyncClient() as probe:
            with pytest.raises(AssertionError, match="S3 forbids real HTTP transports"):
                await probe.get("https://guard.invalid")
    else:
        with module.Client() as probe:
            with pytest.raises(AssertionError, match="S3 forbids real HTTP transports"):
                probe.get("https://guard.invalid")
    assert offline_http_boundary == ["guard.invalid"]
    # Only this deliberate denial probe is consumed; unexpected attempts fail fixture teardown.
    offline_http_boundary.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("module,asynchronous", [(httpx, False), (httpx, True), (httpx2, False), (httpx2, True)])
@pytest.mark.parametrize("host,url", [
    (b"127.0.0.1", "http://127.0.0.1/boundary"),
    (b"::1", "http://[::1]/boundary"),
])
async def test_transport_guard_delegates_loopback_to_both_sdk_pools(
    offline_http_boundary, monkeypatch, module, asynchronous, host, url,
):
    class LocalTransportReached(RuntimeError):
        pass

    calls = []

    def reached(request):
        calls.append((request.url.scheme, request.url.host, request.url.port, request.url.target))
        raise LocalTransportReached()

    if asynchronous:
        transport = module.AsyncHTTPTransport(trust_env=False)

        async def async_reached(request):
            reached(request)

        monkeypatch.setattr(transport._pool, "handle_async_request", async_reached)
        async with module.AsyncClient(transport=transport, trust_env=False) as probe:
            with pytest.raises(LocalTransportReached):
                await probe.get(url)
    else:
        transport = module.HTTPTransport(trust_env=False)
        monkeypatch.setattr(transport._pool, "handle_request", reached)
        with module.Client(transport=transport, trust_env=False) as probe:
            with pytest.raises(LocalTransportReached):
                probe.get(url)
    assert calls == [(b"http", host, None, b"/boundary")]
    assert not offline_http_boundary


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("with_image", [False, True])
async def test_same_short_question_preserves_text_clarify_but_sends_current_image(
    client, monkeypatch, wire, stream, with_image,
):
    attach, bodies = wire
    bound = attach(answer.build_vision_model())
    _install(monkeypatch, ReplyModel("text-model-must-not-answer"))
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: ClarifyModel("请补充您想了解的对象？"))
    monkeypatch.setattr(chat_routes, "resolve_vision_model", lambda: bound)
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", True)
    response = await client.post(
        "/api/chat/stream" if stream else "/api/chat",
        json={"message": "为什么", "images": [IMAGE] if with_image else []},
    )
    assert response.status_code == 200, response.text
    result = parse_sse(response.text)[-1] if stream else response.json()
    if not with_image:
        assert result["kind"] == "clarify" and not bodies
        assert result["generation_status"] == "completed" and result["finish_reason"] is None
        return
    assert result["kind"] == "direct", result
    assert len(bodies) == 1
    body = bodies[0]
    assert body["max_tokens"] == 2048
    assert body["messages"][-1]["content"][-1] == {
        "type": "image_url", "image_url": {"url": IMAGE},
    }
    assert result["type"] == "done" if stream else result["message"].endswith("[]")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_unconfigured_vision_rejected_before_chat_creation(client, db, monkeypatch, stream):
    monkeypatch.setattr(chat_routes, "resolve_vision_model", lambda: None)
    count_before = list((await db.execute(select(Message.id))).scalars())
    response = await client.post(
        "/api/chat/stream" if stream else "/api/chat",
        json={"message": "为什么", "images": [IMAGE]},
    )
    assert response.status_code == 400
    assert list((await db.execute(select(Message.id))).scalars()) == count_before


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_bad_image_provider_failure_has_no_success_side_effects(
    client, db, monkeypatch, stream,
):
    model = ReplyModel("rejected image", "error")
    _install(monkeypatch, model)
    monkeypatch.setattr(chat_routes, "resolve_vision_model", lambda: model)
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", True)
    writes = []
    monkeypatch.setattr(chat_cache, "schedule_cache_write", lambda *a, **k: writes.append("cache"))
    monkeypatch.setattr(chat_routes.memory_mod, "schedule_extract", lambda **k: writes.append("memory"))
    response = await client.post(
        "/api/chat/stream" if stream else "/api/chat",
        json={"message": "为什么", "images": ["data:image/png;base64,INVALID"]},
    )
    assert response.status_code == 200
    result = parse_sse(response.text)[-1] if stream else response.json()
    assert len(model.calls) == 1
    if stream:
        assert result["type"] == "error"
    else:
        assert result["message"] != "rejected image"
    chat_id = result.get("chat_id") if not stream else parse_sse(response.text)[0]["chat_id"]
    await db.rollback()
    rows = list((await db.execute(select(Message).where(Message.chat_id == chat_id))).scalars())
    assert [row.role for row in rows] == ["user"]
    assert not writes


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_ia006_length_is_reported_with_persisted_state(client, db, monkeypatch, stream):
    _install(monkeypatch, ReplyModel("partial response", "length"))
    response = await client.post(
        "/api/chat/stream" if stream else "/api/chat",
        json={"message": "Explain the current record fields."},
    )
    assert response.status_code == 200
    result = parse_sse(response.text)[-1] if stream else response.json()
    if stream:
        assert result["type"] == "done"
    history = await events.list_events(db, result["chat_id"])
    attempt = [event.data for event in history if event.type == "assistant/attempt"][-1]
    assert attempt["generation_status"] == "incomplete" and attempt["finish_reason"] == "length"
    assert result["generation_status"] == "incomplete" and result["finish_reason"] == "length"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason,status", [("stop", "completed"), ("length", "incomplete"), ("aborted", "stopped"), ("error", "failed")])
async def test_terminal_status_matches_history_without_failure_success_effects(
    client, db, monkeypatch, stream, reason, status,
):
    _install(monkeypatch, ReplyModel("partial reply", reason))
    writes = []
    monkeypatch.setattr(chat_cache, "schedule_cache_write", lambda *a, **k: writes.append("cache"))
    monkeypatch.setattr(chat_routes.memory_mod, "schedule_extract", lambda **k: writes.append("memory"))
    response = await client.post(
        "/api/chat/stream" if stream else "/api/chat", json={"message": "Explain the record fields."},
    )
    assert response.status_code == 200, response.text
    frames = parse_sse(response.text) if stream else []
    result = frames[-1] if stream else response.json()
    assert result["generation_status"] == status
    assert result["finish_reason"] == reason
    chat_id = frames[0]["chat_id"] if stream else result["chat_id"]
    await db.rollback()
    rows = list((await db.execute(select(Message).where(Message.chat_id == chat_id).order_by(Message.id))).scalars())
    if status == "failed":
        assert [row.role for row in rows] == ["user"]
    else:
        assert rows[-1].thinking["generation_status"] == status
        assert rows[-1].thinking["finish_reason"] == reason
    if status != "completed":
        assert not writes


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("case", ["immediate", "explicit", "new-topic", "replace", "delete", "other-chat", "compressed", "budget", "count", "bytes", "invalid", "unconfigured", "late-budget"])
async def test_related_history_image_is_bounded_and_never_duplicated(
    client, db, monkeypatch, wire, stream, case,
):
    from app.models import Chat
    attach, bodies = wire
    writes = []
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(chat_cache, "schedule_cache_write", lambda *a, **k: writes.append("cache"))
    vision = attach(answer.build_vision_model())
    _install(monkeypatch, ReplyModel("text reply"))
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: ClarifyModel("请说明对象？"))
    monkeypatch.setattr(chat_routes, "resolve_vision_model", lambda: vision)
    first = await client.post("/api/chat", json={"message": "Describe this picture.", "images": [IMAGE]})
    assert first.status_code == 200, first.text
    chat_id = first.json()["chat_id"]
    await db.rollback()
    original = (await db.execute(select(Message).where(Message.chat_id == chat_id, Message.role == "user"))).scalar_one()
    replacement = IMAGE.replace("image/png", "image/jpeg")
    message = "为什么" if case == "immediate" else "这张图里的内容是什么意思"
    payload = {"chat_id": chat_id, "message": message}
    if case == "new-topic":
        payload["message"] = "Explain database transaction isolation."
    elif case == "replace":
        payload["images"] = [replacement]
    elif case == "delete":
        original.images = []
    elif case == "other-chat":
        other = await client.post("/api/chat", json={"message": "Explain database fields."})
        payload["chat_id"] = other.json()["chat_id"]
    elif case == "compressed":
        chat = await db.get(Chat, chat_id)
        chat.compressed_up_to = original.id
    elif case == "budget":
        original.content = "long image context " * 2000
        vision.spec.context_window = 8192
    elif case == "count":
        original.images = [IMAGE] * 5
    elif case == "bytes":
        original.images = ["data:image/png;base64," + "A" * (14 * 1024 * 1024)] * 2
    elif case == "invalid":
        original.images = ["data:image/png;base64,INVALID"]
    elif case == "unconfigured":
        monkeypatch.setattr(chat_routes, "resolve_vision_model", lambda: None)
    elif case == "late-budget":
        if stream:
            original_stream = chat_routes.stream_tokens
            async def after_preparation(plan, model, *args, **kwargs):
                model.spec.context_window = 1000
                async for item in original_stream(plan, model, *args, **kwargs):
                    yield item
            monkeypatch.setattr(chat_routes, "stream_tokens", after_preparation)
        else:
            original_generate = chat_routes._generate
            async def after_preparation(plan, model):
                model.spec.context_window = 1000
                return await original_generate(plan, model)
            monkeypatch.setattr(chat_routes, "_generate", after_preparation)
    await db.commit()
    bodies.clear()
    response = await client.post("/api/chat/stream" if stream else "/api/chat", json=payload)
    if case == "late-budget":
        assert response.status_code == 200
        result = parse_sse(response.text)[-1] if stream else response.json()
        assert result["generation_status"] == "failed"
        assert "重新附图" in (result["detail"] if stream else result["message"])
        assert not bodies and not writes
        await db.rollback()
        rows = list((await db.execute(select(Message).where(Message.chat_id == chat_id).order_by(Message.id))).scalars())
        assert [row.role for row in rows] == ["user", "assistant", "user"]
        return
    unavailable = case in {"delete", "other-chat", "compressed", "budget", "count", "bytes", "invalid", "unconfigured"}
    if unavailable:
        assert response.status_code == 400, response.text
        assert ("视觉模型" if case == "unconfigured" else "重新附图") in response.json()["detail"]
        assert not bodies
        return
    assert response.status_code == 200, response.text
    if case == "new-topic":
        assert not bodies
    else:
        assert len(bodies) == 1
        assert bodies[0]["messages"][-1]["content"][-1]["image_url"]["url"] == (replacement if case == "replace" else IMAGE)
        assert not writes, "Image answers must not enter the text answer cache"
    await db.rollback()
    rows = list((await db.execute(select(Message).where(Message.chat_id == chat_id, Message.role == "user").order_by(Message.id))).scalars())
    assert (rows[-1].images or []) == ([replacement] if case == "replace" else [])
