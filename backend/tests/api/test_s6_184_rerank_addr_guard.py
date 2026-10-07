"""S6 · 条目 184 取证：重排序接口地址必须与「向量模型」（embed）供应商一致。

产品侧只读口径（本批不改 app/**）：
- backend/app/api/settings_routes.py:397-408（resolve 侧）：base_url 非空 **且** 与
  rerank_target() 的目标服务地址不同 **且** 该服务当前是 embed 服务
  （settings.roles["embed"]["service_id"] == target["id"]）⇒ 400，文案两段拼接；
- backend/app/api/settings_routes.py:467-478（apply 侧兜底）：本次提交的 services 里该服务
  地址与 resolve 时记下的 plan["base_url"] 不同、且 plan["is_embed_service"] ⇒ 同一文案 400。

四条腿：① 与 embed 同址 ⇒ 放行；② embed 改址 ⇒ 400 且内存 / 落盘零副作用；
③ 独立（非 embed）重排序服务改址 ⇒ 放行并真的落盘；④ apply 兜底（本次提交顺带改 embed
地址、rerank 段不给 base_url ⇒ resolve 看的是 settings 旧址、拦不住，只有 apply 侧能拦）。

判别力：删掉 is_embed_service 判定 ⇒ ②④ 必红；400 文案改一个字 ⇒ ②④ 必红；
apply 侧守卫删掉 ⇒ ④ 必红（①③ 仍绿）。
"""

from __future__ import annotations

import json

from tests.api.test_settings_contract import (  # noqa: F401  复用 settings 契约夹具
    SVC_EMBED,
    _dump,
    _env_value,
    _read_env,
    _roles,
    _services,
    client,
    fake_dotenv,
    wired,
)

EMBED_URL = "https://example.invalid/v1"
NEW_URL = "https://rerank.example.invalid/v2"
SVC_RERANK = "svc_rerank"
GUARD_DETAIL = (
    "重排序的接口地址请与「向量模型」那条供应商保持一致"
    "（本版本沿用向量供应商，不单独托管地址）。"
)


def _rerank_payload(*, base_url: str | None) -> dict:
    """enabled=False + provider=dashscope + model="" ⇒ 只有地址分支能让请求失败。"""
    payload: dict = {"enabled": False, "provider": "dashscope", "model": ""}
    if base_url is not None:
        payload["base_url"] = base_url
    return payload


def test_same_address_as_embed_is_allowed(client, wired, fake_dotenv):  # noqa: F811
    """① 地址与「向量模型」那条一致 ⇒ 守卫不触发（200）。"""
    resp = client.put("/api/settings", json={"rerank": _rerank_payload(base_url=EMBED_URL)})
    assert resp.status_code == 200, resp.text


def test_embed_address_change_is_400_and_side_effect_free(client, wired, fake_dotenv):  # noqa: F811
    """② embed 是重排序的落点（无独立绑定）⇒ 改址 400，且 .env / 内存零副作用。"""
    before_env = _read_env(fake_dotenv)
    before_services = wired.MODEL_SERVICES
    before_roles = wired.MODEL_ROLES
    before_flag = wired.RAG_ENABLE_RERANK

    resp = client.put("/api/settings", json={"rerank": _rerank_payload(base_url=NEW_URL)})

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == GUARD_DETAIL
    assert _read_env(fake_dotenv) == before_env, "400 之后 .env 一个字节都不许变"
    assert wired.MODEL_SERVICES == before_services
    assert wired.MODEL_ROLES == before_roles
    assert wired.RAG_ENABLE_RERANK is before_flag


def test_non_embed_rerank_service_address_change_is_allowed(  # noqa: F811
    client, wired, fake_dotenv, monkeypatch
):
    """③ 重排序绑到一条**不在 embed 角色上**的服务 ⇒ 改址放行，并写进 .env。"""
    services = _services()
    services.append(
        {
            "id": SVC_RERANK,
            "name": "重排序服务",
            "provider": "dashscope",
            "base_url": EMBED_URL,
            "api_key": "sk-rerank",
            "models": ["m-rerank"],
            "model_roles": {"m-rerank": ["rerank"]},
            "for_roles": ["rerank"],
        }
    )
    monkeypatch.setattr(wired, "MODEL_SERVICES", _dump(services))
    monkeypatch.setattr(
        wired,
        "MODEL_ROLES",
        _dump(_roles(with_rerank={"service_id": SVC_RERANK, "model": "m-rerank"})),
    )

    resp = client.put("/api/settings", json={"rerank": _rerank_payload(base_url=NEW_URL)})

    assert resp.status_code == 200, resp.text
    stored = json.loads(_env_value(_read_env(fake_dotenv), "MODEL_SERVICES"))
    row = next(item for item in stored if item["id"] == SVC_RERANK)
    assert row["base_url"] == NEW_URL, "独立重排序服务的地址必须真的落盘"
    embed_row = next(item for item in stored if item["id"] == SVC_EMBED)
    assert embed_row["base_url"] == EMBED_URL


def test_apply_side_guard_blocks_embed_address_drift(client, wired, fake_dotenv):  # noqa: F811
    """④ apply 兜底：同一次提交里改掉 embed 地址、rerank 段不给 base_url。

    resolve 侧看到的 base_url == settings 里的旧址 ⇒ 守卫不触发；
    只有 _apply_rerank 拿「本次提交的 services」比旧址时才发现漂移 ⇒ 400、.env 不变。
    """
    before_env = _read_env(fake_dotenv)
    services = _services()
    embed_row = next(item for item in services if item["id"] == SVC_EMBED)
    embed_row["base_url"] = NEW_URL

    resp = client.put(
        "/api/settings",
        json={"services": services, "roles": _roles(), "rerank": _rerank_payload(base_url=None)},
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == GUARD_DETAIL
    assert _read_env(fake_dotenv) == before_env
