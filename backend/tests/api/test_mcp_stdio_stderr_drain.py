"""`回归修复` · `list_tools_stdio` 的 ``stderr=PIPE`` 全程无人排空 ⇒ 唠叨 server 假死到超时。

## 缺陷形态（修前）

``mcp_probe.list_tools_stdio`` 起子进程时 ``stderr=asyncio.subprocess.PIPE``，但全文件
**零读取点**（finally 只 ``kill()+wait()``）。启动期向 stderr 打日志的 server 写满管道
缓冲（约 64KB）后**自身阻塞**在 stderr 写上，永远回不了 ``tools/list`` ⇒ 8 秒后
``_read_json_line`` 误报「MCP server 响应超时」——错误归因（超时）与真因（管道满）
南辕北辙。

## 修法

``stderr=asyncio.subprocess.DEVNULL``：stderr 产物全文件零消费 ⇒ 诊断面零损失；
并发排空（案 B）可保可诊断性，登记在缺陷账本、本笔不做。

## 载体

``fake_mcp_server`` 的 ``noisy`` 模式（协议照常 + 启动先同步刷 ~120KB stderr +
后台线程持续刷）⇒ 探测必须照常完成握手（``error == ""`` 且工具清单非空）；
修前该腿**必然**超时失败。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from app.api import mcp_probe
from tests.api import fake_mcp_server

FAKE = Path(fake_mcp_server.__file__).with_name("fake_mcp_server.py")
assert FAKE.is_file()

TOOL = fake_mcp_server.TOOL_NAME


def _noisy_command() -> list[str]:
    return [sys.executable, str(FAKE), "--mode", "noisy"]


def test_noisy_fixture_is_declared_in_modes():
    """夹具契约：``noisy`` 在 ``MODES`` 里、且**不属于**成帧三档（成帧集合不得被污染）。"""
    assert "noisy" in fake_mcp_server.MODES
    assert "noisy" not in fake_mcp_server.FRAMING_MODES
    assert set(fake_mcp_server.FRAMING_MODES) == {"crlf", "half", "malformed"}


def test_probe_survives_a_chatty_stderr_server():
    """**主腿**：探唠叨 server ⇒ 照常完成握手。

    修前：子进程阻塞在 stderr 写上 ⇒ ``error`` 含「MCP server 响应超时」；
    修后（DEVNULL）：stderr 写入不阻塞 ⇒ ``error == ""`` 且列出工具。
    """
    tools, error = asyncio.run(mcp_probe.probe_server("stdio", _noisy_command(), ""))
    assert error == "", error
    assert [tool["name"] for tool in tools] == [TOOL]
