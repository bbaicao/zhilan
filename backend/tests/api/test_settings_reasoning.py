"""`GET/PUT /api/settings` 的 `reasoning` 段（A8–A11；开工包 §5）。

**为什么单列一个文件**：`test_settings_contract.py` 是大面契约（12 顶层键、五段、三个独立
端点），本文件只钉这一段「读得到 / 写得进 / 写得错」，并含**唯一**一条写通道的四类拒绝。

夹具纪律与 `test_settings_contract.py` 逐字一致（照抄，不改造那个文件）：

- 本 router **不碰数据库** ⇒ 自建 ASGI 客户端；
- 落盘一律走**假 `.env`**（`env_store.ENV_PATH` → `tmp_path`），**绝不许碰仓库真实 `.env`**；
- 断言「非法写入时 `.env` 一字未动」时**读文件**，不看 `.env` 的现值。

⚠️ 本机真实 `.env` 里**确实有** `CHAT_REASONING`（老仓留下的合法用户设置）。本文件的
所有用例都显式 `monkeypatch.setattr(settings, "CHAT_REASONING", ...)`，不依赖它。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import env_store
from app.core.config import Settings, settings

from tests.api._sync_client import SyncClient, build_app

SVC_CHAT = "svc_chat"
SVC_GONE = "svc_gone"
CHAT_KEY = "sk-reasoning0123456789"

REASONING_KEYS = [
    "registered",
    "vendor",
    "param",
    "kind",
    "options",
    "minimum",
    "maximum",
    "default",
    "default_label",
    "current",
    "stale",
    "manual",
    "manual_active",
    "note",
    "source",
]


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json(raw: str):
    return json.loads(raw)


def _mimo_services() -> str:
    return _dump(
        [
            {
                "id": SVC_CHAT,
                "name": "小米",
                "provider": "mimo",
                "base_url": "https://example.invalid/v1",
                "api_key": CHAT_KEY,
                "models": ["mimo-v2.5", "mimo-v2.5-asr"],
                "model_roles": {"mimo-v2.5": ["chat"]},
                "for_roles": ["chat"],
            }
        ]
    )


@pytest.fixture
def fake_dotenv(tmp_path, monkeypatch) -> Path:
    """把 `.env` 指到 `tmp_path` 下的假文件（**绝不许碰仓库真实 .env**）。"""
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(env_store, "ENV_PATH", path)
    assert env_store.ENV_PATH == path
    assert env_store.ENV_PATH != env_store.ROOT_DIR / ".env", "假 .env 落到了仓库真实路径上"
    return path


@pytest.fixture
def client():
    from app.api.settings_routes import router

    return SyncClient(build_app(router))


@pytest.fixture
def mimo(monkeypatch, fake_dotenv):
    """注册表 = 一条 mimo 服务；`chat` 绑 `mimo-v2.5`（**已登记**型号）；`CHAT_REASONING` 空。"""
    monkeypatch.setattr(settings, "MODEL_SERVICES", _mimo_services())
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump({"chat": {"service_id": SVC_CHAT, "model": "mimo-v2.5"}}))
    monkeypatch.setattr(settings, "CHAT_REASONING", "")
    return settings


def _read_env(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- A8：GET 15 键一个不少 + 已登记型号的六个字段全对 -------------------------------------------


def test_a8_get_reasoning_has_all_fifteen_keys(client, mimo):
    resp = client.get("/api/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()["reasoning"]
    assert list(body.keys()) == REASONING_KEYS, "15 键一个不少（缺键 = 冻结前端下拉显示空值）"


def test_a8b_get_reasoning_reflects_registered_model(client, mimo):
    body = client.get("/api/settings").json()["reasoning"]
    assert body["registered"] is True
    assert body["vendor"] == "小米 MiMo"
    assert body["param"] == "thinking.type"
    assert body["kind"] == "enum"
    assert [row["value"] for row in body["options"]] == ["enabled", "disabled"]
    assert [row["label"] for row in body["options"]] == ["enabled", "disabled"]
    assert [row["hint"] for row in body["options"]] == ["开启思考（厂商默认）", "关闭思考，直接作答"]
    assert body["default"] == "enabled"
    assert body["current"] is None, "没设过 ⇒ current 为空（界面显示「厂商默认」）"
    assert body["stale"] is False
    assert body["manual"] is None and body["manual_active"] is False
    assert body["source"].startswith("https://")
    assert body["note"], "厂商提示文案必须给出来"


def test_a8c_get_reasoning_shows_stored_value_and_stale(client, mimo, monkeypatch):
    monkeypatch.setattr(
        settings, "CHAT_REASONING", '{"param":"thinking.type","value":"disabled","model":"mimo-v2.5"}'
    )
    body = client.get("/api/settings").json()["reasoning"]
    assert body["current"] == "disabled"
    assert body["stale"] is False

    # 换成**别的**型号（同厂商、同参数）⇒ 旧值「不属于本型号」，界面要说明而不是静默
    monkeypatch.setattr(
        settings, "MODEL_ROLES", _dump({"chat": {"service_id": SVC_CHAT, "model": "mimo-v2.5-pro"}})
    )
    body = client.get("/api/settings").json()["reasoning"]
    assert body["current"] is None
    assert body["stale"] is True
    assert "已不生效" in body["note"], body["note"]


def test_a8d_get_reasoning_unregistered_model_uses_manual_channel(client, mimo, monkeypatch):
    monkeypatch.setattr(
        settings, "MODEL_SERVICES",
        _dump([{**_json(_mimo_services())[0], "models": ["m-chat"], "model_roles": {"m-chat": ["chat"]}}]),
    )
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump({"chat": {"service_id": SVC_CHAT, "model": "m-chat"}}))
    monkeypatch.setattr(settings, "CHAT_REASONING", '{"param":"thinking.type","value":"disabled"}')

    body = client.get("/api/settings").json()["reasoning"]
    assert body["registered"] is False
    assert body["options"] == []
    assert body["manual"] == {"param": "thinking.type", "value": "disabled"}
    assert body["manual_active"] is True, "手写值正在生效 —— 界面必须如实说，否则用户以为没读到"
    assert "CHAT_REASONING" in body["note"]

    # 写给别的型号的手写值：不发，且 note 里说明
    monkeypatch.setattr(
        settings, "CHAT_REASONING", '{"param":"thinking.type","value":"disabled","model":"mimo-v2.5"}'
    )
    body = client.get("/api/settings").json()["reasoning"]
    assert body["manual"] is None and body["manual_active"] is False
    assert "写给别的型号" in body["note"], body["note"]


def test_a8e_get_reasoning_without_chat_binding(client, monkeypatch, fake_dotenv):
    """没绑定对话模型 ⇒ 全空 + 「还没有绑定对话模型。」，且**仍然 200 + 15 键**（I5）。"""
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", "")
    for key in ("MODEL_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL"):
        monkeypatch.setattr(settings, key, "")
    monkeypatch.setattr(settings, "CHAT_REASONING", "")

    resp = client.get("/api/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()["reasoning"]
    assert list(body.keys()) == REASONING_KEYS
    assert body["registered"] is False and body["options"] == []
    assert body["note"] == "还没有绑定对话模型。"


def test_a8f_get_reasoning_survives_broken_binding(client, monkeypatch, fake_dotenv):
    """绑定指向**已删服务**（`chat_config` 抛 `ValueError`）⇒ 读侧仍 200（I5）。"""
    monkeypatch.setattr(settings, "MODEL_SERVICES", _mimo_services())
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump({"chat": {"service_id": SVC_GONE, "model": "mimo-v2.5"}}))
    monkeypatch.setattr(settings, "CHAT_REASONING", '{"param":"thinking.type","value":"disabled"}')

    resp = client.get("/api/settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()["reasoning"]
    assert list(body.keys()) == REASONING_KEYS
    assert body["registered"] is False
    assert body["note"] == "还没有绑定对话模型。"


# --- A9：PUT 合法写入 ⇒ 200 + current + `.env` 有该键 + 响应仍是 12 顶层键 -------------------------


def test_a9_put_reasoning_writes_env_and_mirrors_memory(client, mimo, fake_dotenv):
    resp = client.put("/api/settings", json={"reasoning": {"param": "thinking.type", "value": "disabled"}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 专项工具升级-3 · 阶段 3 · 回归检查：顶层键 **11 → 12**（追加 `workspace_root`，落**顶层**、
    # 不进 `agent` 段）。⚠️ 这是本仓**第 3 处**「settings 顶层键全等」断言（包 `D-回归检查` 列了 8 处
    # 「11 个顶层键」措辞，但 `D-回归检查` 的例外清单只列了 `test_settings_contract.py` 的 `:209`/`:625`
    # 两条 ⇒ **包漏登本处**，由执行期全量读数暴露；已登记为例外清单第 7 处）。
    # 口径不变：**只换值不换形状**，故仍用全等钉住，只是键集随契约 v1.0.8 扩容。
    assert list(body.keys()) == [
        "services", "roles", "active", "mode", "providers", "reasoning",
        "rerank", "web", "agent", "translate", "data", "workspace_root",
    ], "响应仍是 12 顶层键（形状冻结，只换值不换形状）"
    assert body["reasoning"]["current"] == "disabled"
    assert body["reasoning"]["registered"] is True

    env = _read_env(fake_dotenv)
    assert env.count("CHAT_REASONING=") == 1
    stored = '{"param":"thinking.type","value":"disabled","model":"mimo-v2.5"}'
    assert stored in env
    assert settings.CHAT_REASONING == stored, "落盘成功后才改内存（内存与磁盘同源）"

    # 落盘后重载：新 `Settings` 也读得到（进程重启不丢）
    reloaded = Settings(_env_file=fake_dotenv)
    assert reloaded.CHAT_REASONING == stored


def test_a9b_put_reasoning_bool_value_for_qwen(client, fake_dotenv, monkeypatch):
    """bool 型取值（`enable_thinking`）写进 `.env` 的是 JSON 的 `false`，不是字符串 `"false"`。"""
    monkeypatch.setattr(
        settings,
        "MODEL_SERVICES",
        _dump(
            [
                {
                    "id": SVC_CHAT,
                    "name": "百炼",
                    "provider": "dashscope",
                    "base_url": "https://example.invalid/v1",
                    "api_key": CHAT_KEY,
                    "models": ["qwen-plus"],
                    "model_roles": {"qwen-plus": ["chat"]},
                    "for_roles": ["chat"],
                }
            ]
        ),
    )
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump({"chat": {"service_id": SVC_CHAT, "model": "qwen-plus"}}))
    monkeypatch.setattr(settings, "CHAT_REASONING", "")

    resp = client.put("/api/settings", json={"reasoning": {"param": "enable_thinking", "value": False}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["reasoning"]["current"] is False
    assert '"value":false' in _read_env(fake_dotenv)
    # 字符串 "true" 也收（界面可能从 JSON 里拿到字符串）
    ok = client.put("/api/settings", json={"reasoning": {"param": "enable_thinking", "value": "true"}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["reasoning"]["current"] is True


# --- A10：三种非法写入 ⇒ 400 + `.env` 一字未动 ------------------------------------------------


def test_a10a_put_reasoning_wrong_param_is_400_zero_disk(client, mimo, fake_dotenv):
    before = _read_env(fake_dotenv)
    resp = client.put("/api/settings", json={"reasoning": {"param": "enable_thinking", "value": True}})
    assert resp.status_code == 400, resp.text
    assert "thinking.type" in resp.json()["detail"]
    assert _read_env(fake_dotenv) == before


def test_a10b_put_reasoning_value_out_of_enum_is_400_zero_disk(client, mimo, fake_dotenv):
    before = _read_env(fake_dotenv)
    resp = client.put("/api/settings", json={"reasoning": {"param": "thinking.type", "value": "高"}})
    assert resp.status_code == 400, resp.text
    assert "取值不合法" in resp.json()["detail"]
    assert _read_env(fake_dotenv) == before


def test_a10c_put_reasoning_unregistered_model_is_400_zero_disk(client, fake_dotenv, monkeypatch):
    monkeypatch.setattr(
        settings,
        "MODEL_SERVICES",
        _dump(
            [
                {
                    "id": SVC_CHAT,
                    "name": "某厂",
                    "provider": "mimo",
                    "base_url": "https://example.invalid/v1",
                    "api_key": CHAT_KEY,
                    "models": ["mimo-v2.5-asr"],
                    "model_roles": {"mimo-v2.5-asr": ["chat"]},
                    "for_roles": ["chat"],
                }
            ]
        ),
    )
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump({"chat": {"service_id": SVC_CHAT, "model": "mimo-v2.5-asr"}}))
    monkeypatch.setattr(settings, "CHAT_REASONING", "")

    before = _read_env(fake_dotenv)
    resp = client.put("/api/settings", json={"reasoning": {"param": "thinking.type", "value": "disabled"}})
    assert resp.status_code == 400, resp.text
    assert "没有登记官方思考参数" in resp.json()["detail"]
    assert "CHAT_REASONING" in resp.json()["detail"], "拒绝时必须指出手写通道还在"
    assert _read_env(fake_dotenv) == before


def test_a10d_put_reasoning_without_chat_model_is_400_zero_disk(client, fake_dotenv, monkeypatch):
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", "")
    for key in ("MODEL_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL"):
        monkeypatch.setattr(settings, key, "")
    monkeypatch.setattr(settings, "CHAT_REASONING", "")

    before = _read_env(fake_dotenv)
    resp = client.put("/api/settings", json={"reasoning": {"param": "thinking.type", "value": "disabled"}})
    assert resp.status_code == 400, resp.text
    assert "请先绑定对话模型" in resp.json()["detail"]
    assert _read_env(fake_dotenv) == before


def test_a10e_put_reasoning_broken_binding_is_400_zero_disk(client, fake_dotenv, monkeypatch):
    """绑定指向已删服务 ⇒ `chat_config` 抛 `ValueError` ⇒ 400（不是 500、不是静默写入）。"""
    monkeypatch.setattr(settings, "MODEL_SERVICES", _mimo_services())
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump({"chat": {"service_id": SVC_GONE, "model": "mimo-v2.5"}}))
    monkeypatch.setattr(settings, "CHAT_REASONING", "")

    before = _read_env(fake_dotenv)
    resp = client.put("/api/settings", json={"reasoning": {"param": "thinking.type", "value": "disabled"}})
    assert resp.status_code == 400, resp.text
    assert "服务" in resp.json()["detail"]
    assert _read_env(fake_dotenv) == before


# --- A11：清空通道（设置页选「厂商默认」）------------------------------------------------------


def test_a11_put_empty_param_clears_setting(client, mimo, fake_dotenv, monkeypatch):
    monkeypatch.setattr(
        settings, "CHAT_REASONING", '{"param":"thinking.type","value":"disabled","model":"mimo-v2.5"}'
    )
    resp = client.put("/api/settings", json={"reasoning": {"param": "", "value": None}})
    assert resp.status_code == 200, resp.text
    body = resp.json()["reasoning"]
    assert body["current"] is None
    assert body["stale"] is False
    assert body["manual"] is None and body["manual_active"] is False
    env = _read_env(fake_dotenv)
    assert "CHAT_REASONING=" in env, "写的是**空串**（保留键、清空值），不是把键删掉"
    assert '"param"' not in env
    assert settings.CHAT_REASONING == ""


def test_a11b_clear_on_unregistered_model_never_400(client, fake_dotenv, monkeypatch):
    """清空**不看型号**：任何配置状态下都能把这条设置清掉（否则用户会被卡住）。"""
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", "")
    for key in ("MODEL_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL"):
        monkeypatch.setattr(settings, key, "")
    monkeypatch.setattr(settings, "CHAT_REASONING", '{"param":"thinking.type","value":"enabled"}')

    resp = client.put("/api/settings", json={"reasoning": {"param": "", "value": None}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["reasoning"]["registered"] is False
    assert settings.CHAT_REASONING == ""


# --- 与既有段同请求：不许互相污染 -----------------------------------------------------------------


def test_reasoning_coexists_with_other_segments_in_one_put(client, mimo, fake_dotenv):
    resp = client.put(
        "/api/settings",
        json={
            "agent": {"mode": "react"},
            "reasoning": {"param": "thinking.type", "value": "disabled"},
            "translate": {"api_key": "deepl-key-xyz"},
        },
    )
    assert resp.status_code == 200, resp.text
    env = _read_env(fake_dotenv)
    assert "AGENT_MODE=react" in env
    assert "DEEPL_API_KEY=deepl-key-xyz" in env
    assert '"value":"disabled"' in env
    assert resp.json()["agent"]["mode"] == "react"
    assert resp.json()["reasoning"]["current"] == "disabled"
