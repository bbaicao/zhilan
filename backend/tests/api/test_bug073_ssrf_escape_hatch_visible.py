"""回归修复 判据载体：SSRF 逃生门状态在 `GET /api/settings` 的 `web` 段**只读暴露**（机器可读）。

背景：逃生门 `settings.WEB_SSRF_ALLOW_ANY_HOST`（`config.py`，默认 False）开启后
此前界面上 / API 上都看不出来（只有一条 WARNING 日志），唯一开关通道是改 `.env` + 重启。
拍板②：把它以可选布尔键 `web.ssrf_allow_any_host` 加进 `web_payload()` 的**只读**出参；
**零前端改动**（前端不消费），**不进 PUT 可写键集**。

三条腿：

- **腿①（默认可见）**：GET 的 `web` 段含 `ssrf_allow_any_host`，且逃生门在默认值（False）时
  如实回 `False`、类型是 bool；
- **腿②（现读语义 / 与守卫同源）**：`settings.WEB_SSRF_ALLOW_ANY_HOST` 翻 True ⇒ GET 立即读出
  True（web_payload 每次现读，不在首次 GET 时固化）；翻回 False ⇒ 立即回落 False。
  并交叉核对 `net_guard.allow_any_host()` 与出参**同值**（同一单例、同一来源——
  守卫看到什么，API 就报什么）；
- **腿③（只读性护栏）**：`PUT /api/settings` 带 `{"web": {"ssrf_allow_any_host": true}}`
  ⇒ 仍 400 且文案逐字不变（`WEB_PUT_REJECTED` 路径，不是 422 —— 未知内键被 `WebIn`
  忽略后命中「web 段整体拒写」分支），`.env` 与内存单例一个字不动——
  逃生门没有被顺手变成可写。

🔴 口径（不得含糊）：**本载体只证 API 面**；前端零消费是**有意口径**（拍板② = 零前端改动），
**不得**据此写「界面上已可见」。逃生门的唯一开关通道仍是改 `.env` + 重启。

夹具纪律照 `test_settings_contract.py`：settings 域不碰数据库 ⇒ 自建 ASGI 客户端；
`.env` 指到 `tmp_path`（绝不碰仓库真实 `.env`）。`tests/api/conftest.py` 的 autouse 钉子
（`pin_web_off` 等）对本文件照常生效——GET / PUT 两条被测路径都零出网。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.api.settings_schemas import WEB_PUT_REJECTED
from app.core import env_store
from app.core.config import settings
from app.core.net_guard import allow_any_host

from tests.api._sync_client import SyncClient, build_app


@pytest.fixture
def fake_dotenv(tmp_path, monkeypatch) -> Path:
    """把 `.env` 指到 `tmp_path` 下的假文件（照 `test_settings_contract.py` 的写法）。"""
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


def _web_section(client: SyncClient) -> dict:
    return client.get("/api/settings").json()["web"]


# --- 腿①：GET web 段含 ssrf_allow_any_host，默认态如实回 False --------------------------------


def test_leg1_web_section_exposes_ssrf_hatch_default_false(client, monkeypatch):
    # 本机 .env 可能开着逃生门（settings 是进程级单例）⇒ 显式钉回**出厂默认值**：
    # 本腿断言的是「默认态下 API 如实回 False」，不是「本机当前值」。
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)

    web = _web_section(client)
    assert "ssrf_allow_any_host" in web, "web 段缺 回归修复 的只读暴露键"
    assert web["ssrf_allow_any_host"] is False
    assert isinstance(web["ssrf_allow_any_host"], bool)


# --- 腿②：现读语义——开关翻 True 立即读出 True，且与守卫同值 ------------------------------------


def test_leg2_payload_reads_live_and_matches_guard(client, monkeypatch):
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)
    assert _web_section(client)["ssrf_allow_any_host"] is False

    # 开（守卫与 API 看同一个单例上的同一个值 ⇒「现读、与守卫同源」）
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    web = _web_section(client)
    assert web["ssrf_allow_any_host"] is True, "逃生门开了，GET 却仍回 False ⇒ 不是现读"
    assert allow_any_host() is True
    assert web["ssrf_allow_any_host"] == allow_any_host(), "出参与守卫读到的值不同源"

    # 关（同一次会话内来回翻，钉死「不在首次 GET 时固化」）
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)
    assert _web_section(client)["ssrf_allow_any_host"] is False


# --- 腿③：只读性护栏——PUT 带 web 段仍 400、文案不变、零落盘零改内存 ---------------------------


def test_leg3_put_web_section_still_rejected_400(client, fake_dotenv, monkeypatch):
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)
    before_env = fake_dotenv.read_text(encoding="utf-8")

    resp = client.put("/api/settings", json={"web": {"ssrf_allow_any_host": True}})
    assert resp.status_code == 400, "PUT web 段必须整段拒写（400），而不是接受后丢弃"
    assert resp.json()["detail"] == WEB_PUT_REJECTED, "WEB_PUT_REJECTED 文案不得漂移"
    # 073 红线：这个键没有被顺手变成可写——磁盘与内存都一个字没动
    assert fake_dotenv.read_text(encoding="utf-8") == before_env, ".env 被写了"
    assert settings.WEB_SSRF_ALLOW_ANY_HOST is False, "内存单例被改了"
    assert _web_section(client)["ssrf_allow_any_host"] is False
