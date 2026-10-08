from types import SimpleNamespace

import pytest

from app.agent.types import ToolObservation
from app.agent.web.client import web_material
from app.api import chat_cache, chat_routes
from app.api.turn_plan import TurnPlan
from app.core.config import settings
from tests.api.test_chat_contract import client, parse_sse, use_faux


@pytest.fixture
def search_result(monkeypatch):
    from app.agent.tools import web_search

    calls = []
    result = ToolObservation(materials=[
        web_material("Verified source", "https://source.example/news", "Verified news detail")
    ], text="Verified news detail")

    async def search(args):
        calls.append(args.query)
        return result

    monkeypatch.setattr(settings, "WEB_ENABLE", True)
    monkeypatch.setattr(web_search, "build_spec", lambda: SimpleNamespace(handler=search))
    return calls, result


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_direct_chat_searches_and_returns_sources(client, use_faux, search_result, stream, monkeypatch):
    use_faux("Verified news detail [1]")
    calls, _ = search_result
    prompts = []
    assemble = chat_routes.answer_mod.assemble_answer_prompt

    def capture(*args, **kwargs):
        prompts.append(kwargs.get("materials_text", ""))
        return assemble(*args, **kwargs)

    monkeypatch.setattr(chat_routes.answer_mod, "assemble_answer_prompt", capture)
    path = "/api/chat/stream" if stream else "/api/chat"
    response = await client.post(path, json={
        "message": "What is new today?", "skill_ids": ["builtin:web"], "agent_mode": "off",
    })
    assert response.status_code == 200, response.text
    body = next(e for e in parse_sse(response.text) if e["type"] == "done") if stream else response.json()
    assert calls == ["What is new today?"]
    assert prompts and "Verified news detail" in prompts[-1]
    assert body["answer_source"] == "web"
    assert body["refs"][0]["url"] == "https://source.example/news"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_failed_search_does_not_call_answer_model(client, monkeypatch, use_faux, search_result, stream):
    use_faux("This answer must not be generated")
    _, result = search_result
    result.ok = False
    result.materials = []
    result.text = "Search provider unavailable"

    async def unexpected(*args, **kwargs):
        pytest.fail("answer model called after failed search")

    monkeypatch.setattr(chat_routes.answer_mod, "answer_direct", unexpected)
    monkeypatch.setattr(chat_routes.answer_mod, "stream_answer", unexpected)
    response = await client.post("/api/chat/stream" if stream else "/api/chat", json={
        "message": "What is new today?", "skill_ids": ["builtin:web"], "agent_mode": "off",
    })
    assert response.status_code == 200
    assert "Search provider unavailable" in response.text
    assert "This answer must not be generated" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"needs_model": False}, {"cached": True}, {"agent_mode": "react"},
    {"filters": {"kb_ids": [1]}}, {"skill_ids": []},
])
async def test_search_preserves_existing_scope_guards(search_result, changes):
    calls, _ = search_result
    values = dict(chat=None, question="news", images=[], history=[], filters=None,
                  skill_ids=["builtin:web"], source_type="external", kind="direct",
                  answer_source="general", needs_model=True)
    values.update(changes)
    assert await chat_routes._prepare_direct_web(TurnPlan(**values)) is None
    assert calls == []


def test_web_turn_cannot_use_cached_answers(monkeypatch):
    monkeypatch.setattr(chat_cache.cache_store, "enabled", lambda: True)
    assert chat_cache.build_cache_key(
        question="news", filters=None, agent_mode="off", skill_ids=["builtin:web"],
        images=[], history=[], memory_text="", source_type="external",
    ) is None
