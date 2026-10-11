"""S2: real CubeLoop/OpenAI serialization with a test-only HTTP transport."""
from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.core.config import settings
from app.generation import answer, clarify, note_gen
from app.routing import llm
from app.session import compaction, memory
from cubeloop.providers.base import ImageContent, StreamOptions, TextContent, UserMessage


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    original_connect = socket.socket.connect
    def blocked(sock, address):
        # Windows asyncio creates a loopback self-pipe before async fixtures run.
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        raise AssertionError("S2 checks must not open external sockets")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr("app.usage.instrumentation.record_provider_attempt", lambda *a, **k: None)


def config(provider="mimo", model="mimo-v2.5", base="https://api.xiaomimimo.com/v1"):
    return SimpleNamespace(provider=provider, model=model, base_url=base, api_key="s2-offline-only")


@pytest.mark.parametrize("provider,model_id,base,expected", [
    ("mimo", "mimo-v2.5", "https://api.xiaomimimo.com/v1", 1_000_000),
    ("xiaomi", "mimo-v2.5", "https://api.xiaomimimo.com/v1/", 1_000_000),
    ("mimo", "mimo-v2.6-flash", "https://api.xiaomimimo.com/v1", 1_048_576),
    ("mimo", "mimo-v2.5", "https://proxy.invalid/v1", 8192),
    ("mimo", "mimo-v2.5", "https://api.xiaomimimo.com.remote.invalid/v1", 8192),
    ("mimo", "mimo-v2.5", "http://api.xiaomimimo.com/v1", 8192),
    ("mimo", "mimo-v2.5", "https://api.xiaomimimo.com/v1?proxy=yes", 8192),
    ("other", "mimo-v2.5", "https://api.xiaomimimo.com/v1", 8192),
    ("mimo", "unknown", "https://api.xiaomimimo.com/v1", 8192),
])
def test_window_requires_verified_model_provider_and_endpoint(provider, model_id, base, expected):
    bound = answer._build_bound_model(include_reasoning=False, config=config(provider, model_id, base))
    assert bound.spec.context_window == expected
    assert bound.spec.max_tokens == answer.DIRECT_MAX_TOKENS
    assert bound.spec.reasoning is False
    assert bound.provider._capability.reasoning is None


@pytest.mark.parametrize("text", ["中", "x"])
@pytest.mark.parametrize("images", [None, ["data:image/png;base64,AA=="]])
def test_full_question_and_material_at_exact_window_boundary(text, images):
    model = SimpleNamespace(spec=SimpleNamespace(context_window=100_000))
    question = text * 3000 + "17 NOT 23"
    kwargs = dict(materials_text="[1] 甲公司未批准20万元。", images=images, model=model)
    _, messages, report = answer.assemble_answer_prompt(question, **kwargs)
    assert question in messages[-1]["content"] and kwargs["materials_text"] in messages[-1]["content"]
    required = report["input_units"] + report["output_tokens"] + report["reserve"]
    model.spec.context_window = required
    assert answer.assemble_answer_prompt(question, **kwargs)[2]["window"] == required
    model.spec.context_window = required - 1
    with pytest.raises(answer.ContextBudgetExceeded):
        answer.assemble_answer_prompt(question, **kwargs)
    assert report["reserve"] == answer.CONTEXT_RESERVE + (2048 if images else 0)


def test_known_large_input_passes_but_true_overflow_still_rejected():
    known = answer._build_bound_model(include_reasoning=False, config=config())
    prompt, messages, report = answer.assemble_answer_prompt("中" * 2000, model=known)
    assert report["window"] == 1_000_000 and len(messages[-1]["content"]) > 2000
    assert report["input_units"] + report["reserve"] + report["output_tokens"] < report["window"]
    with pytest.raises(answer.ContextBudgetExceeded):
        answer.assemble_answer_prompt("x" * 1_000_000, model=known)
    unknown = answer._build_bound_model(include_reasoning=False, config=config(base="https://proxy.invalid/v1"))
    with pytest.raises(answer.ContextBudgetExceeded):
        answer.assemble_answer_prompt("中" * 2000, model=unknown)


@pytest_asyncio.fixture
async def wire(monkeypatch, tmp_path):
    services = [{"id": "s2-chat", "provider": "mimo", "base_url": "https://api.xiaomimimo.com/v1",
                 "api_key": "s2-offline-only", "models": ["mimo-v2.5"]}]
    monkeypatch.setattr(settings, "MODEL_SERVICES", json.dumps(services))
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({
        role: {"service_id": "s2-chat", "model": "mimo-v2.5"} for role in ("chat", "vision")
    }))
    monkeypatch.setattr(settings, "CHAT_REASONING", '{"param":"thinking.type","value":"enabled","model":"mimo-v2.5"}')
    bodies = []
    clients = []

    def receive(request):
        bodies.append(json.loads(request.content))
        (tmp_path / "sdk-request.json").write_bytes(request.content)
        chunk = {"id": "s2-offline", "object": "chat.completion.chunk", "created": 1,
                 "model": "mimo-v2.5", "choices": [{"index": 0, "delta": {"content": "[]"},
                                                   "finish_reason": "stop"}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())

    def attach(bound):
        assert bound is not None
        clients.append(bound.provider._client._client)
        bound.provider._client._client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
        clients.append(bound.provider._client._client)
        return bound

    yield attach, bodies
    for client in clients:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [
    "final_sync", "final_stream", "vision", "clarify", "note_sync", "note_stream",
    "history_summary", "memory_extract", "classification", "agent_plan", "agent_decision",
])
async def test_every_role_limit_reaches_serialized_sdk_body(wire, role):
    from app.agent import runner
    attach, bodies = wire
    bound = attach(answer.build_chat_model() if role.startswith("final") else answer.build_aux_model())
    messages = [UserMessage(content=[TextContent(text="offline")])]
    expected = answer.DIRECT_MAX_TOKENS
    if role == "final_sync":
        await answer.answer_direct("offline", model=bound)
    elif role in ("final_stream", "vision"):
        if role == "vision":
            bound = attach(answer.build_vision_model())
            messages[0].content.append(ImageContent(source="AA==", media_type="image/png"))
        stream = await answer.stream_answer(bound, messages, "offline")
        async for _ in stream:
            pass
    elif role == "clarify":
        expected = clarify.CLARIFY_MAX_TOKENS
        await clarify.draft_clarify_question("为什么", model=bound)
    elif role in ("note_sync", "note_stream"):
        expected = note_gen.NOTE_MAX_TOKENS
        async def token(text):
            pass
        await note_gen.generate_note(document_id=1, filename="offline.md", body="正文",
                                     model=bound, on_token=token if role == "note_stream" else None)
    elif role == "history_summary":
        expected = settings.CHAT_COMPRESS_SUMMARY_MAX_TOKENS + compaction.SUMMARY_REASONING_HEADROOM
        await compaction.summarize("", [{"role": "user", "content": "完整事实"}], bound)
    elif role == "memory_extract":
        expected = memory.EXTRACT_MAX_TOKENS + memory.EXTRACT_REASONING_HEADROOM
        await memory._call_model(bound, "完整事实")
    elif role == "classification":
        classifier = llm.build_classifier()
        attach(classifier._bound)
        expected = llm.MAX_OUTPUT_TOKENS
        await classifier.classify("offline")
    elif role == "agent_plan":
        expected = runner.DECISION_MAX_OUTPUT_TOKENS
        await bound.generate(messages, options=StreamOptions(on_payload=runner._cap_decision_payload),
                             max_output_tokens=expected)
    else:
        expected = runner.DECISION_MAX_OUTPUT_TOKENS
        stream = await bound.stream(messages, options=StreamOptions(on_payload=runner._cap_decision_payload))
        async for _ in stream:
            pass
    assert len(bodies) == 1
    body = bodies[0]
    assert body["max_tokens"] == expected
    assert not ({"max_completion_tokens", "max_output_tokens", "extra_body"} & body.keys())
    if role.startswith("final"):
        assert body["thinking"] == {"type": "enabled"}
    else:
        assert not ({"thinking", "enable_thinking", "reasoning_effort", "reasoning"} & body.keys())
    if role == "vision":
        assert body["messages"][-1]["content"][-1]["type"] == "image_url"
    if role == "history_summary":
        assert body["temperature"] == compaction.SUMMARY_TEMPERATURE
    if role == "memory_extract":
        assert body["temperature"] == memory.EXTRACT_TEMPERATURE
    assert bound.spec.max_tokens == answer.DIRECT_MAX_TOKENS
