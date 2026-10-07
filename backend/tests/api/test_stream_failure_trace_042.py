"""An abrupt provider failure must not discard the completed retrieval trace."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import retrieval
from app.api import chat_routes
from app.core.config import settings
from app.models import Message, UsageEvent
from app.session import events
from app.vector.embed import FakeEmbedding
from tests.api.test_chat_contract import client, parse_sse, _StubClassifier  # noqa: F401
from tests.vector.test_u2_spaces import _seed_build


@pytest.mark.asyncio
@pytest.mark.parametrize("after_token", [False, True])
async def test_abrupt_model_failure_preserves_trace(client, db, monkeypatch, after_token):
    embedding = FakeEmbedding(dim=3)
    _document, _old, _current, current_space = await _seed_build(db, embedding)
    query_calls = []

    class BrokenStore:
        def for_space(self, requested):
            assert requested == current_space
            return self

        def query(self, *args, **kwargs):
            query_calls.append((args, kwargs))
            raise RuntimeError("Qdrant disconnected")

    class BrokenModel:
        async def stream(self, *args, **kwargs):
            if not after_token:
                raise TimeoutError("injected model timeout")

            async def chunks():
                yield SimpleNamespace(type="text_delta", delta="partial")
                raise TimeoutError("injected model timeout")

            return chunks()

    monkeypatch.setattr(retrieval.vector, "get_vector_store", lambda **kwargs: BrokenStore())
    monkeypatch.setattr(retrieval.vector, "get_embedding_client", lambda: embedding)
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: BrokenModel())
    monkeypatch.setattr(chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db"))
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    response = await client.post("/api/chat/stream", json={
        "message": "知识库的检索说明", "agent_mode": "off", "request_id": f"trace-042-{after_token}",
    })
    frames = parse_sse(response.text)
    assert len(query_calls) == 1, "故障必须发生在真实可查询的构建上"
    assert [f["type"] for f in frames if f["type"] in {"done", "error", "stopped"}] == ["error"], "I5"
    chat_id = frames[0]["chat_id"]
    starts = await events.list_events_by_type(db, chat_id, "retrieval/start")
    results = await events.list_events_by_type(db, chat_id, "retrieval/result")
    assert len(starts) == len(results) == 1, "I9 completed retrieval must survive model failure"
    assert starts[0].trace == results[0].trace
    assert results[0].data["degraded"] is True
    assert "仅使用关键词检索" in results[0].data["warning"]
    assert len(await events.list_events_by_type(db, chat_id, "routing/decided")) == 1, "I9"
    attempts = await events.list_events_by_type(db, chat_id, "assistant/attempt")
    assert len(attempts) == 1 and attempts[0].data["outcome"] == "aborted"
    rows = list((await db.scalars(select(Message).where(Message.chat_id == chat_id))).all())
    assert [(row.role, row.content) for row in rows] == [("user", "知识库的检索说明")]
    assert list((await db.scalars(select(UsageEvent))).all()) == []
    # The separately deferred turn/end behavior is intentionally unchanged.
    assert await events.list_events_by_type(db, chat_id, "turn/end") == []
