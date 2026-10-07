r"""S8 · 桌面壳启动判据（条目 158 日志落点不可写 / 条目 170 单实例 + 端口预留 + 服务归属）。

作者：s8-desktop（唯一写者），2026-10-03。直接驱动真实 client/app.py，不做任何源码替换。

运行（必须经仓库独占锁，见 backend/scripts/with_lock.ps1）：
  & ./.venv/Scripts/python.exe -B -m pytest tests/test_s8_desktop_startup.py -q --tb=short

负对照（源仓零写入）：把被测模块重定向到字节级副本
  $env:ZHILAN_S8_DESKTOP_APP = "...\copies\app.py.orig158"
再跑 -k 158 / -k 170 即可在「未修版本」上打红。

边界替身（都是 main() 的外部边界，不是被测逻辑）：
  backend_dir / log_file / run_alembic / data_root / instance_identity /
  sys.modules["uvicorn"]、["main"]、["webview"]。
main 驱动统一走 _run_main()：它自己恢复 sys.path（main 会 insert backend 目录）。

口径：本文件只写「与基线双向差集」的读数，⛔ 不写「零回归」；skip 不算通过。
"""
from __future__ import annotations

import errno
import importlib.machinery
import importlib.util
import os
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.request
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_desktop_app():
    """默认测仓库真源；设了 ZHILAN_S8_DESKTOP_APP 就测那份字节级副本（负对照用）。"""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    override = os.environ.get("ZHILAN_S8_DESKTOP_APP")
    if override:
        path = str(Path(override))
        # 副本名不带 .py（如 app.py.orig158），spec_from_file_location 无法自行推断 loader，
        # 必须显式给 SourceFileLoader，否则 spec 为 None（AttributeError: NoneType.loader）。
        loader = importlib.machinery.SourceFileLoader("client.app", path)
        spec = importlib.util.spec_from_file_location("client.app", path, loader=loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["client.app"] = module
        spec.loader.exec_module(module)
        return module
    from client import app as module

    return module


app = _load_desktop_app()
UNDER_TEST = getattr(app, "__file__", "<unknown>")
HAS_170 = hasattr(app, "reserve_port") and hasattr(app, "acquire_instance_guard")

# 未打补丁的原始实现（leg 需要「真实现」时用；_install_boundaries 只覆盖模块属性，不动这里）
_PRISTINE = {
    name: getattr(app, name)
    for name in (
        "data_root",
        "instance_identity",
        "instance_guard_name",
        "acquire_instance_guard",
        "release_instance_guard",
        "reserve_port",
        "wait_server_ready",
        "_serve_in_thread",
        "_shutdown",
        "_port_blocked_reason",
    )
    if hasattr(app, name)
}


# --------------------------------------------------------------------------------------
# 替身与驱动
# --------------------------------------------------------------------------------------
class Recorder:
    """main() 驱动过程中的可观测读数（全部来自替身与真实落点）。"""

    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []
        self.alembic_calls = 0
        self.windows: list[dict] = []
        self.start_calls = 0
        self.reserved: list = []
        self.servers: list = []
        self.threads: list[threading.Thread] = []

    @property
    def notice_texts(self) -> str:
        return "\n".join(f"{t}||{m}" for t, m in self.notices)


class DummySocket:
    """预留端口的替身（158 腿不需要真 socket；170 腿用真 reserve_port）。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def fileno(self) -> int:
        return -1 if self.closed else 0


def _broken_stream(exc_factory):
    class _Broken:
        def write(self, *args, **kwargs):
            raise exc_factory()

        def flush(self, *args, **kwargs):
            raise exc_factory()

        def isatty(self) -> bool:
            return False

    return _Broken()


def _patch_fallback_log_file(monkeypatch, path):
    """未修版本没有 fallback_log_file（条目 158 之前）——有才替换，保证打红原因是真缺陷而不是 AttributeError。"""
    if hasattr(app, "fallback_log_file"):
        monkeypatch.setattr(app, "fallback_log_file", lambda: path)
        return True
    return False


def _patch_if_present(monkeypatch, name, value):
    if hasattr(app, name):
        monkeypatch.setattr(app, name, value)
        return True
    return False


def _install_boundaries(
    monkeypatch,
    rec,
    tmp_path,
    *,
    primary_log,
    start_port=8000,
    chosen_port=8123,
    server_ready=True,
    run_exc=None,
    window_exc=None,
    start_exc=None,
    real_reserve=False,
    identity="s8-desktop-test-identity",
):
    """装好 main() 的全部外部边界替身；返回 backend 临时目录（已建好）。"""
    backend = tmp_path / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app, "backend_dir", lambda: backend)
    monkeypatch.setattr(app, "log_file", lambda: primary_log)
    monkeypatch.setattr(app, "run_alembic", _record_alembic(rec))
    _patch_if_present(monkeypatch, "data_root", lambda: tmp_path / "dataroot")
    _patch_if_present(monkeypatch, "instance_identity", lambda root=None: identity)
    _patch_if_present(monkeypatch, "_native_message", lambda title, message: rec.notices.append((title, message)))
    if not hasattr(app, "_native_message"):  # 未修版本：notify 不存在，这里只留读数位
        pass

    def _reserve(host, start, span=50):
        if real_reserve:
            sock, port, blockers = _PRISTINE["reserve_port"](host, start, span)
        else:
            sock, port, blockers = DummySocket(), chosen_port, []
        rec.reserved.append((sock, port, blockers))
        return sock, port, blockers

    if _patch_if_present(monkeypatch, "reserve_port", _reserve):
        pass
    else:
        monkeypatch.setattr(app, "find_free_port", lambda host, start, span=50: chosen_port)
    _patch_if_present(monkeypatch, "wait_port", lambda host, port, timeout=30: True)

    # 假 uvicorn：Config/Server 只是记录器，run() 按需置 started 并等 should_exit
    class FakeConfig:
        def __init__(self, asgi_app, **kwargs):
            self.asgi_app = asgi_app
            self.kwargs = kwargs

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False
            self.sockets = None
            self.finished = threading.Event()
            rec.servers.append(self)

        def run(self, sockets=None):
            self.sockets = sockets
            if run_exc is not None:
                raise run_exc
            if server_ready:
                self.started = True
            deadline = time.monotonic() + 10.0
            while not self.should_exit and time.monotonic() < deadline:
                time.sleep(0.01)
            self.finished.set()

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.Config = FakeConfig
    fake_uvicorn.Server = FakeServer
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    fake_main = types.ModuleType("main")
    fake_main.app = object()
    monkeypatch.setitem(sys.modules, "main", fake_main)

    def create_window(*args, **kwargs):
        if window_exc is not None:
            raise window_exc
        rec.windows.append({"args": args, "kwargs": kwargs})
        return object()

    def webview_start(*args, **kwargs):
        rec.start_calls += 1
        if start_exc is not None:
            raise start_exc

    fake_webview = types.ModuleType("webview")
    fake_webview.settings = {"ALLOW_DOWNLOADS": False}
    fake_webview.create_window = create_window
    fake_webview.start = webview_start
    monkeypatch.setitem(sys.modules, "webview", fake_webview)

    # 假 settings：main() 里 from app.core.config import settings（避免读真实 .env）
    fake_config = types.ModuleType("app.core.config")
    fake_config.settings = types.SimpleNamespace(PORT=start_port, start_port=start_port)
    fake_core = types.ModuleType("app.core")
    fake_core.__path__ = []  # type: ignore[attr-defined]
    fake_app_pkg = types.ModuleType("app")
    fake_app_pkg.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app", fake_app_pkg)
    monkeypatch.setitem(sys.modules, "app.core", fake_core)
    monkeypatch.setitem(sys.modules, "app.core.config", fake_config)
    return backend


def _record_alembic(rec):
    def _run():
        rec.alembic_calls += 1

    return _run


def _run_main(monkeypatch, rec, tmp_path, **kwargs):
    """跑一遍 main()，返回 (返回码, recorder)；sys.path 复原（main 会 insert backend 目录）。"""
    backend = _install_boundaries(monkeypatch, rec, tmp_path, **kwargs)
    monkeypatch.chdir(backend.parent)
    saved_path = list(sys.path)
    try:
        rc = app.main()
    finally:
        sys.path[:] = saved_path
    return rc, rec


# --------------------------------------------------------------------------------------
# 条目 158 · 日志落点不可写
# --------------------------------------------------------------------------------------
def test_158_a_primary_path_blocked_by_plain_file(tmp_path, monkeypatch, capsys):
    """① 主落点父路径被同名普通文件占位：原实现抛 FileExistsError/NotADirectoryError 逃出调用方。"""
    blocker = tmp_path / "client-root"
    blocker.write_text("占位", encoding="utf-8")
    fallback = tmp_path / "fb" / "start-fallback.log"
    monkeypatch.setattr(app, "log_file", lambda: blocker / "start.log")
    _patch_fallback_log_file(monkeypatch, fallback)

    app.log("158-a 主落点父路径是文件")

    assert fallback.read_text(encoding="utf-8") == "158-a 主落点父路径是文件\n"
    assert "158-a 主落点父路径是文件" in capsys.readouterr().err


def test_158_b_primary_target_is_a_directory(tmp_path, monkeypatch):
    """② 主落点本身就是个目录：open("a") 抛 IsADirectoryError/PermissionError。"""
    target = tmp_path / "client-root" / "start.log"
    target.mkdir(parents=True)
    fallback = tmp_path / "fb" / "start-fallback.log"
    monkeypatch.setattr(app, "log_file", lambda: target)
    _patch_fallback_log_file(monkeypatch, fallback)

    app.log("158-b 主落点是目录")

    assert fallback.read_text(encoding="utf-8") == "158-b 主落点是目录\n"


def test_158_c_permission_error_on_mkdir(tmp_path, monkeypatch):
    """③ 数据目录不可写（mkdir/open 抛 PermissionError）但临时目录可写：退路必须落到具名文件。"""
    primary = tmp_path / "readonly" / "start.log"
    fallback = tmp_path / "fb" / "start-fallback.log"
    real_mkdir = Path.mkdir
    real_open = Path.open
    blocked = str(tmp_path / "readonly")

    def guard_mkdir(self, *args, **kwargs):
        if str(self).startswith(blocked):
            raise PermissionError(13, "拒绝访问。")
        return real_mkdir(self, *args, **kwargs)

    def guard_open(self, *args, **kwargs):
        if str(self).startswith(blocked):
            raise PermissionError(13, "拒绝访问。")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", guard_mkdir)
    monkeypatch.setattr(Path, "open", guard_open)
    monkeypatch.setattr(app, "log_file", lambda: primary)
    _patch_fallback_log_file(monkeypatch, fallback)

    app.log("158-c 数据目录权限不足")

    assert fallback.read_text(encoding="utf-8") == "158-c 数据目录权限不足\n"


def test_158_d_broken_stdout_does_not_break_log(tmp_path, monkeypatch):
    """④ stdout 的 write/flush 抛错（冻结成品里常见的句柄无效）：log() 仍要落地主落点且不抛。"""
    primary = tmp_path / "client-root" / "start.log"
    monkeypatch.setattr(app, "log_file", lambda: primary)
    with monkeypatch.context() as m:
        m.setattr(sys, "stdout", _broken_stream(lambda: OSError(22, "句柄无效。")))
        app.log("158-d stdout 写入失败")

    assert primary.read_text(encoding="utf-8") == "158-d stdout 写入失败\n"


def test_158_d2_stdout_none_is_tolerated(tmp_path, monkeypatch):
    """④′ 冻结态 sys.stdout 可能是 None：控制台那一份直接放弃，不抛也不影响主落点。"""
    primary = tmp_path / "client-root" / "start.log"
    monkeypatch.setattr(app, "log_file", lambda: primary)
    with monkeypatch.context() as m:
        m.setattr(sys, "stdout", None)
        assert app._write_console("x\n") is False
        app.log("158-d2 stdout 为 None")

    assert primary.read_text(encoding="utf-8") == "158-d2 stdout 为 None\n"


def test_158_e_main_opens_window_when_log_sink_unwritable(tmp_path, monkeypatch):
    """⑤ 沿真实 main() 控制流：主落点不可写也必须走到 create_window（原实现死在这里）。"""
    blocker = tmp_path / "client-root"
    blocker.write_text("占位", encoding="utf-8")
    fallback = tmp_path / "fb" / "start-fallback.log"
    _patch_fallback_log_file(monkeypatch, fallback)
    rec = Recorder()

    rc, rec = _run_main(
        monkeypatch,
        rec,
        tmp_path,
        primary_log=blocker / "start.log",
        start_port=8000,
        chosen_port=8123,
    )

    assert rc == 0, "主落点不可写时 main() 必须以 0 收尾（窗口已开）"
    assert rec.alembic_calls == 1
    assert len(rec.windows) == 1, "webview.create_window 必须在日志落点不可写时照样被调用"
    assert rec.start_calls == 1
    kw = rec.windows[0]["kwargs"]
    assert kw["width"] > 0 and kw["height"] > 0
    assert app.FALLBACK_SIZE == (1054, 651) and app.GOLDEN == 1.618, "黄金尺寸常量不得被本条改动"
    if hasattr(app, "fallback_log_file"):
        text = fallback.read_text(encoding="utf-8")
        assert "窗口尺寸" in text, "退路落点必须真的记下窗口尺寸这一行"
        assert "8000" in text and "8123" in text  # 「端口 8000 被占用，已切换到 8123」照样记录


def test_158_f_primary_sink_semantics_unchanged(tmp_path, monkeypatch, capsys):
    """反向保护：主落点可写时，UTF-8 追加 / rstrip / 单个换行 / 打印语义逐字不变。"""
    primary = tmp_path / "client-root" / "start.log"
    monkeypatch.setattr(app, "log_file", lambda: primary)

    app.log("第一行")
    app.log("  第二行有前后空白  ")
    app.log("第三行自带换行\n")

    # 原实现就是 line = message.rstrip() + 换行：只削行尾空白，行首空白必须保留
    expected = "第一行\n  第二行有前后空白\n第三行自带换行\n"
    assert primary.read_text(encoding="utf-8") == expected
    assert capsys.readouterr().out == expected
    assert primary.read_bytes().startswith("第一行".encode("utf-8"))


def test_158_g_stderr_fallback_is_used_when_primary_fails(tmp_path, monkeypatch, capsys):
    """退路一：主落点失败时 stderr 必须拿到那一行（⛔ 不得回调 log() 造成递归）。"""
    blocker = tmp_path / "client-root"
    blocker.write_text("占位", encoding="utf-8")
    monkeypatch.setattr(app, "log_file", lambda: blocker / "start.log")
    _patch_fallback_log_file(monkeypatch, tmp_path / "fb" / "start-fallback.log")

    app.log("158-g 走 stderr 退路")

    assert capsys.readouterr().err == "158-g 走 stderr 退路\n"


def test_158_h_all_sinks_failed_is_silent(tmp_path, monkeypatch):
    """全部退路都失败：log() 返回 None、不抛（窗口照样要能开）。"""
    blocker = tmp_path / "client-root"
    blocker.write_text("占位", encoding="utf-8")
    monkeypatch.setattr(app, "log_file", lambda: blocker / "start.log")
    _patch_fallback_log_file(monkeypatch, blocker / "fb.log")
    with monkeypatch.context() as m:
        m.setattr(sys, "stderr", _broken_stream(lambda: OSError(9, "错误的文件描述符。")))
        assert app.log("158-h 全部退路失败") is None


def test_158_i_keyboard_interrupt_is_not_swallowed(tmp_path, monkeypatch):
    """③ 只吞日志 I/O 的 Exception：KeyboardInterrupt / SystemExit 照旧穿出。"""
    monkeypatch.setattr(app, "log_file", lambda: tmp_path / "client-root" / "start.log")
    with monkeypatch.context() as m:
        m.setattr(sys, "stdout", _broken_stream(KeyboardInterrupt))
        with pytest.raises(KeyboardInterrupt):
            app.log("158-i 不该被吞")

# --------------------------------------------------------------------------------------
# 条目 170 · 双开 / 端口预留 / 服务归属 / 收尾
# --------------------------------------------------------------------------------------
CHILD_GUARD_SCRIPT = r'''import importlib.machinery, importlib.util, os, sys, time

app_path, identity, out_path, stop_path = sys.argv[1:5]
loader = importlib.machinery.SourceFileLoader("client.app", app_path)
spec = importlib.util.spec_from_file_location("client.app", app_path, loader=loader)
module = importlib.util.module_from_spec(spec)
sys.modules["client.app"] = module
spec.loader.exec_module(module)


def note(text):
    with open(out_path, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


handle = module.acquire_instance_guard(identity)
if not handle:
    note("ALREADY")
    raise SystemExit(0)
note("ACQUIRED")
deadline = time.time() + 30.0
while time.time() < deadline and not os.path.exists(stop_path):
    time.sleep(0.05)
module.release_instance_guard(handle)
note("RELEASED")
'''


def _require_170():
    if not HAS_170:
        pytest.fail("条目 170 修复未落盘：reserve_port / acquire_instance_guard 不存在（修前红）")


def _free_port(host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _third_party_bind(port, host="127.0.0.1"):
    """第三者抢端口：抢到就返回 socket，抢不到把 OSError 抛给调用方。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock


def _wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _guard_is_free(identity):
    handle = app.acquire_instance_guard(identity)
    if not handle:
        return False
    app.release_instance_guard(handle)
    return True


def _read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def test_170_a_second_instance_is_blocked(tmp_path, monkeypatch):
    """① 同身份第二实例：守卫挡住 ⇒ 不迁移、不抢端口、不开窗，提示「已运行」并正常退出（rc 0）。"""
    _require_170()
    identity = "s8-170-a-" + uuid.uuid4().hex[:8]
    holder = app.acquire_instance_guard(identity)
    assert holder, "第一个实例应当取得守卫"
    try:
        rec = Recorder()
        rc, rec = _run_main(monkeypatch, rec, tmp_path, primary_log=tmp_path / "start.log", identity=identity)
        assert rc == 0, "重复实例必须正常退出（rc 0），实际 rc=%r" % (rc,)
        assert rec.alembic_calls == 0, "重复实例不得再迁移"
        assert rec.reserved == [], "重复实例不得再抢端口"
        assert rec.windows == [], "重复实例不得开第二个窗口"
        assert app.ALREADY_RUNNING_MESSAGE in rec.notice_texts, rec.notice_texts
    finally:
        app.release_instance_guard(holder)
    assert _guard_is_free(identity), "第一实例释放后守卫应当可用（不残留）"


def test_170_a2_cross_process_guard_and_crash_release(tmp_path, monkeypatch):
    """① 跨进程：另一进程持有时本进程拿不到；持守卫的进程被杀后守卫立刻失效（⛔ 不靠删文件解锁）。"""
    _require_170()
    identity = "s8-170-a2-" + uuid.uuid4().hex[:8]
    out_path = tmp_path / "child-guard.log"
    stop_path = tmp_path / "child-stop.flag"
    script = tmp_path / "child_guard.py"
    script.write_text(CHILD_GUARD_SCRIPT, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    cmd = [sys.executable, "-B", str(script), str(UNDER_TEST), identity, str(out_path), str(stop_path)]

    log_handle = open(out_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env, stdout=log_handle, stderr=subprocess.STDOUT)
    try:
        assert _wait_for(lambda: "ACQUIRED" in _read_text(out_path), 30.0), "子进程没有拿到守卫"
        assert app.acquire_instance_guard(identity) is None, "另一进程持有时本进程也拿到了守卫（守卫不是内核级）"
    finally:
        proc.kill()
        proc.wait(timeout=20.0)
        log_handle.close()
    deadline = time.monotonic() + 15.0
    handle = None
    while time.monotonic() < deadline and handle is None:
        handle = app.acquire_instance_guard(identity)
        if handle is None:
            time.sleep(0.1)
    assert handle, "持守卫的进程退出后守卫必须立刻失效（⛔ 不得残留）"
    app.release_instance_guard(handle)

    # 第三段：子进程走正常释放路径 ⇒ 父进程随后也能拿到（可重启）
    out2 = tmp_path / "child-guard-2.log"
    log_handle2 = open(out2, "w", encoding="utf-8")
    proc2 = subprocess.Popen(
        [sys.executable, "-B", str(script), str(UNDER_TEST), identity, str(out2), str(stop_path)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_handle2,
        stderr=subprocess.STDOUT,
    )
    try:
        assert _wait_for(lambda: "ACQUIRED" in _read_text(out2), 30.0), "第二段子进程没有拿到守卫"
        stop_path.write_text("stop", encoding="utf-8")
        assert _wait_for(lambda: "RELEASED" in _read_text(out2), 15.0), "子进程没有正常释放守卫"
        assert proc2.wait(timeout=20.0) == 0
    finally:
        if proc2.poll() is None:
            proc2.kill()
            proc2.wait(timeout=20.0)
        log_handle2.close()
    assert _guard_is_free(identity), "子进程正常释放后守卫应当可用"


def test_170_b_restart_after_release_without_lock_files(tmp_path, monkeypatch):
    """② 第一进程退出后可重启；守卫⛔ 不落任何锁文件，解锁⛔ 也不靠删文件。"""
    _require_170()
    identity = "s8-170-b-" + uuid.uuid4().hex[:8]
    root = tmp_path / "dataroot"
    root.mkdir()
    handle = app.acquire_instance_guard(identity)
    assert handle
    app.release_instance_guard(handle)
    assert list(root.rglob("*")) == [], "守卫不得在数据根里留下任何文件"

    rec = Recorder()
    rc, rec = _run_main(
        monkeypatch,
        rec,
        tmp_path,
        primary_log=tmp_path / "start.log",
        start_port=_free_port(),
        real_reserve=True,
        identity=identity,
    )
    assert rc == 0, "释放守卫后必须能真的启动一次"
    assert rec.alembic_calls == 1 and len(rec.windows) == 1, (rec.alembic_calls, rec.windows)
    assert list(root.rglob("*")) == [], "启动过程也不得留下锁文件"
    assert rec.reserved[0][0].fileno() == -1, "main 收尾必须关闭预留 socket"
    assert _guard_is_free(identity), "main 退出后守卫必须已释放（可再次启动）"


def test_170_c_identities_differ_per_data_root(tmp_path, monkeypatch):
    """③ 不同隔离持久数据根 ⇒ 身份与守卫名不同、可同时启动（⛔ 不动真实用户根）。"""
    _require_170()
    root_a = tmp_path / "rootA"
    root_b = tmp_path / "rootB"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setattr(app, "data_root", lambda: root_a)
    id_a = app.instance_identity()
    name_a = app.instance_guard_name()
    monkeypatch.setattr(app, "data_root", lambda: root_b)
    id_b = app.instance_identity()
    name_b = app.instance_guard_name()
    assert id_a != id_b and name_a != name_b
    normalized_a = os.path.normcase(os.path.normpath(str(root_a)))
    assert normalized_a in id_a, "身份必须来自已解析的数据根：%r" % (id_a,)
    assert id_a.endswith("|session=" + str(app._current_session_id())), "身份必须带当前 Windows 会话作用域：%r" % (id_a,)
    assert id_a == app.instance_identity(root_a), "显式传根必须与解析出的根同身份"
    monkeypatch.setattr(app, "data_root", lambda: Path(str(root_a).swapcase()))
    assert app.instance_identity() == id_a, "身份必须大小写/路径归一（同一安装的不同写法不得算两个实例）"

    monkeypatch.setattr(app, "data_root", lambda: root_a)
    guard_a = app.acquire_instance_guard()
    guard_b = app.acquire_instance_guard(app.instance_identity(root_b))
    try:
        assert guard_a and guard_b, "不同数据根的两个实例必须能同时启动（隔离）"
    finally:
        app.release_instance_guard(guard_a)
        app.release_instance_guard(guard_b)


def test_170_d_reserved_port_is_held_against_third_party(tmp_path):
    """④ bind 成功即持有：预留期间第三者抢不到；关闭后又能抢到（反向保护）。"""
    _require_170()
    start = _free_port()
    sock, port, blockers = app.reserve_port("127.0.0.1", start)
    assert port == start and blockers == []
    assert sock.fileno() != -1, "预留 socket 必须一直开着（不是探测完就关）"
    try:
        with pytest.raises(OSError):
            _third_party_bind(port).close()
    finally:
        sock.close()
    assert sock.fileno() == -1
    rival = _third_party_bind(port)
    rival.close()


def test_170_m_third_party_cannot_bind_during_migration(tmp_path, monkeypatch):
    """④ 迁移阻塞期间：预留 socket 仍被本进程持有 ⇒ 第三者 bind 失败（条目 170 ② 的核心）。"""
    _require_170()
    start = _free_port()
    migrated = threading.Event()
    release = threading.Event()
    rec = Recorder()

    def slow_alembic():
        rec.alembic_calls += 1
        migrated.set()
        release.wait(timeout=30.0)

    backend = _install_boundaries(
        monkeypatch,
        rec,
        tmp_path,
        primary_log=tmp_path / "start.log",
        start_port=start,
        real_reserve=True,
        identity="s8-170-m-" + uuid.uuid4().hex[:8],
    )
    monkeypatch.setattr(app, "run_alembic", slow_alembic)
    monkeypatch.chdir(backend.parent)
    result = {}
    saved_path = list(sys.path)
    worker = threading.Thread(target=lambda: result.setdefault("rc", app.main()), daemon=True)
    worker.start()
    try:
        assert migrated.wait(30.0), "迁移没有开始（驱动失效）"
        assert rec.reserved and rec.reserved[0][1] == start, rec.reserved
        with pytest.raises(OSError):
            _third_party_bind(start).close()
    finally:
        release.set()
        worker.join(timeout=60.0)
        sys.path[:] = saved_path
    assert result.get("rc") == 0, result
    assert len(rec.windows) == 1
    rival = _third_party_bind(start)
    rival.close()


def test_170_e_scan_skips_occupied_and_reports_readable_reasons(tmp_path):
    """⑤ 扫描跳过被占用者并给出可读原因；EACCES/保留端口⛔ 不许说成「被占用」；候选全败 ⇒ 明确失败。"""
    _require_170()
    start = _free_port()
    occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupier.bind(("127.0.0.1", start))
    occupier.listen(1)
    try:
        sock, port, blockers = app.reserve_port("127.0.0.1", start)
        try:
            assert port == start + 1, "必须向上扫过被占用的 start 端口，实际 %r" % (port,)
            assert blockers and "端口已被占用" in blockers[0], blockers
        finally:
            sock.close()
        with pytest.raises(RuntimeError) as excinfo:
            app.reserve_port("127.0.0.1", start, span=1)
        message = str(excinfo.value)
        assert "连续 1 个端口都不可用" in message and "端口已被占用" in message, message
    finally:
        occupier.close()
    eacces = getattr(errno, "EACCES", 13)
    assert "权限不足" in app._port_blocked_reason(OSError(eacces, "denied"))
    reserved = OSError(eacces, "reserved")
    reserved.winerror = 10013
    reserved_reason = app._port_blocked_reason(reserved)
    assert "EACCES" in reserved_reason and "被占用" not in reserved_reason, reserved_reason
    in_use = OSError(getattr(errno, "EADDRINUSE", 10048), "in use")
    in_use.winerror = 10048
    in_use_reason = app._port_blocked_reason(in_use)
    assert "被占用" in in_use_reason and "权限不足" not in in_use_reason, in_use_reason


def test_170_f_server_run_failure_no_window(tmp_path, monkeypatch):
    """⑥ 本 server 起不来（bind 失败）⇒ 立刻判失败、不开窗、收尾完成。"""
    _require_170()
    identity = "s8-170-f-" + uuid.uuid4().hex[:8]
    rec = Recorder()
    rc, rec = _run_main(
        monkeypatch,
        rec,
        tmp_path,
        primary_log=tmp_path / "start.log",
        start_port=_free_port(),
        real_reserve=True,
        identity=identity,
        run_exc=RuntimeError("bind failed"),
    )
    assert rc == 1 and rec.windows == [] and rec.start_calls == 0, (rc, rec.windows, rec.start_calls)
    assert "就绪" in rec.notice_texts and "bind failed" in rec.notice_texts, rec.notice_texts
    assert rec.reserved[0][0].fileno() == -1, "失败路径也必须关闭预留 socket"
    assert _guard_is_free(identity), "失败路径也必须释放守卫"


def test_170_f2_thread_systemexit_is_failure_not_success(tmp_path, monkeypatch):
    """⑥ 服务线程里 SystemExit ⇒ 线程退出异常被记录 ⇒ 判失败、不开窗。"""
    _require_170()
    identity = "s8-170-f2-" + uuid.uuid4().hex[:8]
    rec = Recorder()
    rc, rec = _run_main(
        monkeypatch,
        rec,
        tmp_path,
        primary_log=tmp_path / "start.log",
        start_port=_free_port(),
        real_reserve=True,
        identity=identity,
        run_exc=SystemExit(3),
    )
    assert rc == 1 and rec.windows == [], (rc, rec.windows)
    assert "SystemExit" in rec.notice_texts, rec.notice_texts
    assert _guard_is_free(identity)


def test_170_f3_wait_server_ready_requires_this_server(tmp_path):
    """⑥ 就绪判定只认本次 server：线程抛过异常 / 线程已死 / 始终未 started 都不算就绪。"""
    _require_170()

    class FakeServerState:
        def __init__(self, started):
            self.started = started

    stop = threading.Event()
    errors = []
    alive = threading.Thread(target=lambda: stop.wait(30.0), daemon=True)
    alive.start()
    try:
        assert app.wait_server_ready(FakeServerState(True), alive, errors, timeout=1.0) is True
        errors.append("RuntimeError: boom")
        assert app.wait_server_ready(FakeServerState(True), alive, errors, timeout=1.0) is False, (
            "服务线程抛过异常 ⇒ 不得算就绪"
        )
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        assert app.wait_server_ready(FakeServerState(True), dead, [], timeout=1.0) is False, "线程已死 ⇒ 不得算就绪"
        assert app.wait_server_ready(FakeServerState(False), alive, [], timeout=0.2) is False, "始终未 started ⇒ 有界超时失败"
    finally:
        stop.set()
        alive.join(timeout=5.0)
    assert not hasattr(app, "wait_port"), "旧 wait_port（connect 即就绪）不得留作死函数"
    assert not hasattr(app, "find_free_port"), "旧 find_free_port（探测完即关）不得留作死函数"


def test_170_f4_foreign_listener_is_not_readiness(tmp_path):
    """⑥ 端口上有别的服务在应答 ⇒ 仍不得算「本次 server 就绪」（修前红探针 B1 的判据化）。"""
    _require_170()
    port = _free_port()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", port))
    listener.listen(4)

    class Foreign:
        started = False
        config = types.SimpleNamespace(host="127.0.0.1", port=port)

    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    try:
        assert app.wait_server_ready(Foreign(), dead, [], timeout=1.0) is False, (
            "异源 listener 在应答、本次 server 未 started 且线程已死 ⇒ 不得算就绪"
        )
        assert app.wait_server_ready(Foreign(), dead, ["RuntimeError: boom"], timeout=1.0) is False, (
            "服务线程抛过异常时，异源 listener 也不得把结果翻成就绪"
        )
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            assert probe.connect_ex(("127.0.0.1", port)) == 0, "探针前提：异源 listener 确实在应答"
        finally:
            probe.close()
    finally:
        listener.close()


def test_170_g_happy_path_and_full_cleanup(tmp_path, monkeypatch):
    """⑦ 正常启动与收尾：窗口参数保留、下载开关打开、预留 socket 关闭、服务线程收尾、守卫释放。"""
    _require_170()
    start = _free_port()
    identity = "s8-170-g-" + uuid.uuid4().hex[:8]
    rec = Recorder()
    rc, rec = _run_main(
        monkeypatch,
        rec,
        tmp_path,
        primary_log=tmp_path / "start.log",
        start_port=start,
        real_reserve=True,
        identity=identity,
    )
    assert rc == 0, rec.notice_texts
    assert rec.alembic_calls == 1 and len(rec.windows) == 1 and rec.start_calls == 1
    sock, port, blockers = rec.reserved[0]
    assert port == start and blockers == []
    window = rec.windows[0]
    width, height = app.window_size()
    assert window["args"][1] == "http://127.0.0.1:%d/" % port, window["args"]
    assert window["kwargs"]["text_select"] is True, window["kwargs"]
    assert window["kwargs"]["width"] == width and window["kwargs"]["height"] == height, window["kwargs"]
    assert sock.fileno() == -1, "main 收尾必须关闭预留 socket"
    server = rec.servers[0]
    assert server.should_exit is True and server.finished.wait(10.0), "服务线程必须收尾"
    assert sys.modules["webview"].settings["ALLOW_DOWNLOADS"] is True
    assert _guard_is_free(identity), "main 退出后守卫必须已释放（可再次启动）"


def test_170_h_window_exception_still_cleans_up(tmp_path, monkeypatch):
    """⑦ create_window 抛错 ⇒ 收尾照做（socket/线程/守卫），⛔ 不留半开状态。"""
    _require_170()
    identity = "s8-170-h-" + uuid.uuid4().hex[:8]
    rec = Recorder()
    rc, rec = _run_main(
        monkeypatch,
        rec,
        tmp_path,
        primary_log=tmp_path / "start.log",
        start_port=_free_port(),
        real_reserve=True,
        identity=identity,
        window_exc=RuntimeError("WebView2 缺失"),
    )
    assert rc == 1 and rec.start_calls == 0
    assert rec.reserved[0][0].fileno() == -1
    assert rec.servers[0].should_exit is True and rec.servers[0].finished.wait(10.0)
    assert _guard_is_free(identity)


def test_170_i_start_exception_still_cleans_up(tmp_path, monkeypatch):
    """⑦ start 抛错 ⇒ 收尾照做（socket/线程/守卫）。"""
    _require_170()
    identity = "s8-170-i-" + uuid.uuid4().hex[:8]
    rec = Recorder()
    rc, rec = _run_main(
        monkeypatch,
        rec,
        tmp_path,
        primary_log=tmp_path / "start.log",
        start_port=_free_port(),
        real_reserve=True,
        identity=identity,
        start_exc=RuntimeError("start 失败"),
    )
    assert rc == 1 and rec.start_calls == 1
    assert rec.reserved[0][0].fileno() == -1
    assert rec.servers[0].should_exit is True and rec.servers[0].finished.wait(10.0)
    assert _guard_is_free(identity)


def test_170_j_guard_name_has_no_credentials(tmp_path, monkeypatch):
    """① 守卫名 = 前缀 + sha256 前 32 位十六进制：⛔ 不含路径明文/用户名/凭据，且稳定。"""
    _require_170()
    monkeypatch.setattr(app, "data_root", lambda: tmp_path / "含空格 与中文" / "root")
    name = app.instance_guard_name()
    suffix = name[len(app.GUARD_NAME_PREFIX):]
    assert name.startswith(app.GUARD_NAME_PREFIX)
    assert len(suffix) == 32 and all(ch in "0123456789abcdef" for ch in suffix), suffix
    assert "\x00" not in name
    assert app.instance_guard_name() == name, "同一身份必须得到稳定的守卫名"
    assert app.instance_guard_name("other-identity") != name


def test_170_o_guard_failure_is_fail_closed(tmp_path, monkeypatch):
    """① 身份解析失败 ⇒ fail-closed：不静默放行第二个实例，⛔ 不迁移、⛔ 不开窗，给可读失败。"""
    _require_170()
    rec = Recorder()
    backend = _install_boundaries(
        monkeypatch,
        rec,
        tmp_path,
        primary_log=tmp_path / "start.log",
        start_port=_free_port(),
        real_reserve=True,
    )

    def broken_root():
        raise RuntimeError("数据根解析失败（测试替身）")

    def broken_name(identity=None):
        raise RuntimeError("身份解析失败（测试替身）")

    monkeypatch.setattr(app, "data_root", broken_root)
    with pytest.raises(RuntimeError, match="数据根解析失败"):
        _PRISTINE["instance_identity"]()
    monkeypatch.setattr(app, "instance_guard_name", broken_name)
    monkeypatch.chdir(backend.parent)
    saved_path = list(sys.path)
    try:
        rc = app.main()
    finally:
        sys.path[:] = saved_path
    assert rc == 1, "守卫建立失败必须 fail-closed（rc 1）"
    assert rec.alembic_calls == 0 and rec.windows == [], (rec.alembic_calls, rec.windows)
    assert "无法建立单实例守卫" in rec.notice_texts, rec.notice_texts


def test_170_l_real_uvicorn_serves_on_reserved_socket(tmp_path):
    """④ 复用已装 uvicorn 的 Server.run(sockets=[...])：预留端口真能对外应答，收尾后端口释放。"""
    _require_170()
    import uvicorn

    async def asgi(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        else:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": "zhilan-s8-170".encode("utf-8")})

    start = _free_port()
    sock, port, blockers = app.reserve_port("127.0.0.1", start)
    server = uvicorn.Server(uvicorn.Config(asgi, host="127.0.0.1", port=port, log_level="warning"))
    errors = []
    thread = threading.Thread(target=app._serve_in_thread, args=(server, sock, errors), daemon=True)
    thread.start()
    try:
        assert app.wait_server_ready(server, thread, errors, timeout=20.0) is True, errors
        with pytest.raises(OSError):
            _third_party_bind(port).close()
        with urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=5.0) as response:
            body = response.read()
        assert response.status == 200, response.status
        assert body == b"zhilan-s8-170", body
    finally:
        app._shutdown(server, thread, sock, None)
    assert not thread.is_alive(), "收尾后服务线程必须结束"
    rival = _third_party_bind(port)
    rival.close()


def test_170_n_real_uvicorn_lifespan_failure_is_not_ready(tmp_path):
    """④ lifespan 启动失败 ⇒ server.started 不为真 / 线程退出 ⇒ 就绪判定为 False（main 由此不开窗）。"""
    _require_170()
    import uvicorn

    async def broken_asgi(scope, receive, send):
        if scope["type"] == "lifespan":
            message = await receive()
            if message["type"] == "lifespan.startup":
                raise RuntimeError("lifespan boom")
            return
        raise RuntimeError("unexpected scope")

    start = _free_port()
    sock, port, blockers = app.reserve_port("127.0.0.1", start)
    server = uvicorn.Server(
        uvicorn.Config(broken_asgi, host="127.0.0.1", port=port, log_level="critical", lifespan="on")
    )
    errors = []
    thread = threading.Thread(target=app._serve_in_thread, args=(server, sock, errors), daemon=True)
    thread.start()
    try:
        ready = app.wait_server_ready(server, thread, errors, timeout=20.0)
        assert ready is False, "lifespan 失败不得被当成就绪"
        assert errors or not thread.is_alive(), (errors, thread.is_alive())
    finally:
        app._shutdown(server, thread, sock, None)
