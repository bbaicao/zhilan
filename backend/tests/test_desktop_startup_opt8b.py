"""OPT8B 启动失败可见且有界退出；只替换外部边界，不连接真实库或打开窗口。"""
from __future__ import annotations

import sys

import pytest

from tests import test_s8_desktop_startup as s8


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    rec = s8.Recorder()
    backend = s8._install_boundaries(
        monkeypatch, rec, tmp_path, primary_log=tmp_path / "start.log"
    )
    guard = object()
    released = []
    monkeypatch.setattr(s8.app, "acquire_instance_guard", lambda: guard)
    monkeypatch.setattr(s8.app, "release_instance_guard", released.append)
    monkeypatch.setattr(s8.app, "window_size", lambda: s8.app.FALLBACK_SIZE)
    thread_type = s8.app.threading.Thread

    def record_thread(*args, **kwargs):
        thread = thread_type(*args, **kwargs)
        rec.threads.append(thread)
        return thread

    monkeypatch.setattr(s8.app.threading, "Thread", record_thread)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.chdir(backend.parent)
    monkeypatch.delenv("PORT", raising=False)
    saved_path = list(sys.path)
    yield s8.app, rec, guard, released
    sys.path[:] = saved_path


def _assert_failed(desktop, cause):
    app, rec, guard, released = desktop
    assert app.main() == 1
    assert "知澜启动失败" in rec.notice_texts and cause in rec.notice_texts
    assert rec.start_calls == 0
    if rec.reserved:
        assert rec.reserved[0][0].closed
        assert released == [guard]
    assert all(server.should_exit for server in rec.servers)
    assert all(not thread.is_alive() for thread in rec.threads)


@pytest.mark.parametrize("port", ["bad-port", "0", "-1", "65536"])
def test_invalid_environment_port_is_visible_and_does_not_raise(desktop, monkeypatch, port):
    monkeypatch.setitem(sys.modules, "app.core.config", None)
    monkeypatch.setenv("PORT", port)
    _assert_failed(desktop, "PORT")
    assert desktop[1].alembic_calls == 0 and not desktop[1].reserved
    assert desktop[3] == []


@pytest.mark.parametrize("env_port", ["8124", None])
def test_settings_import_failure_uses_environment_or_default(desktop, monkeypatch, env_port):
    monkeypatch.setitem(sys.modules, "app.core.config", None)
    if env_port is not None:
        monkeypatch.setenv("PORT", env_port)
    selected = []
    reserve = desktop[0].reserve_port
    monkeypatch.setattr(
        desktop[0], "reserve_port",
        lambda host, start: (selected.append(start), reserve(host, start))[1],
    )
    assert desktop[0].main() == 0
    assert selected == [int(env_port or 8000)]
    assert desktop[1].alembic_calls == 1 and desktop[1].start_calls == 1
    assert desktop[1].windows[0]["kwargs"]["text_select"] is True
    assert desktop[3] == [desktop[2]]


def test_valid_settings_port_keeps_priority_over_invalid_environment(desktop, monkeypatch):
    monkeypatch.setenv("PORT", "invalid-ignored")
    assert desktop[0].main() == 0
    assert desktop[1].start_calls == 1
    assert sys.modules["webview"].settings["ALLOW_DOWNLOADS"] is True


def test_missing_webview_is_visible_before_resources_are_acquired(desktop, monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", None)
    _assert_failed(desktop, "PyWebview")
    assert desktop[1].alembic_calls == 0 and not desktop[1].reserved
    assert desktop[3] == []


@pytest.mark.parametrize("module", ["uvicorn", "main"])
def test_backend_import_failure_is_visible_and_releases_resources(desktop, monkeypatch, module):
    monkeypatch.setitem(sys.modules, module, None)
    _assert_failed(desktop, "后端服务")


def test_backend_initialization_failure_is_visible_and_releases_resources(desktop, monkeypatch):
    def broken_config(*args, **kwargs):
        raise RuntimeError("startup configuration failed")
    monkeypatch.setattr(sys.modules["uvicorn"], "Config", broken_config)
    _assert_failed(desktop, "后端服务")


@pytest.mark.parametrize("operation", ["create_window", "start"])
def test_window_failure_is_visible_and_releases_resources(desktop, monkeypatch, operation):
    def broken_window(*args, **kwargs):
        raise RuntimeError("WebView2 unavailable")
    monkeypatch.setattr(sys.modules["webview"], operation, broken_window)
    _assert_failed(desktop, "WebView2")
