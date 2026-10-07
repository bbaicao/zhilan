"""stdlib-only fake MCP server for tests（开工包 D23）.

Not collected by pytest（文件名不匹配 ``test_*.py``）——只当子进程脚本用。

Modes
-----
``normal``    顶层标量 + 一个必填（A18 正向对照）
``nested``    顶层字段是 ``object``（内含 ``required``）（A19 过滤腿）
``is_error``  ``tools/call`` 回 ``result.isError = true``（A20）
``hang``      收到 ``tools/call`` 后永不回（A21② 超时腿）
``crlf``      **成帧**改成 ``\r\n`` 结尾（CRLF 半支持族的对称件，见下）
``half``      **成帧**改成「先写一半 → flush → 等 0.2s → 写另一半」（半包 / 跨读重组）
``malformed`` **成帧**改成坏 JSON（每行都故意坏掉）
``noisy``     协议照常，但**持续向 stderr 唠叨**（`回归修复` 载体：启动先同步刷
一批 ~130KB + 后台线程持续刷，保证握手期间 stderr 管道恒有写入/恒满）

> 后三个是**成帧**维度（``crlf`` / ``half`` / ``malformed``）而不是协议维度：它们回答的是
> 「客户端拿到**不规整的字节流**时会不会炸穿 / 会不会把原始 JSON 当正文」——
> `回归修复`（协议块 CRLF 行尾半支持）的**漏检成因**正是夹具面只有合法 JSON-RPC
> （判据面补钉 H-3）。

Protocol
--------
换行分隔 JSON-RPC 2.0 + UTF-8，**每次写出后必须 flush**。
``initialize`` 响应里 **恒回** ``protocolVersion = "2024-11-05"``（与仓内
``app.api.mcp_probe.PROTOCOL_VERSION`` 同值），不跟随客户端 offer
（客户端 offer 是 ``2025-11-25``；回不受支持的版本会直接 ``RuntimeError``）。

握手顺序 = ``initialize`` → ``notifications/initialized``（通知，不回）→
``tools/list`` → ``tools/call``。

Invocation
----------
``command=sys.executable``,
``args=[本文件绝对路径, "--mode", <mode>, "--pid-file", <path>]``。

pid 文件
--------
每次进程启动时把自己的 ``os.getpid()`` **覆盖写**进 pid 文件（语义 =
「最近一次调用的子进程 pid」——A21 按多次「新会话」构造时注意只留最后一条）。
路径取值优先级：``--pid-file`` > 环境变量 ``FAKE_MCP_PID_FILE`` >
``<tempdir>/fake_mcp_server.pid``。

Fixed strings（用例可直接断言）
------------------------------
``TOOL_NAME`` = ``fake_mcp_ping``
``OK_TEXT``   = ``FAKE_MCP_OK``     （normal / is_error=0 的 ``tools/call`` 文本）
``ERR_TEXT``  = ``FAKE_MCP_ERROR``  （is_error 模式的 ``tools/call`` 文本）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "fake-mcp-server"
SERVER_VERSION = "0.1.0"
TOOL_NAME = "fake_mcp_ping"
TOOL_DESCRIPTION = "Fake MCP ping tool (D23 fixture)."
OK_TEXT = "FAKE_MCP_OK"
ERR_TEXT = "FAKE_MCP_ERROR"
PID_ENV = "FAKE_MCP_PID_FILE"
MODES = ("normal", "nested", "is_error", "hang", "crlf", "half", "malformed", "noisy")

#: **成帧**维度（判据面补钉 H-3）：这三档不换协议、只换字节流形态。
FRAMING_MODES = ("crlf", "half", "malformed")

#: ``noisy`` 模式的噪音行与启动批量：``NOISY_LINE * NOISY_BATCH_LINES ≈ 4MB``，
#: **单笔**写出（一行一次 syscall 的分批写可能被 OS 管道缓冲整体吃下——实测 Windows
#: 匿名管道缓冲远超 64KB 常说值，120KB 分批写未阻塞），单笔 4MB 写在「``stderr=PIPE``
#: 且无人排空」时**必然阻塞**（管道缓冲吃满后 write 挂起，服务循环开始前就卡死），
#: 而在 ``DEVNULL`` 下瞬时完成（`回归修复` 载体）。
NOISY_LINE = b"[noisy] fake-mcp-server chatty stderr line " + b"x" * 80 + b"\n"
NOISY_BATCH_LINES = 32768

#: normal：顶层标量（string）+ 一个必填。
NORMAL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "description": "message to echo"},
    },
    "required": ["message"],
}

#: nested：顶层字段是 object（内含 required）——上线过滤应判 ``nested_object``。
NESTED_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "options": {
            "type": "object",
            "description": "nested options object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    "required": ["options"],
}


def _send(obj: dict, mode: str = "normal") -> None:
    """写出一行 JSON-RPC 并强制 flush（D23：必须 flush）。

    ``mode`` 里的**成帧**三档在这里生效（其余模式与改动前逐字同形）：
    ``crlf`` 换行尾、``half`` 分两次写、``malformed`` 把 JSON 故意写坏。
    """
    text = json.dumps(obj, ensure_ascii=False)
    if mode == "malformed":
        text = text[:-1] + ",}"  # 尾对象里多一个逗号 ⇒ 不是合法 JSON
    payload = text.encode("utf-8") + (b"\r\n" if mode == "crlf" else b"\n")
    if mode == "half":
        cut = max(1, len(payload) // 2)
        sys.stdout.buffer.write(payload[:cut])
        sys.stdout.buffer.flush()
        time.sleep(0.2)  # 让对端**至少读两次**才能拼出完整帧
        sys.stdout.buffer.write(payload[cut:])
        sys.stdout.buffer.flush()
        return
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def _reply(req_id, result: dict | None = None, error: dict | None = None, mode: str = "normal") -> None:
    resp: dict = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result if result is not None else {}
    _send(resp, mode)


def _write_pid(path: Path) -> None:
    """把自己的 pid 覆盖写进 pid 文件，并 flush。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
        fh.flush()


def _schema_for(mode: str) -> dict:
    return NESTED_SCHEMA if mode == "nested" else NORMAL_SCHEMA


def _initialize_result() -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _tools_list_result(mode: str) -> dict:
    return {
        "tools": [
            {
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "inputSchema": _schema_for(mode),
            }
        ]
    }


def _tools_call_result(mode: str) -> dict:
    if mode == "is_error":
        return {
            "content": [{"type": "text", "text": ERR_TEXT}],
            "isError": True,
        }
    return {
        "content": [{"type": "text", "text": OK_TEXT}],
        "isError": False,
    }


def _resolve_pid_path(cli_pid_file: str | None) -> Path:
    if cli_pid_file:
        return Path(cli_pid_file)
    env = os.environ.get(PID_ENV)
    if env:
        return Path(env)
    return Path(tempfile.gettempdir()) / "fake_mcp_server.pid"


def _start_stderr_noise() -> None:
    """（``noisy`` 模式）启动先**同步**刷一批 stderr，再起后台线程持续刷。

    目标：握手的整个生命周期内 stderr 管道恒满/持续有写——
    父进程用 ``DEVNULL`` 时写入照单全收、服务不受影响；
    父进程用 ``PIPE`` 且全程无人读 ⇒ 子进程阻塞在 stderr 写上（`回归修复` 的
    「唠叨 server」载体）。启动批量**单笔**写出：管道缓冲吃满后 write 挂起，子进程在
    服务循环开始之前就阻塞，正好复现「连 initialize 都回不出来」的缺陷形态。
    """
    import threading

    def _flood() -> None:
        while True:
            try:
                sys.stderr.buffer.write(NOISY_LINE)
                sys.stderr.buffer.flush()
            except (OSError, ValueError):
                return  # 管道已关（父进程 kill 收尸）⇒ 安静退出

    try:
        sys.stderr.buffer.write(NOISY_LINE * NOISY_BATCH_LINES)
        sys.stderr.buffer.flush()
    except (OSError, ValueError):
        return
    threading.Thread(target=_flood, daemon=True).start()


def _handle_request(msg: dict, mode: str) -> bool:
    """处理一条 JSON-RPC 消息。

    返回 ``False`` 表示应永久挂起（``hang`` 模式收到 ``tools/call``）。
    通知（无 ``id`` 且有 ``method``）直接忽略、不回。
    """
    method = msg.get("method")
    if method is None:
        return True

    has_id = "id" in msg
    if not has_id:
        # JSON-RPC notification：不回。典型是 notifications/initialized。
        return True

    req_id = msg.get("id")

    if method == "initialize":
        _reply(req_id, _initialize_result(), mode=mode)
        return True
    if method == "tools/list":
        _reply(req_id, _tools_list_result(mode), mode=mode)
        return True
    if method == "tools/call":
        if mode == "hang":
            return False
        _reply(req_id, _tools_call_result(mode), mode=mode)
        return True
    if method == "ping":
        _reply(req_id, {}, mode=mode)
        return True

    _reply(
        req_id,
        error={"code": -32601, "message": f"Method not found: {method}"},
        mode=mode,
    )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="stdlib-only fake MCP stdio server (D23)")
    parser.add_argument(
        "--mode",
        choices=MODES,
        default="normal",
        help="normal | nested | is_error | hang | crlf | half | malformed",
    )
    parser.add_argument(
        "--pid-file",
        default=None,
        help="overwrite with os.getpid() on startup (default: env FAKE_MCP_PID_FILE or tempdir)",
    )
    args = parser.parse_args(argv)

    _write_pid(_resolve_pid_path(args.pid_file))

    if args.mode == "noisy":
        _start_stderr_noise()

    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            break
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(msg, dict):
            continue
        if not _handle_request(msg, args.mode):
            # hang：收到 tools/call 后永不回，进程保持存活直至被杀。
            while True:
                time.sleep(3600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
