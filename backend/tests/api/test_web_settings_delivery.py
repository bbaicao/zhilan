import pytest

from app.api import settings_routes
from app.core.config import ModelConfig
from app.core.config import settings
from tests.api.test_settings_contract import client, fake_dotenv, wired


def test_save_web_and_preserve_keys(client, fake_dotenv, wired):
    response = client.put("/api/settings", json={"web": {
        "enabled": True, "channel": "api", "provider": "tavily", "api_key": "search-secret",
        "max_results": 3, "timeout": 15, "llm_provider": "mimo",
        "llm_api_key": "mimo-secret", "llm_search_tool": "web_search", "llm_max_searches": 4,
    }})
    assert response.status_code == 200, response.text
    assert response.json()["web"]["effective"]
    assert settings.WEB_ENABLE is True and settings.WEB_FETCH_TIMEOUT == 15
    response = client.put("/api/settings", json={"web": {"api_key": "", "llm_api_key": "", "enabled": False}})
    assert response.status_code == 200
    assert settings.WEB_SEARCH_API_KEY == "search-secret"
    assert settings.WEB_LLM_API_KEY == "mimo-secret"
    assert settings.WEB_ENABLE is False
    assert "search-secret" not in response.text and "mimo-secret" not in response.text
    assert "WEB_ENABLE=false" in fake_dotenv.read_text(encoding="utf-8")


@pytest.mark.parametrize("web", [
    {"max_results": 0}, {"timeout": 100}, {"llm_max_searches": 7},
    {"channel": "bad"}, {"ssrf_allow_any_host": False},
])
def test_invalid_web_rejects_other_segments_atomically(client, fake_dotenv, wired, web):
    before = fake_dotenv.read_bytes()
    rounds = settings.AGENT_MAX_ROUNDS
    response = client.put("/api/settings", json={"web": web, "agent": {"max_rounds": 4}})
    assert response.status_code == 400
    assert fake_dotenv.read_bytes() == before
    assert settings.AGENT_MAX_ROUNDS == rounds


def test_legacy_provider_does_not_block_api_save(client, wired, monkeypatch):
    monkeypatch.setattr(settings, "WEB_LLM_PROVIDER", "deepseek")
    response = client.put("/api/settings", json={"web": {
        "channel": "api", "provider": "duckduckgo", "llm_provider": "deepseek",
    }})
    assert response.status_code == 200, response.text
    assert settings.WEB_LLM_PROVIDER == "deepseek"


def test_probe_reuses_mimo_key_only_for_effective_endpoint(client, wired, monkeypatch):
    chat = ModelConfig(kind="openai", provider="custom", base_url="https://api.xiaomimimo.com/v1",
                       api_key="private-mimo-key", model="mimo-v2.6-flash")
    monkeypatch.setattr(type(settings), "chat_config", property(lambda _: chat))
    monkeypatch.setattr(settings, "WEB_LLM_PROVIDER", "mimo")
    monkeypatch.setattr(settings, "WEB_LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "WEB_LLM_API_KEY", "")
    calls = []

    async def fetch(base, key):
        calls.append((base, key))
        return [chat.model]

    monkeypatch.setattr(settings_routes, "_fetch_models", fetch)
    response = client.post("/api/web/llm/models", json={})
    assert response.status_code == 200, response.text
    assert calls == [(chat.base_url, chat.api_key)]
    assert chat.api_key not in response.text
    response = client.post("/api/web/llm/models", json={"base_url": "https://other.example/v1"})
    assert response.status_code == 400
    assert len(calls) == 1
