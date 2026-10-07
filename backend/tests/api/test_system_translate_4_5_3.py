"""`POST /api/system/pick-directory` 与 `POST /api/translate`（阶段 4 · 条目 `4.5.3`）。

两条端点各有一处**必须钉住**的口径：

- `pick-directory`：**不在模块级 import tkinter**（精简版 Python 没有它，模块级 import 会让
  `main.py` 起不来）；弹不出来时**不 500、不挂**，如实回 `{available: false, path: "", detail: 中文}`；
- `translate`：空文本 / 非法 target / 未配置 Key / 超长**全是 400**（不是 422！冻结前端与 live 契约
  用例都按 400 断言）；`:fx` 结尾的 Key 走免费端点；**任何响应里都不出现 Key 本体**。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.api import system_routes, translate_routes, translate_spec
from app.core.config import settings

from tests.api._sync_client import SyncClient, build_app

BACKEND_DIR = Path(__file__).resolve().parents[2]  # tests/api/x.py -> backend


@pytest.fixture
def client():
    return SyncClient(build_app(system_routes.router, translate_routes.router))


# --- pick-directory --------------------------------------------------------


def test_tkinter_is_not_imported_at_module_level():
    """模块级不许 import tkinter（否则精简版 Python 上 main.py 直接起不来）。"""
    import os

    code = "import sys, app.api.system_routes as m; assert 'tkinter' not in sys.modules, 'import 期就拉了 tkinter'"
    result = subprocess.run(  # noqa: S603 —— 固定 argv，无 shell
        [sys.executable, "-c", code],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONPATH": str(BACKEND_DIR), "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_pick_directory_returns_chosen_path(client, monkeypatch):
    monkeypatch.setattr(system_routes, "_ask_directory", lambda initial: "D:/知澜数据")
    resp = client.post("/api/system/pick-directory", json={"initial": "D:/"})
    assert resp.status_code == 200
    assert resp.json() == {"available": True, "path": "D:/知澜数据", "detail": ""}


def test_pick_directory_without_body_still_works(client, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(system_routes, "_ask_directory", lambda initial: seen.append(initial) or "")
    resp = client.post("/api/system/pick-directory")
    assert resp.status_code == 200
    assert resp.json()["available"] is True and resp.json()["path"] == ""
    assert seen == [""]


def test_pick_directory_without_gui_reports_readable_chinese(client, monkeypatch):
    """无图形界面：**200 + available=false + 可读中文**（不是 400/500，也不许挂）。

    契约形状就是 `{available, path, detail}`（`api.ts:1243-1251` 的注释：「available = false
    表示本机弹不出来（无图形界面等），界面应退回手填」）——回 400 会让冻结前端把它当异常弹，
    而不是按设计退回手填。
    """
    def _boom(initial: str) -> str:
        raise system_routes.PickerUnavailable("本机没有可用的图形界面（no display），请手动填写路径。")

    monkeypatch.setattr(system_routes, "_ask_directory", _boom)
    resp = client.post("/api/system/pick-directory", json={"initial": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False and body["path"] == ""
    assert "图形界面" in body["detail"] and body["detail"]


def test_pick_directory_timeout_is_readable(client, monkeypatch):
    import time

    monkeypatch.setattr(system_routes, "PICK_TIMEOUT", 0.01)
    monkeypatch.setattr(system_routes, "_ask_directory", lambda initial: time.sleep(0.4) or "")
    resp = client.post("/api/system/pick-directory", json={"initial": ""})
    assert resp.status_code == 200
    assert resp.json()["available"] is False
    assert "超时" in resp.json()["detail"]


# --- translate -------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, json_body=None) -> None:
        self.status_code = status_code
        self._json_body = json_body

    def json(self):
        if self._json_body is None:
            raise ValueError("No JSON")
        return self._json_body


class _FakeAsyncClient:
    """`httpx.AsyncClient` 替身：按脚本回放（首项是响应实例或待抛异常），并记录请求。"""

    def __init__(self, script: list, log: list, **opts) -> None:
        self._script = script
        self._log = log
        self.opts = opts

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def post(self, url: str, **kwargs):
        self._log.append({"url": url, "opts": self.opts, **kwargs})
        item = self._script.pop(0) if self._script else _FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item


def _install_httpx(monkeypatch, *script) -> list[dict]:
    """把 `translate_spec` 里的 `httpx` **换成一个只带 AsyncClient 的壳**。

    ⚠️ **不能直接 `monkeypatch.setattr(httpx, "AsyncClient", ...)`**：`translate_spec.httpx`
    就是 `httpx` 模块本身，改它会把本文件的 `SyncClient`（也走 `httpx.AsyncClient`）
    一起换掉 ⇒ 用例连请求都发不出去（实测踩过）。换成壳之后真 httpx 一点没动。
    """
    import httpx

    log: list[dict] = []
    items = list(script)

    class _Shim:
        TimeoutException = httpx.TimeoutException
        HTTPError = httpx.HTTPError

        @staticmethod
        def AsyncClient(**opts):  # noqa: ANN001
            return _FakeAsyncClient(items, log, **opts)

    monkeypatch.setattr(translate_spec, "httpx", _Shim)
    return log


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "DEEPL_API_KEY", "deepl-key-abc")
    return settings


def test_translate_empty_text_is_400_not_422(client, _key):
    resp = client.post("/api/translate", json={"text": "", "target": "en"})
    assert resp.status_code == 400
    assert isinstance(resp.json()["detail"], str) and resp.json()["detail"]


def test_translate_bad_target_is_400_not_422(client, _key):
    resp = client.post("/api/translate", json={"text": "hello", "target": "fr"})
    assert resp.status_code == 400
    assert "zh" in resp.json()["detail"]


def test_translate_without_key_is_400_readable(client, monkeypatch):
    monkeypatch.setattr(settings, "DEEPL_API_KEY", "")
    resp = client.post("/api/translate", json={"text": "hello", "target": "zh"})
    assert resp.status_code == 400
    assert "DeepL" in resp.json()["detail"]


def test_translate_too_long_is_400(client, _key):
    resp = client.post("/api/translate", json={"text": "x" * (translate_spec.MAX_TRANSLATE_CHARS + 1), "target": "zh"})
    assert resp.status_code == 400
    assert "上限" in resp.json()["detail"]


def test_translate_happy_path_uses_official_endpoint_and_hides_key(client, _key, monkeypatch):
    log = _install_httpx(
        monkeypatch,
        _FakeResponse(json_body={"translations": [{"text": "你好", "detected_source_language": "EN"}]}),
    )
    resp = client.post("/api/translate", json={"text": "hello", "target": "zh"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"text": "你好", "detected_source_language": "EN", "target": "zh"}
    assert "deepl-key-abc" not in resp.text, "响应里不许出现 Key 本体"

    assert len(log) == 1
    assert log[0]["url"] == "https://api.deepl.com/v2/translate"
    assert log[0]["headers"]["Authorization"] == "DeepL-Auth-Key deepl-key-abc"
    assert log[0]["json"] == {"text": ["hello"], "target_lang": "ZH"}


def test_translate_free_key_uses_free_endpoint(client, monkeypatch):
    monkeypatch.setattr(settings, "DEEPL_API_KEY", "free-key:fx")
    log = _install_httpx(monkeypatch, _FakeResponse(json_body={"translations": [{"text": "hi"}]}))
    resp = client.post("/api/translate", json={"text": "你好", "target": "en"})
    assert resp.status_code == 200
    assert log[0]["url"] == "https://api-free.deepl.com/v2/translate"
    assert log[0]["json"]["target_lang"] == "EN-US"


@pytest.mark.parametrize(
    "script,status",
    [
        (_FakeResponse(status_code=403), 401),
        (_FakeResponse(status_code=456), 502),
        (_FakeResponse(status_code=429), 502),
        (_FakeResponse(status_code=500), 502),
        (_FakeResponse(status_code=200, json_body={"nope": 1}), 502),
    ],
)
def test_translate_error_mapping(client, _key, monkeypatch, script, status):
    _install_httpx(monkeypatch, script)
    resp = client.post("/api/translate", json={"text": "hello", "target": "zh"})
    assert resp.status_code == status, resp.text
    assert isinstance(resp.json()["detail"], str) and resp.json()["detail"]


def test_translate_timeout_maps_to_504(client, _key, monkeypatch):
    import httpx

    _install_httpx(monkeypatch, httpx.TimeoutException("boom"))
    resp = client.post("/api/translate", json={"text": "hello", "target": "zh"})
    assert resp.status_code == 504
    assert "超时" in resp.json()["detail"]


def test_translate_network_error_is_readable(client, _key, monkeypatch):
    import httpx

    _install_httpx(monkeypatch, httpx.ConnectError("连接失败"))
    resp = client.post("/api/translate", json={"text": "hello", "target": "zh"})
    assert resp.status_code == 502
    assert "网络" in resp.json()["detail"]


def test_deepl_base_url_switch():
    assert translate_spec.deepl_base_url("k:fx") == "https://api-free.deepl.com"
    assert translate_spec.deepl_base_url("k") == "https://api.deepl.com"
    assert translate_spec.target_lang(" ZH ") == "ZH"
    assert translate_spec.target_lang("fr") is None
