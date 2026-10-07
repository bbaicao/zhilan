"""`GET/PUT /api/settings` 契约面（阶段 4 · 条目 `4.5.3`；开工指令包 §5 的 **A1–A15**）。

**为什么单列一个文件**：settings 域是「12 个顶层键 + 五段 + 三个独立端点」的大面，
而且它的验收断言（A1–A16）是**跨键、跨分支**的集合断言，混进 `test_chat_contract.py`
那种「一个端点一节」的文件会看不出覆盖面。

**夹具纪律**（开工指令包 §5 明文）：

- `GET/PUT /api/settings` **不碰数据库**（`settings_routes` 无 `Depends(get_db)`）⇒ 自建 ASGI 客户端；
- 落盘一律走**假 `.env`**（`env_store.ENV_PATH` → `tmp_path`）——**绝不许碰仓库真实 `.env`**，
  照 `tests/test_model_roles.py::fake_dotenv` 的写法（含「落到了真实路径上」的自证断言）；
- `tests/api/conftest.py` 的 `pin_rerank_off` / `pin_web_off` 是 **autouse**：要断言 `.env` 现值
  就读**文件**，别把钉当成产品行为。

**A16（全量零回归）不在本文件**：它由主会话在四域都完成后统一跑全量并逐条比对失败清单。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import env_store
from app.core.config import Settings, settings

from tests.api._sync_client import SyncClient, build_app

SVC_CHAT = "svc_chat"
SVC_EMBED = "svc_embed"
CHAT_KEY = "sk-leak0123456789abcdef"  # 前 8 位 `sk-leak0` 用来做「响应里不许出现 Key」的断言

TOP_KEYS = [
    "services",
    "roles",
    "active",
    "mode",
    "providers",
    "reasoning",
    "rerank",
    "web",
    "agent",
    "translate",
    "data",
    # 工作区根（专项工具升级-3 · 阶段 3 · 回归检查）：**顶层第 12 键**（与 upload/sqlite/skill 同族）。
    # ⚠️ **不得**塞进 `agent` 段 —— 那是四键冻结面（见 :275 的全等断言 + `K-回归检查`）。
    "workspace_root",
]
SERVICE_KEYS = [
    "id",
    "name",
    "provider",
    "base_url",
    "has_key",
    "models",
    "model_roles",
    "for_roles",
    "roles",
]
RERANK_KEYS = [
    "enabled",
    "provider",
    "provider_label",
    "model",
    "base_url",
    "effective_base_url",
    "has_key",
    "key_source",
    "models",
    "providers",
    "dependency_ok",
    "dependency_detail",
    "threshold",
    "effective",
    "blocked_by",
    "note",
]
RERANK_PROVIDER_KEYS = [
    "id",
    "label",
    "kind",
    "summary",
    "models",
    "default_model",
    "needs_key",
    "base_url_hint",
    "requirement",
    "cost",
    "note",
    "source",
    "dependency_ok",
    "dependency_detail",
]
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
ACTIVE_KEYS = ["configured", "service_id", "service_name", "provider", "model", "error"]
DATA_KEYS = ["chroma_dir", "chroma_path", "exists", "files", "size_bytes", "vectors", "notice"]

#: 运行时（本机 `.env`）带进来的扁平模型键：env 模式用例必须把它们清空，
#: 否则 `build_registry_from_legacy` 会认为「有可迁移内容」而把用例的判定带偏。
FLAT_KEYS = [
    "MODEL_PROVIDER",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "EMBED_PROVIDER",
    "EMBED_MODEL",
    "EMBED_BASE_URL",
    "EMBED_API_KEY",
    "VISION_MODEL",
]


def _services(*, embed_key: str = "sk-embed") -> list[dict]:
    return [
        {
            "id": SVC_CHAT,
            "name": "对话服务",
            "provider": "dashscope",
            "base_url": "https://example.invalid/v1",
            "api_key": CHAT_KEY,
            "models": ["m-chat", "m-vision"],
            "model_roles": {"m-chat": ["chat"]},
            "for_roles": ["chat", "vision"],
        },
        {
            "id": SVC_EMBED,
            "name": "向量服务",
            "provider": "dashscope",
            "base_url": "https://example.invalid/v1",
            "api_key": embed_key,
            "models": ["m-embed"],
            "model_roles": {"m-embed": ["embed"]},
            "for_roles": ["embed"],
        },
    ]


def _roles(*, with_rerank: dict | None = None) -> dict:
    out = {
        "chat": {"service_id": SVC_CHAT, "model": "m-chat"},
        "embed": {"service_id": SVC_EMBED, "model": "m-embed"},
        "vision": {"service_id": SVC_CHAT, "model": "m-vision"},
    }
    if with_rerank:
        out["rerank"] = with_rerank
    return out


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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
    """只挂 settings router 的干净 ASGI 应用（不碰 DB / lifespan）。"""
    from app.api.settings_routes import router

    return SyncClient(build_app(router))


@pytest.fixture
def wired(monkeypatch, fake_dotenv):
    """把 `settings` 单例钉成一个**可控**的注册表状态：两条服务 + 三角色绑定 + 重排关。"""
    monkeypatch.setattr(settings, "MODEL_SERVICES", _dump(_services()))
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump(_roles()))
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", False)
    monkeypatch.setattr(settings, "RAG_MIN_RERANK_SCORE", 0.2)
    monkeypatch.setattr(settings, "WEB_ENABLE", False)
    monkeypatch.setattr(settings, "DEEPL_API_KEY", "")
    return settings


def _read_env(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- A1：GET 200 + 12 键一个不少 -----------------------------------------------------------


def test_a1_get_settings_has_all_twelve_top_keys(client, wired):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert list(body.keys()) == TOP_KEYS, "顶层键必须与 02 §7.6 逐字一致（顺序也无妨，这里用全等钉住）"


# --- A2：服务 9 键齐 + 响应正文不含任何已存 Key -------------------------------------------------


def test_a2_services_have_nine_keys_and_never_leak_api_key(client, wired):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["services"]) == 2
    for service in body["services"]:
        assert list(service.keys()) == SERVICE_KEYS
        assert isinstance(service["has_key"], bool)
        assert service["has_key"] is True
    raw = resp.text
    assert CHAT_KEY[:8] not in raw, "响应正文里出现了已存 Key 的前 8 位"
    assert CHAT_KEY not in raw
    assert "sk-embed" not in raw
    assert "api_key" not in raw


# --- A3：rerank 16 键 / providers 14 键 / threshold 取配置值 -----------------------------------


def test_a3_rerank_sixteen_keys_and_provider_options_fourteen(client, wired, monkeypatch):
    resp = client.get("/api/settings")
    body = resp.json()
    rerank = body["rerank"]
    assert list(rerank.keys()) == RERANK_KEYS
    assert isinstance(rerank["providers"], list) and rerank["providers"], "providers 必须非空数组"
    for provider in rerank["providers"]:
        assert list(provider.keys()) == RERANK_PROVIDER_KEYS
    assert rerank["threshold"] == settings.RAG_MIN_RERANK_SCORE

    # ⚠️ **必须走 monkeypatch**（2026-09-20 独立只读审计实测）：原先这里是**裸赋值**
    # `settings.RAG_MIN_RERANK_SCORE = 0.37` —— `settings` 是**进程级单例**，
    # 那条赋值会**活过整个会话**，把后续用例（尤其按配置指纹断言的缓存用例，
    # 指纹里含 `rerank_min_score`）带偏。断言内容一个字没改，只是补上还原。
    monkeypatch.setattr(settings, "RAG_MIN_RERANK_SCORE", 0.37)
    assert client.get("/api/settings").json()["rerank"]["threshold"] == 0.37


def test_a3b_reasoning_web_agent_data_keys_complete(client, wired):
    """五段的键位齐（缺键 = 冻结前端弹窗崩，这是 A1 之外更要紧的一条）。

    ⚠️ **与专包 A14 的一处有意差异（已登记）**：`web.effective` / `web.blocked_by` 在本条
    **不做「恒 false / 恒非空」断言**。v2 的联网检索 api 渠道**已经实装并接线**（条目 4.1），
    在 `.env` 配置齐全时它是**真的能跑**——把它写成 `blocked_by = "尚未在本版本实现"` 是一句假话
    （那正是本条要消灭的那类缺陷的镜像）。这里的断言改为「形状 + 真值」：`providers` 非空（来自
    `app/agent/web/spec.py` 的真实登记表）、`channel_options` 非空、`blocked_by` 非空**当且仅当**
    真的缺 Key / 渠道未实装。
    """
    body = client.get("/api/settings").json()
    assert list(body["reasoning"].keys()) == REASONING_KEYS
    for key in ("enabled", "provider", "provider_label", "has_key", "providers", "max_results",
                "timeout", "effective", "blocked_by", "note"):
        assert key in body["web"], f"web 段缺必填键 {key}"
    assert len(body["web"]["providers"]) == 6
    assert [row["id"] for row in body["web"]["providers"]][0] == "duckduckgo"
    for row in body["web"]["providers"]:
        assert list(row.keys()) == [
            "id", "label", "summary", "needs_key", "key_hint", "cost", "note", "source",
        ]
    assert {row["id"] for row in body["web"]["channel_options"]} == {"api", "llm"}
    assert isinstance(body["web"]["effective"], bool)
    assert sorted(body["agent"]) == ["audit", "max_rounds", "mode", "modes"]
    assert set(body["translate"]) == {"has_key"}
    assert list(body["data"].keys()) == DATA_KEYS
    for role in ("chat", "embed", "vision"):
        assert list(body["active"][role].keys()) == ACTIVE_KEYS


# --- A4：R-1 未知服务商 ⇒ 400 且 .env 一个字节不变 --------------------------------------------


def test_a4_unknown_rerank_provider_is_400_and_env_untouched(client, wired, fake_dotenv):
    before = _read_env(fake_dotenv)
    resp = client.put("/api/settings", json={"rerank": {"enabled": True, "provider": "不存在的厂"}})
    assert resp.status_code == 400
    assert "未知的重排序服务商" in resp.json()["detail"]
    assert _read_env(fake_dotenv) == before


# --- A5：R-2 开着但没型号 ⇒ 400 -----------------------------------------------------------


def test_a5_rerank_enabled_without_model_is_400(client, wired, fake_dotenv):
    before = _read_env(fake_dotenv)
    resp = client.put("/api/settings", json={"rerank": {"enabled": True, "model": ""}})
    assert resp.status_code == 400
    assert "需要先填重排序型号" in resp.json()["detail"]
    assert _read_env(fake_dotenv) == before


# --- A6：R-3 bge 在 v2 恒不可用 -----------------------------------------------------------


def test_a6_bge_is_always_unavailable(client, wired, fake_dotenv):
    before = _read_env(fake_dotenv)
    resp = client.put(
        "/api/settings",
        json={"rerank": {"enabled": True, "provider": "bge", "model": "BAAI/bge-reranker-v2-m3"}},
    )
    assert resp.status_code == 400
    assert "本地重排序实现" in resp.json()["detail"]
    assert _read_env(fake_dotenv) == before

    bge = next(row for row in client.get("/api/settings").json()["rerank"]["providers"] if row["id"] == "bge")
    assert bge["dependency_ok"] is False
    assert bge["dependency_detail"]


# --- A7 + A8：口径 A 落盘（无 Key ⇒ 400；补 Key ⇒ 200 并写 MODEL_ROLES + RAG_ENABLE_RERANK）-----


def test_a7_a8_rerank_writes_role_and_switch_never_flat_keys(client, monkeypatch, fake_dotenv):
    monkeypatch.setattr(settings, "MODEL_SERVICES", _dump(_services(embed_key="")))
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump(_roles()))
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", False)

    payload = {"rerank": {"enabled": True, "provider": "dashscope", "model": "qwen3.7-text-rerank"}}
    before = _read_env(fake_dotenv)
    refused = client.put("/api/settings", json=payload)
    assert refused.status_code == 400
    assert "API Key" in refused.json()["detail"]
    assert _read_env(fake_dotenv) == before, "R-4 拒绝时不许落盘"

    ok = client.put("/api/settings", json={**payload, "rerank": {**payload["rerank"], "api_key": "sk-x"}})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["rerank"]["enabled"] is True
    assert body["rerank"]["effective"] is True, body["rerank"]["blocked_by"]
    assert body["roles"]["rerank"] == {"service_id": SVC_EMBED, "model": "qwen3.7-text-rerank"}

    env = _read_env(fake_dotenv)
    assert 'RAG_ENABLE_RERANK=true' in env
    roles = json.loads(_env_value(env, "MODEL_ROLES"))
    assert roles["rerank"] == {"service_id": SVC_EMBED, "model": "qwen3.7-text-rerank"}
    assert roles["chat"] == {"service_id": SVC_CHAT, "model": "m-chat"}, "别的角色不许被动"
    services = json.loads(_env_value(env, "MODEL_SERVICES"))
    embed = next(row for row in services if row["id"] == SVC_EMBED)
    assert embed["api_key"] == "sk-x", "api_key 非空 ⇒ 写进目标服务的 MODEL_SERVICES"
    for forbidden in ("RERANK_MODEL", "RERANK_PROVIDER", "RERANK_BASE_URL", "RERANK_API_KEY"):
        assert f"\n{forbidden}=" not in "\n" + env, f"口径 A 禁止写入扁平键 {forbidden}"


def _env_value(env_text: str, key: str) -> str:
    for line in env_text.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(f".env 里没有 {key}")


# --- A9：P5 不擦除 —— PUT roles（不带 rerank）不许把它抹掉 --------------------------------------


def test_a9_put_roles_keeps_existing_rerank_binding(client, monkeypatch, fake_dotenv):
    rerank_binding = {"service_id": SVC_EMBED, "model": "m-rerank"}
    monkeypatch.setattr(settings, "MODEL_SERVICES", _dump(_services()))
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump(_roles(with_rerank=rerank_binding)))

    resp = client.put(
        "/api/settings",
        json={
            "roles": {
                "chat": {"service_id": SVC_CHAT, "model": "m-chat2"},
                "vision": {"service_id": SVC_CHAT, "model": "m-vision"},
            }
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["roles"]["rerank"] == rerank_binding, "前端不发的 rerank 绑定被整表替换抹掉了"
    assert body["roles"]["chat"]["model"] == "m-chat2"
    env_roles = json.loads(_env_value(_read_env(fake_dotenv), "MODEL_ROLES"))
    assert env_roles["rerank"] == rerank_binding


def test_a9b_put_roles_rejects_unknown_role(client, wired):
    resp = client.put("/api/settings", json={"roles": {"nope": {"service_id": SVC_CHAT, "model": "m"}}})
    assert resp.status_code == 400
    assert "未知的模型角色" in resp.json()["detail"]


# --- A10：P7 留空 api_key = 保留原 Key -------------------------------------------------------


def test_a10_blank_api_key_keeps_stored_key(client, wired, fake_dotenv):
    resp = client.put(
        "/api/settings",
        json={
            "services": [
                {
                    "id": SVC_CHAT,
                    "name": "对话服务",
                    "provider": "dashscope",
                    "base_url": "https://example.invalid/v1",
                    "models": ["m-chat", "m-vision"],
                    "for_roles": ["chat", "vision"],
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    services = json.loads(_env_value(_read_env(fake_dotenv), "MODEL_SERVICES"))
    assert services[0]["api_key"] == CHAT_KEY, "留空 = 保留原 Key（不许清空、不许回显）"
    assert resp.json()["services"][0]["has_key"] is True
    assert CHAT_KEY[:8] not in resp.text


# --- A11：P6 重启不丢（纯配置层：重新构造 Settings 也读得到）---------------------------------------


def test_a11_rerank_survives_settings_reload(client, monkeypatch, fake_dotenv):
    monkeypatch.setattr(settings, "MODEL_SERVICES", _dump(_services()))
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump(_roles()))
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", False)
    ok = client.put(
        "/api/settings",
        json={"rerank": {"enabled": True, "provider": "dashscope", "model": "qwen3.7-text-rerank"}},
    )
    assert ok.status_code == 200, ok.text
    expected = ok.json()["roles"]["rerank"]

    monkeypatch.delenv("RAG_ENABLE_RERANK", raising=False)
    monkeypatch.delenv("MODEL_ROLES", raising=False)
    monkeypatch.delenv("MODEL_SERVICES", raising=False)
    reloaded = Settings(_env_file=fake_dotenv)
    assert reloaded.RAG_ENABLE_RERANK is True, "bool 键必须被还原成真 bool（`\"false\"` 为真的老坑）"
    assert reloaded.roles["rerank"] == expected


# --- A12：threshold 就是 RAG_MIN_RERANK_SCORE（既有 tests/retrieval/test_rerank.py 钉过滤行为）------


def test_a12_threshold_reflects_configured_value(client, monkeypatch, fake_dotenv):
    monkeypatch.setattr(settings, "RAG_MIN_RERANK_SCORE", 0.05)
    assert client.get("/api/settings").json()["rerank"]["threshold"] == 0.05
    monkeypatch.setattr(settings, "RAG_MIN_RERANK_SCORE", 0.8)
    assert client.get("/api/settings").json()["rerank"]["threshold"] == 0.8


# --- A13：mode="env" 分支 + 无可迁移内容时 400（不是 500、不是静默）-------------------------------


def test_a13_env_mode_and_rerank_without_services_is_400(client, monkeypatch, fake_dotenv):
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", "")
    for key in FLAT_KEYS:
        monkeypatch.setattr(settings, key, "")

    body = client.get("/api/settings").json()
    assert body["mode"] == "env"
    assert body["services"] == []
    assert body["roles"] == {}
    for role in ("chat", "embed", "vision"):
        assert body["active"][role]["configured"] is False

    before = _read_env(fake_dotenv)
    resp = client.put(
        "/api/settings",
        json={"rerank": {"enabled": True, "provider": "dashscope", "model": "qwen3.7-text-rerank"}},
    )
    assert resp.status_code == 400
    assert "请先在设置页配置一条向量模型服务" in resp.json()["detail"]
    assert _read_env(fake_dotenv) == before, "无可迁移内容时不许写盘"


# --- A14：未实装段的明确 400（web / data 两段）-----------------------------------------------


@pytest.mark.parametrize(
    "segment,payload,needle",
    [
        ("web", {"enabled": True, "provider": "tavily"}, "联网检索"),
        ("data", {"chroma_dir": "D:/x", "migrate": True}, "向量库位置"),
    ],
)
def test_a14_unimplemented_segments_are_explicit_400(client, wired, fake_dotenv, segment, payload, needle):
    before = _read_env(fake_dotenv)
    resp = client.put("/api/settings", json={segment: payload})
    assert resp.status_code == 400, f"{segment} 段应明确 400，实际 {resp.status_code}"
    detail = resp.json()["detail"]
    assert isinstance(detail, str) and needle in detail
    assert _read_env(fake_dotenv) == before, f"{segment} 段被拒绝时不许落盘"


def test_a14_reasoning_segment_is_now_real(client, wired, fake_dotenv, monkeypatch):
    """**与专包 §5 A14 的一处有意差异（已登记）**：`reasoning` 段**不再是**「未实装段」。

    「推理等级登记表回流」条目把厂商思考参数登记表与 `CHAT_REASONING` 写通道都落地了 ⇒
    再回 400 就是**假话**（界面上那些选项是真的能生效的）。本条钉住两件事：

    1. `wired` 夹具下对话型号是 `m-chat`（**未登记**型号）⇒ 400，且**理由是真话**
       （「没有登记官方思考参数」），并指向手写通道；
    2. 换成**已登记**型号（`mimo-v2.5` + provider `mimo`）⇒ 200 并真的落盘。

    `m-chat` 走 400 不是回归，而是本条目的**第 3 条非法写入**（型号未登记），
    逐项断言在 `tests/api/test_settings_reasoning.py`。
    """
    before = _read_env(fake_dotenv)
    refused = client.put(
        "/api/settings", json={"reasoning": {"param": "thinking.type", "value": "enabled"}}
    )
    assert refused.status_code == 400, refused.text
    assert "没有登记官方思考参数" in refused.json()["detail"]
    assert _read_env(fake_dotenv) == before, "非法写入时不许落盘"

    monkeypatch.setattr(
        settings,
        "MODEL_SERVICES",
        _dump(
            [
                {
                    "id": SVC_CHAT,
                    "name": "对话服务",
                    "provider": "mimo",
                    "base_url": "https://example.invalid/v1",
                    "api_key": CHAT_KEY,
                    "models": ["mimo-v2.5"],
                    "model_roles": {"mimo-v2.5": ["chat"]},
                    "for_roles": ["chat"],
                }
            ]
        ),
    )
    monkeypatch.setattr(
        settings, "MODEL_ROLES", _dump({"chat": {"service_id": SVC_CHAT, "model": "mimo-v2.5"}})
    )
    ok = client.put(
        "/api/settings", json={"reasoning": {"param": "thinking.type", "value": "disabled"}}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["reasoning"]["current"] == "disabled"
    env = _read_env(fake_dotenv)
    assert '"param":"thinking.type"' in env and '"value":"disabled"' in env
    assert '"model":"mimo-v2.5"' in env


def test_a14b_agent_and_translate_are_real(client, wired, fake_dotenv):
    """**与专包 §4.6 的有意差异（已登记）**：`agent` / `translate` 在 v2 **是真实现**，
    不是「未实装段」。理由：`AGENT_*` 三条键的消费方已在 4.1/4.2/4.3 落地；
    `DEEPL_API_KEY` + `POST /api/translate` 由本条目实装（冻结前端 `DeepLDialog.tsx:35`
    就是走 `PUT {translate:{api_key}}` 保存的）。若按专包回 400，冻结前端的两个入口直接死掉，
    且文案「尚未在本版本实装」对已交付的能力是**假话**。
    """
    ok = client.put("/api/settings", json={"agent": {"mode": "react", "max_rounds": 3, "audit": True}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["agent"] == {
        "mode": "react",
        "max_rounds": 3,
        "audit": True,
        "modes": ok.json()["agent"]["modes"],
    }
    env = _read_env(fake_dotenv)
    assert "AGENT_MODE=react" in env and "AGENT_MAX_ROUNDS=3" in env and "AGENT_ENABLE_AUDIT=true" in env

    translated = client.put("/api/settings", json={"translate": {"api_key": "deepl-key-123"}})
    assert translated.status_code == 200, translated.text
    assert translated.json()["translate"]["has_key"] is True
    assert "deepl-key-123" not in translated.text, "Key 本体不许回传"
    assert "DEEPL_API_KEY=deepl-key-123" in _read_env(fake_dotenv)

    # 留空 = 保留现值，一个字都不写
    before = _read_env(fake_dotenv)
    assert client.put("/api/settings", json={"translate": {"api_key": ""}}).status_code == 200
    assert _read_env(fake_dotenv) == before


@pytest.mark.parametrize(
    "agent,needle",
    [
        ({"mode": "nope"}, "未知的 Agent 模式"),
        ({"max_rounds": 0}, "1..12"),
        ({"max_rounds": 13}, "1..12"),
        ({"max_rounds": "3"}, "1..12"),
        ({"max_rounds": True}, "1..12"),
    ],
)
def test_a14c_agent_validation_is_400_chinese(client, wired, fake_dotenv, agent, needle):
    before = _read_env(fake_dotenv)
    resp = client.put("/api/settings", json={"agent": agent})
    assert resp.status_code == 400
    assert needle in resp.json()["detail"]
    assert _read_env(fake_dotenv) == before


def test_bug_028_put_never_auto_repairs_missing_role(client, monkeypatch, fake_dotenv):
    """`回归修复` 的**行为**栅栏（缺陷行点名的加强项）。

    「换个名字的 `repair_missing_role_bindings` 照样能过名字栅栏」——所以这里钉**行为**：
    注册表在、`vision` 没绑定时，**任何** `PUT` 分支都不许凭空给 `vision` 补一条绑定
    （补绑必然要改写 `MODEL_ROLES`，而那正是被删掉的地雷）。
    """
    roles = _roles()
    roles.pop("vision")
    monkeypatch.setattr(settings, "MODEL_SERVICES", _dump(_services()))
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump(roles))

    resp = client.put(
        "/api/settings",
        json={"roles": {"chat": {"service_id": SVC_CHAT, "model": "m-chat"}}},
    )
    assert resp.status_code == 200, resp.text
    assert "vision" not in resp.json()["roles"], "PUT 凭空补出了 vision 绑定（自动补绑复活）"
    env_roles = json.loads(_env_value(_read_env(fake_dotenv), "MODEL_ROLES"))
    assert "vision" not in env_roles, "落盘的 MODEL_ROLES 里被自动补了 vision 绑定"


def test_get_settings_with_live_env_state(client, fake_dotenv):
    """I5 冒烟：**不钉任何配置**（就是本机 `.env` 的真实状态）时 `GET` 也要 200 + 12 键。

    这一条不 monkeypatch `MODEL_SERVICES` / `MODEL_ROLES`：本机 `.env` 里可能有遗留的
    `RERANK_*`、可能有指向已删服务的悬挂绑定——`GET` 侧一切读取都必须不抛（I5），
    这正是设置页启动时那一次调用的真实处境。
    """
    resp = client.get("/api/settings")
    assert resp.status_code == 200, resp.text
    assert list(resp.json().keys()) == TOP_KEYS


# --- 旧扁平段（`chat` / `embed` / `vision`）：冻结前端在 env 模式真的会发 ---------------------------


def test_flat_chat_unknown_provider_is_400_zero_disk(client, wired, fake_dotenv):
    """**live 契约用例 `test_settings_put_unknown_provider_400` 的同一条**。

    冻结前端 `api.ts:1173` 的 `SettingsIn` 有 `chat?: {provider,base_url,model,api_key}`
    （注释：仅在 `mode="env"` 时使用），`App.tsx:498-501` 顶栏切模型时真的会发它。
    若 `SettingsIn` 把它当未知字段吞掉 ⇒ 回 200 ⇒ 未知厂商静默通过（live 红）。
    """
    before = _read_env(fake_dotenv)
    resp = client.put("/api/settings", json={"chat": {"provider": "nonexistent_provider_xyz"}})
    assert resp.status_code == 400
    assert "未知的模型厂商" in resp.json()["detail"]
    assert _read_env(fake_dotenv) == before


def test_flat_chat_writes_flat_keys_and_keeps_key(client, wired, fake_dotenv, monkeypatch):
    """env 模式下切模型走的就是这条路：写 `LLM_MODEL`，`api_key` 缺省 ⇒ 一个字节都不写。"""
    monkeypatch.setattr(settings, "MODEL_PROVIDER", "dashscope")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://flat.invalid/v1")
    monkeypatch.setattr(settings, "LLM_API_KEY", "sk-flat-old")
    monkeypatch.setattr(settings, "LLM_MODEL", "m-old")

    resp = client.put("/api/settings", json={"chat": {"model": "m-new"}})
    assert resp.status_code == 200, resp.text
    env = _read_env(fake_dotenv)
    assert "LLM_MODEL=m-new" in env
    assert "LLM_BASE_URL=https://flat.invalid/v1" in env
    assert "LLM_API_KEY" not in env, "未传 api_key ⇒ 保留现值，不是写空"

    # 传了非空 api_key 才写
    assert client.put("/api/settings", json={"chat": {"api_key": "sk-flat-new"}}).status_code == 200
    assert "LLM_API_KEY=sk-flat-new" in _read_env(fake_dotenv)


def test_flat_vision_and_embed_segments(client, wired, fake_dotenv, monkeypatch):
    monkeypatch.setattr(settings, "VISION_MODEL", "m-vision-old")
    monkeypatch.setattr(settings, "EMBED_PROVIDER", "dashscope")
    monkeypatch.setattr(settings, "EMBED_BASE_URL", "https://embed.invalid/v1")
    monkeypatch.setattr(settings, "EMBED_MODEL", "m-embed-old")

    assert client.put("/api/settings", json={"vision": {"model": "m-vision-new"}}).status_code == 200
    assert "VISION_MODEL=m-vision-new" in _read_env(fake_dotenv)
    assert client.put("/api/settings", json={"embed": {"model": "m-embed-new"}}).status_code == 200
    env = _read_env(fake_dotenv)
    assert "EMBED_MODEL=m-embed-new" in env and "VISION_MODEL=m-vision-new" in env


# --- A15：幂等往返 ---------------------------------------------------------------------


def test_a15_put_is_idempotent(client, wired, fake_dotenv):
    payload = {
        "services": [
            {
                "id": SVC_CHAT,
                "name": "对话服务",
                "provider": "dashscope",
                "base_url": "https://example.invalid/v1",
                "models": ["m-chat", "m-vision"],
                "for_roles": ["chat", "vision"],
            }
        ],
        "roles": {"chat": {"service_id": SVC_CHAT, "model": "m-chat"}},
    }
    first = client.put("/api/settings", json=payload)
    assert first.status_code == 200, first.text
    env_after_first = _read_env(fake_dotenv)
    second = client.put("/api/settings", json=payload)
    assert second.status_code == 200, second.text
    assert second.json() == first.json(), "同一份 payload 连打两次，响应必须逐字相同（for_roles 不许漂移）"
    env_after_second = _read_env(fake_dotenv)
    assert _env_value(env_after_second, "MODEL_SERVICES") == _env_value(env_after_first, "MODEL_SERVICES")
    assert _env_value(env_after_second, "MODEL_ROLES") == _env_value(env_after_first, "MODEL_ROLES")


def test_a15b_empty_payload_does_not_touch_env(client, wired, fake_dotenv):
    before = _read_env(fake_dotenv)
    resp = client.put("/api/settings", json={})
    assert resp.status_code == 200
    assert _read_env(fake_dotenv) == before


# --- I5 / 回归修复：读侧一切不抛 + 缺绑定显式可见 ----------------------------------------------


def test_read_side_never_throws_on_broken_binding(client, monkeypatch, fake_dotenv):
    """绑定指向已删服务：`GET` **仍 200**，原因写进 `active.*.error`（I5）。"""
    broken = _roles()
    broken["chat"] = {"service_id": "svc_gone", "model": "m-chat"}
    monkeypatch.setattr(settings, "MODEL_SERVICES", _dump(_services()))
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump(broken))

    resp = client.get("/api/settings")
    assert resp.status_code == 200
    chat = resp.json()["active"]["chat"]
    assert chat["configured"] is False
    assert "服务" in chat["error"] and chat["error"]


def test_bug_028_missing_role_binding_is_visible(client, monkeypatch, fake_dotenv):
    """`回归修复`：注册表在、角色缺 ⇒ `active` 标注原因 + `roles` 如实缺 + 启动日志。

    **禁止**据此恢复「自动改写 `MODEL_ROLES`」类实现 —— 见 `config.py:699-702` 的注释与
    `tests/test_model_roles.py::test_repair_missing_role_bindings_stays_deleted`。
    """
    from app.api.settings_routes import missing_role_bindings

    roles = _roles()
    roles.pop("vision")
    monkeypatch.setattr(settings, "MODEL_SERVICES", _dump(_services()))
    monkeypatch.setattr(settings, "MODEL_ROLES", _dump(roles))

    body = client.get("/api/settings").json()
    assert "vision" not in body["roles"], "roles 如实缺，不许悄悄补一条"
    vision = body["active"]["vision"]
    assert vision["configured"] is False
    assert "还没有绑定服务" in vision["error"]
    assert missing_role_bindings() == ["vision"]

    import logging

    from app.api import settings_routes

    records: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record):  # noqa: ANN001
            records.append(record.getMessage())

    logger = logging.getLogger(settings_routes.__name__)
    handler = _Handler()
    logger.addHandler(handler)
    try:
        settings_routes.log_missing_role_bindings()
    finally:
        logger.removeHandler(handler)
    assert any("视觉" in message for message in records), records
