"""知澜桌面客户端：迁移、选端口、启动服务、打开窗口。"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 冻结态（打包后）没有源码树：``__file__`` 落在临时解包目录，``ROOT/backend`` 与 ``__file__`` 本身
# 在会话结束后都会消失，故三处（后端目录、chdir 目标、日志文件）都必须改锚到可执行文件一侧。
# **开发态行为与改造前逐字一致**（阶段 6 · 条目 6.2 的验收标准就是这条）。
#
# 注意：判定必须**每次调用时读**，不能固化成 import 期常量——`sys.frozen` 是运行期属性，
# 固化后既无法测试、也与 `backend/main.py::_roots()` 的调用期判定不一致（晚绑定陷阱）。
FROZEN = bool(getattr(sys, "frozen", False))  # 快照常量，仅作可读展示；判定一律用下方函数


def is_frozen() -> bool:
    """是否运行在打包后的冻结态（每次调用实时读取，勿缓存）。"""
    return bool(getattr(sys, "frozen", False))


def backend_dir() -> Path:
    """后端目录：开发态 ``<仓库根>/backend``；冻结态是资源根（``main`` 由打包器内置，见条目 6.3）。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        return Path(meipass) if meipass else Path(sys.executable).resolve().parent
    return ROOT / "backend"


def log_file() -> Path:
    """日志文件：开发态 ``<仓库根>/client/start.log``；冻结态放可执行文件旁边（那里一定可写）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent / "start.log"
    return ROOT / "client" / "start.log"

# 初始窗口尺寸：黄金矩形 —— 宽取屏幕宽的黄金分割（0.618），长 = 宽 ÷ 1.618。
# 取不到屏幕信息（没有 tkinter / 无图形环境）时回落到这个尺寸。
FALLBACK_SIZE = (1054, 651)
GOLDEN = 1.618


def window_size() -> tuple[int, int]:
    """初始窗口尺寸，单位与 ``webview.create_window`` 一致（逻辑像素）。

    **宽 = 屏幕宽度 × 0.618（黄金分割），长 = 宽 ÷ 1.618** —— 窗口本身即黄金矩形。
    屏高不够时按屏高收一下，并给任务栏（48）与标题栏留出余量——否则窗口会比屏幕还高，
    标题栏或底部输入框会落到屏幕外。
    取不到屏幕信息（没有 tkinter / 无图形环境）时回落到 ``FALLBACK_SIZE``。
    """
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        root.destroy()
    except Exception:
        return FALLBACK_SIZE
    width = int(screen_w * 0.618)
    return width, min(int(width / GOLDEN), screen_h - 80)


# 条目 158：日志落点不可写时 **不得** 把异常抛出 main() —— console=False 的冻结成品里 stdout
# 根本看不见，异常一旦逃出去就是「窗口不开、用户没有任何提示」。
# 受控退路的落点只用标准库：tempfile 的临时目录（⛔ 不读 ROOT —— 冻结门禁只允许 ROOT 出现在
# backend_dir()/log_file() 内）。
LOG_FALLBACK_NAME = "zhilan-desktop-start.log"


def fallback_log_file() -> Path:
    """退路日志落点：标准库临时目录下的具名文件（与主落点无关，绝不读 ROOT）。"""
    return Path(tempfile.gettempdir()) / LOG_FALLBACK_NAME


def _write_console(line: str) -> bool:
    """控制台那一份（原 print(line, end="", flush=True) 的等价写法）；失败只记 False，不打断调用方。"""
    if getattr(sys, "stdout", None) is None:  # PyInstaller --windowed 下 stdout 可能是 None
        return False
    try:
        print(line, end="", flush=True)
    except Exception:  # noqa: BLE001 — 条目 158：日志失败不许让调用方失败
        return False
    return True


def _write_primary_log(line: str) -> bool:
    """主落点（log_file()）追加一行：语义与改造前逐字一致，区别只是失败返回 False 而不抛出。"""
    try:
        target = log_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001
        return False
    return True


def _write_to_stderr(line: str) -> bool:
    """退路一：stderr 直写。⛔ 这里绝不能再回调 log() —— stderr 也失败时就成递归了（条目 158）。"""
    stream = getattr(sys, "stderr", None)
    if stream is None:
        return False
    try:
        stream.write(line)
        stream.flush()
    except Exception:  # noqa: BLE001
        return False
    return True


def _append_to_fallback_file(line: str) -> bool:
    """退路二：临时目录里的具名文件。全部退路都失败 ⇒ 静默放弃这一行日志，但窗口照样要能开。"""
    try:
        target = fallback_log_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001
        return False
    return True


def log(message: str) -> None:
    """打印一行并追加到日志文件。**永不抛**（只吞 Exception；KeyboardInterrupt / SystemExit 照旧穿出）。

    条目 158：日志落点不可写（只读安装目录、被同名普通文件占位、被锁住……）时，原实现让
    log_file().parent.mkdir / open 的异常直接逃出 main()；console=False 的冻结成品里连 print 都
    落不到用户眼前 ⇒ 窗口永远不出现。这里改成受控降级，**只允许日志副作用**：
    控制台一份 → 主落点一份 → 失败则 stderr 一份 → 再失败则标准库临时目录下的具名文件一份
    → 全都失败就放弃这一行日志。既有主落点语义不变（开发态 <仓库根>/client/start.log、
    冻结态 exe 旁 start.log）。"""
    line = message.rstrip() + "\n"
    _write_console(line)
    if not _write_primary_log(line):
        _write_to_stderr(line)
        _append_to_fallback_file(line)


SERVER_READY_TIMEOUT = 30.0
SHUTDOWN_TIMEOUT = 5.0
ALREADY_RUNNING_MESSAGE = "知澜已运行，请切换到已有窗口。"
# 具名内核对象名不带命名空间前缀，默认就落在当前 Windows 会话作用域（Local 语义，跨会话互不干扰）；
# 身份串里另带会话号，⛔ 不用 Global 前缀，名字里也不放路径明文或用户名。
GUARD_NAME_PREFIX = "zhilan-desktop-"
ERROR_ALREADY_EXISTS = 183

_KERNEL32 = None


def _kernel32():
    """kernel32 句柄（模块级缓存）。argtypes/restype 必须写全：HANDLE 不能被截成 32 位。"""
    global _KERNEL32
    if _KERNEL32 is None:
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise RuntimeError("当前平台没有 ctypes.WinDLL：条目 170 的单实例守卫只在 Windows 上可用")
        _KERNEL32 = win_dll("kernel32", use_last_error=True)
    return _KERNEL32


def _port_blocked_reason(exc: OSError) -> str:
    """候选端口不可用的可读原因（条目 170 ④：EACCES 不许说成「被占用」）。"""
    if getattr(exc, "errno", None) == errno.EACCES or getattr(exc, "winerror", None) == 10013:
        return "Windows 保留了该端口或权限不足（EACCES）"
    if getattr(exc, "errno", None) == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
        return "端口已被占用"
    return str(exc) or exc.__class__.__name__


def reserve_port(host: str, start: int, span: int = 50) -> tuple[socket.socket, int, list[str]]:
    """向上找一个能绑定、并**一直持有到 uvicorn 接管**的端口，返回 (socket, 端口, 被跳过的原因)。

    条目 170 ②：改造前的 find_free_port 用 with 包住 socket，探测成功就立刻关闭——「探测到空闲」
    不等于「预留」，别的进程（含另一个知澜）可以随手抢走它（修前红探针 A1/A3/A4 已复现）。这里
    bind 成功即把 socket 交给调用方持有；Windows 上显式 SO_EXCLUSIVEADDRUSE，不许重复绑定。
    """
    blockers: list[str] = []
    span = min(span, 65536 - start)
    for port in range(start, start + span):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            sock.bind((host, port))
        except OSError as exc:
            blockers.append(f"{port}：{_port_blocked_reason(exc)}")
            sock.close()
            continue
        return sock, port, blockers
    detail = "；".join(blockers[-5:]) if blockers else "没有任何候选端口"
    raise RuntimeError(f"从 {start} 起连续 {span} 个端口都不可用（{detail}）")


def wait_server_ready(server, thread, errors: list, timeout: float = SERVER_READY_TIMEOUT) -> bool:
    """等**本次这个** uvicorn server 真的就绪（条目 170 ④）。

    改造前的 wait_port 只看「端口有人应答」：别人的服务会被当成就绪、本进程 bind/启动失败也可能
    误判成功（修前红探针 B1 已复现）。这里只认本次 server.started 且服务线程仍存活；线程里抛过
    异常（errors 非空）或线程已经结束都算失败；等待用 time.monotonic 有界。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if errors:
            return False
        if getattr(server, "started", False):
            return thread.is_alive()
        if not thread.is_alive():
            return False
        time.sleep(0.1)
    return False


def _serve_in_thread(server, sock, errors: list) -> None:
    """在后台线程里跑 uvicorn，并把任何退出异常收进 errors（⛔ 不让线程静默死掉）。"""
    try:
        server.run(sockets=[sock])
    except BaseException as exc:  # noqa: BLE001 — 线程边界：任何退出都要能被就绪判定看到
        errors.append(f"{exc.__class__.__name__}: {exc}")


def data_root() -> Path:
    """当前安装对应的持久数据根——条目 170 单实例身份的唯一来源。

    ⛔ 不用 backend_dir()/ROOT/sys._MEIPASS/PID/端口/随机数：身份必须来自**已解析**的数据根
    （与 .env、数据库同一处解析器）。解析失败 fail-closed：宁可拒绝启动，也不能拿错身份开第二个实例。
    """
    from app.core.env_store import resolve_data_root

    return Path(resolve_data_root())


def _current_session_id() -> int:
    """当前 Windows 会话号；拿不到就 -1（身份里带上它，跨会话互不干扰）。"""
    try:
        session = ctypes.c_ulong()
        kernel32 = _kernel32()
        if kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session)):
            return int(session.value)
    except Exception:  # noqa: BLE001 — 取不到只影响粒度，不该让启动失败
        pass
    return -1


def instance_identity(root=None) -> str:
    """单实例身份 = 「持久数据根 + Windows 当前会话」规范化后的稳定字符串。"""
    base = Path(root) if root is not None else data_root()
    normalized = os.path.normcase(os.path.normpath(str(base)))
    return f"{normalized}|session={_current_session_id()}"


def instance_guard_name(identity: str | None = None) -> str:
    """守卫名 = 前缀 + 身份规范化后的 sha256 前 32 位（⛔ 名字里不含路径明文/用户名/凭据）。"""
    text = instance_identity() if identity is None else str(identity)
    digest = hashlib.sha256(os.path.normcase(text).encode("utf-8")).hexdigest()[:32]
    return f"{GUARD_NAME_PREFIX}{digest}"


def acquire_instance_guard(identity: str | None = None):
    """取得当前安装的单实例守卫（Windows 内核具名互斥体）。

    返回句柄（整个 main 期间都必须持有，退出时 release_instance_guard）；返回 None = 已有实例在跑；
    原生调用失败 ⇒ 抛 RuntimeError（带可读原因；fail-closed，⛔ 不许静默放行第二个实例）。
    """
    name = instance_guard_name(identity)
    kernel32 = _kernel32()
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, 0, name)
    last_error = ctypes.get_last_error()
    if not handle:
        detail = ctypes.FormatError(last_error) if last_error else "未知原生错误"
        raise RuntimeError(f"无法建立单实例守卫（{name}）：{detail}")
    if last_error == ERROR_ALREADY_EXISTS:
        release_instance_guard(handle)  # 已有实例：拿到的这个句柄也要关掉（⛔ 不留多余引用）
        return None
    return handle


def release_instance_guard(handle) -> None:
    """释放守卫句柄（幂等；⛔ 不删任何文件，也⛔ 不用「删锁文件」的方式解锁）。"""
    if not handle:
        return
    try:
        _kernel32().CloseHandle(ctypes.c_void_p(handle))
    except Exception:  # noqa: BLE001 — 释放失败不该影响退出流程
        pass


def _native_message(title: str, message: str) -> bool:
    """原生提示框：console=False 的冻结成品里 stdout/stderr 都可能不可见（条目 170 ①）。

    ⛔ 只做提示：取不到 user32 或调用失败都返回 False，绝不抛（⛔ 不能因此开不了窗口）。
    """
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW(None, message, title, 0x00000040)  # MB_OK | MB_ICONINFORMATION
        return True
    except Exception:  # noqa: BLE001
        return False


def notify(title: str, message: str) -> None:
    """给用户看的提示：先落日志（可能不可见），再弹原生框（可能不可用）；两者都不抛。"""
    log(message)
    _native_message(title, message)


def _shutdown(server, thread, reserved, guard) -> None:
    """收尾（条目 170 ⑤）：请求服务退出 → 有界 join → 关闭预留 socket → 释放守卫。"""
    try:
        if server is not None:
            server.should_exit = True
    except Exception:  # noqa: BLE001
        pass
    if thread is not None:
        try:
            thread.join(timeout=SHUTDOWN_TIMEOUT)
        except Exception:  # noqa: BLE001
            pass
    if reserved is not None:
        try:
            reserved.close()
        except Exception:  # noqa: BLE001
            pass
    release_instance_guard(guard)


def allow_downloads() -> bool:
    """打开 pywebview 的下载开关，返回是否成功。

    **条目 50 真机症状的桌面端根因**：pywebview 的 Edge(WebView2) 后端默认
    ``settings['ALLOW_DOWNLOADS'] = False``，而它的 ``on_download_starting`` 在这个开关关着时
    直接 ``args.Cancel = True`` —— **每一个下载都被整条静默取消**：完成页显示「已生成并开始下载」，
    磁盘上（下载目录 / 桌面 / 项目目录）一个文件都没有，下载记录里也留不下条目。
    打开后走原生「另存为」对话框（默认目录＝系统下载目录），用户可自选保存路径。
    """
    try:
        import webview

        # settings 是 ImmutableDict：只允许改已有键，这里正是官方口径的改法
        webview.settings["ALLOW_DOWNLOADS"] = True
        return True
    except Exception as exc:  # noqa: BLE001 — 老版本 pywebview 没有这个键，不能因此开不了窗口
        log(f"未能开启客户端下载：{exc}")
        return False


def run_alembic() -> None:
    from alembic import command
    from alembic.config import Config

    backend = backend_dir()
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    command.upgrade(config, "head")


def _startup_error(exc: Exception) -> str:
    """复用后端日志脱敏；后端不可导入时只报告异常类型，避免泄露凭据。"""
    try:
        from app.core.app_logging import redact

        return redact(f"{type(exc).__name__}: {exc}")
    except Exception:
        return type(exc).__name__


def main() -> int:
    backend = backend_dir()
    sys.path.insert(0, str(backend))
    os.chdir(backend)

    try:
        import webview
    except Exception as exc:
        notify("知澜启动失败", f"无法加载 PyWebview 桌面组件：{_startup_error(exc)}。请重新安装知澜。")
        return 1

    try:
        from app.core.config import settings

        configured_port = settings.PORT
    except Exception:
        configured_port = os.environ.get("PORT") or 8000
    try:
        start = int(configured_port)
        if not 1 <= start <= 65535:
            raise ValueError("PORT out of range")
    except (TypeError, ValueError, OverflowError):
        notify("知澜启动失败", "PORT 必须是 1 到 65535 之间的整数，请检查端口设置。")
        return 1

    # 条目 170 ①：先拿单实例守卫，必须早于迁移与服务线程。重复进程在这里就退出：
    # ⛔ 不得再迁移、不得再抢端口、更不得开第二个窗口。
    try:
        guard = acquire_instance_guard()
    except Exception as exc:
        notify("知澜启动失败", f"无法建立单实例守卫：{exc}")
        return 1
    if guard is None:
        notify("知澜", ALREADY_RUNNING_MESSAGE)
        return 0

    server = None
    thread = None
    reserved = None
    try:
        # 条目 170 ②：端口预留——探测成功就一路持有这块 socket，直到 uvicorn 真正接管。
        try:
            reserved, port, blockers = reserve_port("127.0.0.1", start)
        except RuntimeError as exc:
            notify("知澜启动失败", str(exc))
            return 1
        if port != start:
            reason = f"（{blockers[0]}）" if blockers else ""
            log(f"端口 {start} 被占用，已切换到 {port}{reason}")

        try:
            run_alembic()
        except Exception as exc:
            log(f"数据库迁移未完成：{exc}")

        try:
            import uvicorn
            from main import app

            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info"))
            serve_errors: list[str] = []
            thread = threading.Thread(
                target=_serve_in_thread, args=(server, reserved, serve_errors), daemon=True
            )
            thread.start()
        except Exception as exc:
            notify("知澜启动失败", f"无法启动后端服务：{_startup_error(exc)}。请检查启动日志或重新安装知澜。")
            return 1
        # 条目 170 ④：只认**本次** server 的就绪状态，⛔ 不再把「端口有人应答」当就绪。
        if not wait_server_ready(server, thread, serve_errors):
            detail = f"：{serve_errors[0]}" if serve_errors else ""
            notify(
                "知澜启动失败",
                f"后端服务未能在 {SERVER_READY_TIMEOUT:.0f} 秒内就绪，已放弃打开窗口{detail}",
            )
            return 1

        url = f"http://127.0.0.1:{port}/"
        width, height = window_size()
        log(f"窗口尺寸：{width}x{height}（逻辑像素；可拖动边缘自行调整）")
        try:
            # 条目 50：先开下载开关——否则桌面端所有下载都被 pywebview 静默取消（PPT 导出真机失败的
            # 桌面端根因，见 allow_downloads 的说明）。
            allow_downloads()
            # 条目 49：允许在页面里选中文字。pywebview 的 text_select 默认是 False，会在页面里
            # 注入 `body{user-select:none; cursor:default}`——整个界面（包括回答正文）都拖不出
            # 选区，Ctrl+C 也只复制到空。这里显式打开，正文恢复可选；界面里真正的「装饰」
            # （按钮 / 标签 / 拖拽把手）由 index.css 单独标记不可选。
            webview.create_window("知澜", url, width=width, height=height, text_select=True)
            webview.start()
        except Exception as exc:
            notify(
                "知澜启动失败",
                f"无法打开桌面窗口：{_startup_error(exc)}。\n请确认 Microsoft Edge WebView2 可用，或重新安装知澜。",
            )
            return 1
        return 0
    finally:
        # 条目 170 ⑤：所有出口（正常关窗 / 任何 return / 抛错）都要收尾：退服务、关预留 socket、放守卫。
        _shutdown(server, thread, reserved, guard)
