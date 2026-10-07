"""`/api/models` 三条端点（阶段 4 · 条目 `4.5.3`；02 §7.6「各独立端点行为」）。

- `POST /api/models`：按草稿（还没保存的地址 / Key）拉模型列表；给了 `service_id` 就用存好的 Key 兜底；
- `POST /api/web/llm/models`：联网检索大模型搜索的拉模型；草稿没填就回落已保存配置；
- `GET /api/models`：只列「对话角色绑定的那条供应商」里标了「对话」的型号；无标记回退全部已勾选型号。

**绝不出网**：`_fetch_models`（真打厂商 `/models` 的那一层）在本文件里一律换成桩，
并按草稿断言它收到的 `(base_url, api_key)`——否则用例会真连 `example.invalid` 上游。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api import settings_routes
from app.core import env_store
from app.core.config import settings

from tests.api._sync_client import SyncClient, build_app

SVC_CHAT = "svc_chat"
SVC_EMBED = "svc_embed"
CHAT_KEY = "sk-chat-secret0001"
BASE_URL = "https://example.invalid/v1"


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _services() -> list[dict]:
    return [
        {
            "id": SVC_CHAT,
            "name": "对话服务",
            "provider": "dashscope",
            "base_url": BASE_URL,
            "api_key": CHAT_KEY,
            "models": ["m-chat", "m-vision", "m-other"],
            "model_roles": {"m-chat": ["chat"], "m-vision": ["vision"]},
            "for_roles": ["chat", "vision"],
        },
        {
            "id": SVC_EMBED,
            "name": "向量服务",
            "provider": "dashscope",
            "base_url": BASE_URL,
            "api_key": "sk-embed-secret",
            "models": ["m-embed"],
            "model_roles": {"m-embed": ["embed"]},
            "for_roles": ["embed"],
        },
    ]


def _roles() -> dict:
    return {
        "chat": {"service_id": SVC_CHAT, "model": "m-chat"},
        "embed": {"service_id": SVC_EMBED, "model": "m-embed"},
        "vision": {"service_id": SVC_CHAT, "model": "m-vision"},
    }


@pytest.fixture
def fake_dotenv(tmp_path, monkeypatch) -> Path:
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(env_store, "ENV_PATH", path)
    assert env_store.ENV_PATH != env_store.ROOT_DIR / ".env", "假 .env 落到了仓库真实路径上"
    return path


@pytest.fixture
def client():
    return SyncClient(build_app(settings_routes.router))


@pytest.fixture
def wired(monkeypatch, fake_dotenv):
    monkeypatch.setattr(settings, "MODEL_SERVICES", _dump(_services()))
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump(_roles()))
    monkeypatch.setattr(settings, "WEB_LLM_BASE_URL", "")
    monkeypatch.setattr(settings, "WEB_LLM_API_KEY", "")
    return settings


@pytest.fixture
def fetch_log(monkeypatch):
    """把真出网的那一层换成桩，并记录每次调用（测试不出网的唯一手段）。"""
    calls: list[dict] = []

    async def _fake(base_url: str, api_key: str) -> list[str]:
        calls.append({"base_url": base_url, "api_key": api_key})
        return ["m-a", "m-b"]

    monkeypatch.setattr(settings_routes, "_fetch_models", _fake)
    return calls


# --- POST /api/models ------------------------------------------------------


def test_probe_models_uses_stored_key_when_draft_key_is_blank(client, wired, fetch_log):
    resp = client.post("/api/models", json={"service_id": SVC_CHAT})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"models": ["m-a", "m-b"]}
    assert fetch_log == [{"base_url": BASE_URL, "api_key": CHAT_KEY}]
    assert CHAT_KEY not in resp.text, "响应里不许出现 Key 本体"


def test_probe_models_draft_key_wins(client, wired, fetch_log):
    resp = client.post("/api/models", json={"service_id": SVC_CHAT, "api_key": "sk-draft"})
    assert resp.status_code == 200
    assert fetch_log[0]["api_key"] == "sk-draft"


def test_probe_models_unknown_service_is_400(client, wired, fetch_log):
    resp = client.post("/api/models", json={"service_id": "svc_gone"})
    assert resp.status_code == 400
    assert "服务不存在" in resp.json()["detail"]
    assert fetch_log == []


def test_probe_models_without_provider_is_400(client, wired, fetch_log):
    resp = client.post("/api/models", json={})
    assert resp.status_code == 400
    assert "请先选择模型厂商" in resp.json()["detail"]
    assert fetch_log == []


def test_probe_models_without_key_is_400(client, wired, fetch_log):
    resp = client.post("/api/models", json={"provider": "dashscope", "base_url": BASE_URL})
    assert resp.status_code == 400
    assert "API Key" in resp.json()["detail"]
    assert fetch_log == []


def test_probe_models_non_openai_provider_is_400(client, wired, fetch_log):
    resp = client.post("/api/models", json={"provider": "claude", "api_key": "sk-x"})
    assert resp.status_code == 400
    assert "不支持在线拉取模型列表" in resp.json()["detail"]
    assert fetch_log == []


# --- POST /api/web/llm/models ----------------------------------------------


def test_probe_web_models_requires_address_then_key(client, wired, fetch_log):
    resp = client.post("/api/web/llm/models", json={})
    assert resp.status_code == 400
    assert "请先填写接口地址" in resp.json()["detail"]

    resp = client.post("/api/web/llm/models", json={"base_url": BASE_URL})
    assert resp.status_code == 400
    assert "API Key" in resp.json()["detail"]
    assert fetch_log == []


def test_probe_web_models_uses_draft_then_falls_back_to_saved(client, wired, fetch_log, monkeypatch):
    resp = client.post("/api/web/llm/models", json={"base_url": BASE_URL, "api_key": "sk-draft"})
    assert resp.status_code == 200
    assert fetch_log[-1] == {"base_url": BASE_URL, "api_key": "sk-draft"}

    monkeypatch.setattr(settings, "WEB_LLM_BASE_URL", "https://saved.invalid/v1")
    monkeypatch.setattr(settings, "WEB_LLM_API_KEY", "sk-saved")
    resp = client.post("/api/web/llm/models", json={})
    assert resp.status_code == 200
    assert fetch_log[-1] == {"base_url": "https://saved.invalid/v1", "api_key": "sk-saved"}


# --- GET /api/models -------------------------------------------------------


def test_list_models_only_tagged_chat(client, wired, fetch_log):
    resp = client.get("/api/models")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["models"] == ["m-chat"], "有「对话」标记时不许把 vision / 其他型号混进来"
    assert body["service_id"] == SVC_CHAT
    assert body["service_name"] == "对话服务"
    assert body["current"] == "m-chat"
    assert body["note"] == ""
    assert fetch_log == [], "有绑定那条供应商时不该现场拉全量"


def test_list_models_falls_back_to_all_when_no_tag(client, wired, fetch_log, monkeypatch):
    services = _services()
    services[0]["model_roles"] = {}
    monkeypatch.setattr(settings, "MODEL_SERVICES", _dump(services))
    body = client.get("/api/models").json()
    assert body["models"] == ["m-chat", "m-vision", "m-other"]
    assert body["note"] == ""


def test_list_models_empty_pool_gives_note(client, wired, fetch_log, monkeypatch):
    services = _services()
    services[0]["models"] = []
    services[0]["model_roles"] = {}
    monkeypatch.setattr(settings, "MODEL_SERVICES", _dump(services))
    body = client.get("/api/models").json()
    assert body["models"] == []
    assert "还没勾选型号" in body["note"]


def test_list_models_env_mode_fetches_live(client, wired, fetch_log, monkeypatch):
    """没有服务注册表（还在手改 `.env`）时保持旧行为：现场拉一次。"""
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", "")
    monkeypatch.setattr(settings, "MODEL_PROVIDER", "dashscope")
    monkeypatch.setattr(settings, "LLM_BASE_URL", BASE_URL)
    monkeypatch.setattr(settings, "LLM_API_KEY", "sk-flat")
    monkeypatch.setattr(settings, "LLM_MODEL", "m-flat")

    resp = client.get("/api/models")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["models"] == ["m-a", "m-b"]
    assert body["service_id"] == "" and body["service_name"] == ""
    assert body["current"] == "m-flat"
    assert fetch_log == [{"base_url": BASE_URL, "api_key": "sk-flat"}]


def test_list_models_unconfigured_is_400_chinese(client, wired, fetch_log, monkeypatch):
    for key in ("MODEL_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.setattr(settings, key, "")
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", "")

    resp = client.get("/api/models")
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert isinstance(detail, str) and "对话模型" in detail
    assert fetch_log == []
