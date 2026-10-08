import httpx
import pytest

from app.agent.web import client as web
from app.agent.web.llm import resolved_config
from app.core.config import ModelConfig, settings
from tests.agent.test_web_client import _FakeResponse, _install_httpx


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for key, value in {
        "WEB_CHANNEL": "llm", "WEB_LLM_PROVIDER": "mimo",
        "WEB_LLM_BASE_URL": "https://api.xiaomimimo.com/v1",
        "WEB_LLM_API_KEY": "private-test-key", "WEB_LLM_MODEL": "mimo-v2.6-flash",
        "WEB_LLM_SEARCH_TOOL": "web_search", "WEB_LLM_MAX_SEARCHES": 5,
    }.items():
        monkeypatch.setattr(settings, key, value)


@pytest.mark.asyncio
async def test_native_tool_and_only_annotated_sources(monkeypatch):
    log = _install_httpx(monkeypatch, _FakeResponse(json_body={"choices": [{"message": {
        "content": "Search summary https://fabricated.example",
        "annotations": [
            {"type": "url_citation", "url": "https://source.example/article", "title": "Source",
             "summary": "Verified article summary"},
            {"type": "url_citation", "url_citation": {"url": "https://source.example/article"}},
            {"type": "url_citation", "url": "file:///private"},
        ],
    }}]}))
    hits = await web.search_web("current news")
    assert [hit.url for hit in hits] == ["https://source.example/article"]
    assert hits[0].snippet == "Verified article summary"
    request = log[-1]
    assert request["json"]["tools"] == [{"type": "web_search", "force_search": True, "max_keyword": 5}]
    assert request["json"]["model"] == "mimo-v2.6-flash"
    assert log[0]["opts"]["event_hooks"]["request"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    _FakeResponse(status_code=401, text="private-test-key"),
    _FakeResponse(json_body={"choices": [{"message": {"content": "https://made-up.example"}}]}),
    httpx.ConnectError("private-test-key"),
])
async def test_failure_is_explicit_and_redacted(monkeypatch, response):
    _install_httpx(monkeypatch, response)
    with pytest.raises(web.WebToolError) as caught:
        await web.search_web("news")
    assert "private-test-key" not in str(caught.value)


def test_key_reuse_is_limited_to_official_mimo_host(monkeypatch):
    monkeypatch.setattr(settings, "WEB_LLM_API_KEY", "")
    monkeypatch.setattr(settings, "WEB_LLM_MODEL", "")
    chat = ModelConfig(kind="openai", provider="custom", base_url="https://other.example/v1",
                       api_key="other-provider-key", model="other-model")
    monkeypatch.setattr(type(settings), "chat_config", property(lambda _: chat))
    assert resolved_config()[2:] == ("", "")
    chat.base_url = "https://api.xiaomimimo.com/v1"
    assert resolved_config()[2:] == ("other-provider-key", "other-model")
    for target in ("https://other.example/v1", "http://api.xiaomimimo.com/v1",
                   "https://api.xiaomimimo.com:8443/v1"):
        monkeypatch.setattr(settings, "WEB_LLM_BASE_URL", target)
        assert resolved_config()[2:] == ("", "")
