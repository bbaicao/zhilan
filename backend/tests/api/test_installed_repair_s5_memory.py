"""Real HTTP route plus installed SDK, using only MockTransport."""
import json

import httpx
import pytest

from app.api import chat_routes
from app.core.config import settings
from app.session import memory
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401
from tests.api.t0_wire import wire_sse_text
from tests.session.test_compaction import FakeCompactionModel
from tests.session.test_installed_repair_s5 import FACTS, QUESTIONS, capture, row, fixed_provider
from tests.test_opt5_observation_usage import _provider


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("question", QUESTIONS)
async def test_bilingual_memory_in_actual_route_sdk_send(client, db, monkeypatch, stream, question):
    db.add_all([row(text) for text in FACTS] + [row("无关天气记录")])
    await db.commit()
    bodies = []
    provider = fixed_provider(bodies)
    bound = provider.model("controlled", context_window=8192)
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: bound)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: FakeCompactionModel('{"facts": []}'))
    monkeypatch.setattr(chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external"))
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    monkeypatch.setattr(settings, "CHAT_COMPRESS_ENABLE", False)
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", True)
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    monkeypatch.setattr(memory, "schedule_extract", lambda **kwargs: None)
    try:
        response = await client.post("/api/chat/stream" if stream else "/api/chat",
                                     json={"message": question, "agent_mode": "off"})
        assert response.status_code == 200, response.text
        result = parse_sse(response.text)[-1] if stream else response.json()
        capture(f"memory-wire-{int(stream)}-{QUESTIONS.index(question)}",
                {"bodies": bodies, "result": result, "quality": "unverified; fixed response"})
        assert len(bodies) == 1
        text = json.dumps(bodies[0], ensure_ascii=False)
        assert all(fact in text for fact in FACTS)
        assert "无关天气记录" not in text
        assert all(memory.MEMORY_HEADER not in str(item["content"]) for item in bodies[0]["messages"]
                   if item["role"] != "system")
    finally:
        await provider._client.close()
