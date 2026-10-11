"""IA-008: candidate registry validation and one final settings write."""

from __future__ import annotations

import json

import pytest

from app.api import settings_routes as routes
from app.core.config import settings
from tests.api.test_settings_contract import (  # noqa: F401
    FLAT_KEYS,
    _dump,
    _env_value,
    client,
    fake_dotenv,
)

SERVICE_ID = "s2-vector"
MODEL = "qwen3.7-text-rerank"
FIELDS = ("MODEL_SERVICES", "MODEL_ROLES", "RAG_ENABLE_RERANK")


@pytest.fixture
def empty_registry(monkeypatch, fake_dotenv):
    for key in FLAT_KEYS:
        monkeypatch.setattr(settings, key, "")
    monkeypatch.setattr(settings, "MODEL_SERVICES", "[]")
    monkeypatch.setattr(settings, "MODEL_ROLES", "{}")
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", False)
    fake_dotenv.write_text("# S2 preserved\nUNRELATED=keep\n", encoding="utf-8")


def _combined(*, key="s2-dummy-key", base="https://s2.invalid/v1"):
    return {
        "services": [{
            "id": SERVICE_ID,
            "provider": "dashscope",
            "base_url": base,
            "api_key": key,
            "models": ["qwen3.7-text-embedding-flash"],
            "for_roles": ["embed"],
        }],
        "roles": {"embed": {"service_id": SERVICE_ID, "model": "qwen3.7-text-embedding-flash"}},
        "rerank": {"enabled": True, "provider": "dashscope", "model": MODEL},
    }


def _snapshot(path):
    return path.read_bytes(), {key: getattr(settings, key) for key in FIELDS}


def _spy_writes(monkeypatch):
    calls = []
    original = routes.persist_env

    def write(updates):
        calls.append(dict(updates))
        original(updates)

    monkeypatch.setattr(routes, "persist_env", write)
    return calls


def test_combined_candidate_registry_saves_once(client, empty_registry, fake_dotenv, monkeypatch):
    calls = _spy_writes(monkeypatch)
    response = client.put("/api/settings", json=_combined())
    assert response.status_code == 200, response.text
    assert response.json()["roles"]["rerank"] == {"service_id": SERVICE_ID, "model": MODEL}
    assert response.json()["rerank"]["effective"] is True
    assert len(calls) == 1
    assert set(calls[0]) == set(FIELDS)
    assert "s2-dummy-key" not in response.text
    stored = fake_dotenv.read_text(encoding="utf-8")
    assert "# S2 preserved\nUNRELATED=keep\n" in stored
    assert "RERANK_API_KEY=" not in stored
    assert settings.RAG_ENABLE_RERANK is True


@pytest.mark.parametrize("failure", ["key", "model", "provider", "role"])
def test_invalid_combined_candidate_is_atomic(
    client, empty_registry, fake_dotenv, monkeypatch, failure
):
    payload = _combined()
    if failure == "key":
        payload["services"][0]["api_key"] = ""
    elif failure == "role":
        payload["roles"]["embed"]["service_id"] = "missing"
    else:
        payload["rerank"][failure] = "" if failure == "model" else "not-a-provider"
    before = _snapshot(fake_dotenv)
    calls = _spy_writes(monkeypatch)
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 400, response.text
    assert _snapshot(fake_dotenv) == before
    assert calls == []


@pytest.mark.parametrize("blank", ["", "  ", None])
def test_blank_candidate_keys_keep_saved_key(
    client, empty_registry, fake_dotenv, monkeypatch, blank
):
    assert client.put("/api/settings", json=_combined()).status_code == 200
    payload = _combined(key=blank)
    payload["rerank"]["api_key"] = blank
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200, response.text
    stored = json.loads(_env_value(fake_dotenv.read_text(encoding="utf-8"), "MODEL_SERVICES"))
    assert stored[0]["api_key"] == "s2-dummy-key"
    assert "s2-dummy-key" not in response.text


def test_candidate_rerank_binding_and_model_are_used(
    client, empty_registry, fake_dotenv
):
    payload = _combined()
    payload["roles"]["rerank"] = {"service_id": SERVICE_ID, "model": MODEL}
    del payload["rerank"]["model"]
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["roles"]["rerank"] == {"service_id": SERVICE_ID, "model": MODEL}


def test_candidate_target_replaces_removed_old_binding(
    client, empty_registry, fake_dotenv
):
    original = _combined()
    assert client.put("/api/settings", json=original).status_code == 200
    payload = _combined(key="s2-replacement-key")
    payload["services"][0]["id"] = "s2-replacement"
    payload["roles"]["embed"]["service_id"] = "s2-replacement"
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["roles"]["rerank"]["service_id"] == "s2-replacement"


@pytest.mark.parametrize("base", ["http://127.0.0.1:8000/v1", "http://localhost:8000/v1"])
def test_local_candidate_needs_no_key(client, empty_registry, fake_dotenv, base):
    response = client.put("/api/settings", json=_combined(key="", base=base))
    assert response.status_code == 200, response.text
    stored = json.loads(_env_value(fake_dotenv.read_text(encoding="utf-8"), "MODEL_SERVICES"))
    assert stored[0]["api_key"] == ""
    assert response.json()["rerank"]["effective"] is True


@pytest.mark.parametrize("base", ["https://localhost.remote.invalid/v1", "https://remote.invalid/127.0.0.1"])
def test_remote_lookalike_requires_key(client, empty_registry, fake_dotenv, base):
    before = _snapshot(fake_dotenv)
    response = client.put("/api/settings", json=_combined(key="", base=base))
    assert response.status_code == 400, response.text
    assert "API Key" in response.json()["detail"]
    assert _snapshot(fake_dotenv) == before


@pytest.mark.parametrize("destination,expected", [
    ("https://remote.invalid/v1", 400),
    ("http://localhost:9000/v1", 200),
])
def test_rerank_key_validation_uses_final_address(
    client, empty_registry, fake_dotenv, monkeypatch, destination, expected
):
    service = _combined(key="", base="http://localhost:8000/v1")["services"][0]
    monkeypatch.setattr(settings, "MODEL_SERVICES", _dump([service]))
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump({
        "rerank": {"service_id": SERVICE_ID, "model": MODEL},
    }))
    before = _snapshot(fake_dotenv)
    response = client.put("/api/settings", json={
        "rerank": {"enabled": True, "base_url": destination},
    })
    assert response.status_code == expected, response.text
    if expected == 400:
        assert _snapshot(fake_dotenv) == before
    else:
        assert settings.services[0]["base_url"] == destination


def _legacy(monkeypatch):
    monkeypatch.setattr(settings, "EMBED_PROVIDER", "dashscope")
    monkeypatch.setattr(settings, "EMBED_BASE_URL", "https://legacy.invalid/v1")
    monkeypatch.setattr(settings, "EMBED_API_KEY", "s2-legacy-dummy")
    monkeypatch.setattr(settings, "EMBED_MODEL", "m-embed")
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump({"custom": {"field": "keep"}}))


def test_legacy_migration_has_one_final_write(
    client, empty_registry, fake_dotenv, monkeypatch
):
    _legacy(monkeypatch)
    calls = _spy_writes(monkeypatch)
    response = client.put("/api/settings", json={"rerank": {"enabled": True, "model": MODEL}})
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert set(calls[0]) == set(FIELDS)
    saved_roles = json.loads(settings.MODEL_ROLES)
    assert saved_roles["custom"] == {"field": "keep"}
    assert saved_roles["rerank"]["service_id"] == saved_roles["embed"]["service_id"]


def test_same_request_flat_candidate_is_migrated(
    client, empty_registry, fake_dotenv, monkeypatch
):
    calls = _spy_writes(monkeypatch)
    response = client.put("/api/settings", json={
        "embed": {
            "provider": "dashscope",
            "base_url": "https://new-legacy.invalid/v1",
            "api_key": "s2-flat-dummy",
            "model": "new-embed",
        },
        "rerank": {"enabled": True, "model": MODEL},
    })
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    embed_id = response.json()["roles"]["embed"]["service_id"]
    service = next(row for row in settings.services if row["id"] == embed_id)
    assert service["base_url"] == "https://new-legacy.invalid/v1"
    assert service["api_key"] == "s2-flat-dummy"
    assert response.json()["roles"]["embed"]["model"] == "new-embed"
    assert settings.EMBED_MODEL == "new-embed"


def test_legacy_invalid_request_has_no_migration_side_effect(
    client, empty_registry, fake_dotenv, monkeypatch
):
    _legacy(monkeypatch)
    before = _snapshot(fake_dotenv)
    calls = _spy_writes(monkeypatch)
    response = client.put("/api/settings", json={
        "rerank": {"enabled": True, "model": MODEL, "base_url": "https://other.invalid/v1"},
    })
    assert response.status_code == 400, response.text
    assert _snapshot(fake_dotenv) == before
    assert calls == []


def test_legacy_write_failure_does_not_change_memory(
    client, empty_registry, fake_dotenv, monkeypatch
):
    _legacy(monkeypatch)
    before = _snapshot(fake_dotenv)
    calls = []

    def fail(updates):
        calls.append(dict(updates))
        raise OSError("S2 controlled write failure")

    monkeypatch.setattr(routes, "persist_env", fail)
    response = client.put("/api/settings", json={"rerank": {"enabled": True, "model": MODEL}})
    assert response.status_code == 500, response.text
    assert _snapshot(fake_dotenv) == before
    assert len(calls) == 1


def test_explicit_empty_registry_is_not_revived_from_legacy(
    client, empty_registry, fake_dotenv, monkeypatch
):
    _legacy(monkeypatch)
    before = _snapshot(fake_dotenv)
    response = client.put("/api/settings", json={
        "services": [], "roles": {}, "rerank": {"enabled": True, "model": MODEL},
    })
    assert response.status_code == 400, response.text
    assert _snapshot(fake_dotenv) == before


@pytest.fixture
def s2_r1_bound(client, empty_registry, fake_dotenv, monkeypatch):
    payload = _combined()
    payload.pop("rerank")
    payload["roles"] = {
        role: {"service_id": SERVICE_ID, "model": f"model-{role}"}
        for role in ("chat", "embed", "vision", "rerank")
    }
    assert client.put("/api/settings", json=payload).status_code == 200
    original = json.loads(settings.MODEL_ROLES)
    original["translate"] = {"service_id": SERVICE_ID, "model": "custom-translate"}
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump(original))
    routes.persist_env({"MODEL_ROLES": settings.MODEL_ROLES})
    return original


def _s2_r1_readback(client, path, expected):
    from app.core.config import Settings

    known = {role: bound for role, bound in expected.items() if role in routes.ALL_ROLE_KEYS}
    response = client.get("/api/settings")
    assert response.status_code == 200
    assert response.json()["roles"] == known
    assert json.loads(settings.MODEL_ROLES) == expected
    stored = path.read_text(encoding="utf-8")
    saved = _env_value(stored, "MODEL_ROLES")
    assert json.loads(saved) == expected
    reloaded = Settings(
        _env_file=None, MODEL_ROLES=saved,
        MODEL_SERVICES=_env_value(stored, "MODEL_SERVICES"),
    )
    assert reloaded.roles == known
    assert reloaded.services == settings.services


@pytest.mark.parametrize("operation", ["empty-three", "empty-four", "delete-services"])
def test_s2_r1_clear_never_revives_known_bindings(
    client, s2_r1_bound, fake_dotenv, monkeypatch, operation
):
    if operation == "empty-three":
        payload = {"roles": {}}
        expected = {key: value for key, value in s2_r1_bound.items()
                    if key in ("rerank", "translate")}
    elif operation == "empty-four":
        payload = {"roles": {role: {} for role in routes.ALL_ROLE_KEYS}}
        expected = {"translate": s2_r1_bound["translate"]}
    else:
        payload = {"services": []}
        expected = {"translate": s2_r1_bound["translate"]}
    calls = _spy_writes(monkeypatch)
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert response.json()["roles"] == {
        key: value for key, value in expected.items() if key in routes.ALL_ROLE_KEYS
    }
    _s2_r1_readback(client, fake_dotenv, expected)
    if operation == "delete-services":
        assert response.json()["services"] == []
        assert response.json()["rerank"]["model"] == ""


@pytest.mark.parametrize("payload", [
    {},
    {"workspace_root": "s2-r1-workspace"},
    {"services": [_combined()["services"][0]]},
])
def test_s2_r1_omitted_roles_preserve_live_bindings(
    client, s2_r1_bound, fake_dotenv, monkeypatch, payload
):
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", "")
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200, response.text
    _s2_r1_readback(client, fake_dotenv, s2_r1_bound)


def test_s2_r1_clear_and_replace_in_one_save(
    client, s2_r1_bound, fake_dotenv, monkeypatch
):
    cleared = client.put("/api/settings", json={"services": [], "roles": {}})
    assert cleared.status_code == 200
    _s2_r1_readback(client, fake_dotenv, {"translate": s2_r1_bound["translate"]})
    payload = _combined()
    payload["services"][0]["id"] = "s2-r1-new"
    payload["roles"]["embed"]["service_id"] = "s2-r1-new"
    calls = _spy_writes(monkeypatch)
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    expected = {
        "embed": payload["roles"]["embed"],
        "rerank": {"service_id": "s2-r1-new", "model": MODEL},
        "translate": s2_r1_bound["translate"],
    }
    _s2_r1_readback(client, fake_dotenv, expected)
    assert response.json()["rerank"]["effective"] is True


@pytest.mark.parametrize("invalid", [
    {"roles": {"unexpected": {"service_id": SERVICE_ID, "model": "model"}}},
    {"roles": {"embed": {"service_id": "deleted", "model": "model"}}},
    {"services": [{**_combined()["services"][0], "provider": "invalid"}]},
    {"rerank": {"enabled": True, "model": MODEL, "base_url": "https://other.invalid/v1"}},
])
def test_s2_r1_invalid_update_keeps_bound_state_atomic(
    client, s2_r1_bound, fake_dotenv, monkeypatch, invalid
):
    before = _snapshot(fake_dotenv)
    calls = _spy_writes(monkeypatch)
    response = client.put("/api/settings", json=invalid)
    assert response.status_code == 400, response.text
    assert calls == []
    assert _snapshot(fake_dotenv) == before
    _s2_r1_readback(client, fake_dotenv, s2_r1_bound)


def test_s2_r1_legacy_preview_preserves_only_unknown_old_roles(
    client, empty_registry, fake_dotenv, monkeypatch
):
    _legacy(monkeypatch)
    stale = {
        role: {"service_id": "deleted-service", "model": f"old-{role}"}
        for role in ("chat", "vision", "rerank")
    }
    stale["custom"] = {"field": "keep"}
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump(stale))
    calls = _spy_writes(monkeypatch)
    response = client.put("/api/settings", json={"rerank": {"enabled": True, "model": MODEL}})
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert set(response.json()["roles"]) == {"embed", "rerank"}
    expected = {**response.json()["roles"], "custom": {"field": "keep"}}
    _s2_r1_readback(client, fake_dotenv, expected)
    assert all(bound["service_id"] != "deleted-service"
               for bound in response.json()["roles"].values())
