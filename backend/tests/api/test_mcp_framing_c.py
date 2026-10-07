"""判据面补钉 **H-3** · 假 MCP server 的**成帧畸形档**对称用例（crlf / half / malformed）。

## 为什么补这一件

`回归修复`（协议块 CRLF 行尾半支持 ⇒ 结束决策静默失效 + 原始 JSON 漏进可见正文）的
**漏检成因**是夹具面只有「合法 JSON-RPC 四模式」（`normal|nested|is_error|hang`）——
**成帧**维度（行尾 / 分包 / 坏 JSON）整类零覆盖。收口总账把它列为 **§3.8 H-3**。

本文件把三种成帧形态钉成**对称**用例：

- **能容忍的要证明它真的容忍**（`crlf` / `half` ⇒ 发现与真调用都通）；
- **不能容忍的要证明它是可读失败而不是炸穿**（`malformed` ⇒ 零 specs、不抛、日志留痕）。

## 夹具自证（防空操作）

新加的 `--mode` 如果只是「名字变了、字节流没变」，上面每条断言都会**静默通过**
（正是 H-2 记的「空操作变异可静默 PASS」）。故本文件另有一条**夹具自证**：
直接读子进程 stdout 的**原始字节**，证明 `crlf` 真的是 `\\r\\n`、`half` 真的是分两次写、
`malformed` 真的不是合法 JSON。

⚠️ 真起子进程（业主已授权测试起 MCP 子进程，第 2 批前提③）。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.agent import mcp_transport
from app.api import mcp_probe
from app.core.config import settings
from tests.api import fake_mcp_server

FAKE = Path(__file__).with_name("fake_mcp_server.py")
assert FAKE.is_file()

TOOL = fake_mcp_server.TOOL_NAME


def _raw(framing: str) -> str:
    pack = {
        "slug": "fake",
        "name": "假 MCP 包",
        "transport": "stdio",
        "enabled": True,
        "available": True,
        "command": [sys.executable, str(FAKE), "--mode", framing],
        "url": "",
        "tools": [{"name": TOOL, "enabled": True, "dangerous": False}],
    }
    return json.dumps([pack], ensure_ascii=False)


def _allow(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MCP_ALLOW_SERVERS", "fake")
    monkeypatch.setattr(settings, "MCP_ALLOW_EGRESS", "")


def _command(framing: str) -> list[str]:
    return [sys.executable, str(FAKE), "--mode", framing]


# --------------------------------------------------------------------------
# 夹具自证：原始字节流真的是那个形态（防空操作）
# --------------------------------------------------------------------------


def _read_line_raw(framing: str, *, first_only: bool = False) -> tuple[bytes, bytes]:
    """起假 server、发一条 ``initialize``、**按原始字节**读回响应。

    返回 ``(首块, 累计)``。进程由看门狗在 20s 后强杀（夹具失手时不会挂住整轮）。
    """
    request = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"
    proc = subprocess.Popen(
        [sys.executable, str(FAKE), "--mode", framing],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    killer = None
    try:
        proc.stdin.write(request)
        proc.stdin.flush()
        fd = proc.stdout.fileno()
        first = os.read(fd, 65536)
        if first_only:
            return first, first
        deadline = time.time() + 5.0
        acc = first
        while not acc.endswith(b"\n") and time.time() < deadline:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            acc += chunk
        return first, acc
    finally:
        proc.kill()
        proc.wait(timeout=10)
        if killer is not None:  # pragma: no cover - 预留
            killer.cancel()


def test_c_framing_fixture_declares_the_three_modes():
    """夹具契约：三个成帧档必须在 ``MODES`` 里、且在 ``FRAMING_MODES`` 里各就各位。"""
    assert set(fake_mcp_server.FRAMING_MODES) == {"crlf", "half", "malformed"}
    assert set(fake_mcp_server.FRAMING_MODES) <= set(fake_mcp_server.MODES)


def test_c_framing_fixture_really_emits_crlf_and_malformed():
    """`crlf` 的行尾真是 ``\\r\\n``；`malformed` 真不是合法 JSON（**不能是空操作**）。"""
    _, crlf_line = _read_line_raw("crlf")
    assert crlf_line.endswith(b"\r\n"), crlf_line[-40:]
    # CRLF 只是**行尾**：JSON 本身仍然合法（`json.loads` 容忍尾随空白）——这正是「半支持」
    # 那一族的关键差别：坏的是**成帧**，不是内容。
    assert json.loads(crlf_line.decode("utf-8"))["jsonrpc"] == "2.0"

    _, bad_line = _read_line_raw("malformed")
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad_line.decode("utf-8"))
    assert b'"jsonrpc"' in bad_line, "坏包必须仍是「看起来像」的帧（否则测不出后端的宽容度）"

    _, normal_line = _read_line_raw("normal")
    assert normal_line.endswith(b"\n") and not normal_line.endswith(b"\r\n")
    assert json.loads(normal_line.decode("utf-8"))["jsonrpc"] == "2.0"


def test_c_framing_fixture_really_splits_the_half_mode():
    """`half` 真的是**分两次写**：首读拿到的字节 < 整帧，且首读没有行尾。"""
    first, whole = _read_line_raw("half")
    assert not first.endswith(b"\n"), first[-40:]
    assert 0 < len(first) < len(whole), (len(first), len(whole))
    assert json.loads(whole.decode("utf-8"))["jsonrpc"] == "2.0"


# --------------------------------------------------------------------------
# 对称用例 · 能容忍的两档
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("framing", ["crlf", "half"])
async def test_c_framing_discovery_tolerates_crlf_and_half(framing, monkeypatch):
    """CRLF 行尾 / 半包 ⇒ **发现层照常拿到工具**（成帧不得影响协议判定）。"""
    _allow(monkeypatch)
    outcome = await mcp_transport._adiscover(raw=_raw(framing))
    assert [spec.name for spec in outcome.specs] == [f"mcp:fake:{TOOL}"], outcome.skipped


@pytest.mark.asyncio
@pytest.mark.parametrize("framing", ["crlf", "half"])
async def test_c_framing_call_tolerates_crlf_and_half(framing, monkeypatch):
    """同一档下**真调用**也通（发现与调用是两段读取，别只测一段）。"""
    _allow(monkeypatch)
    outcome = await mcp_transport._adiscover(raw=_raw(framing))
    assert outcome.specs
    spec = outcome.specs[0]
    obs = await spec.handler(spec.parameters(message="hi"))
    assert obs.ok is True and fake_mcp_server.OK_TEXT in obs.text


@pytest.mark.parametrize("framing", ["crlf", "half"])
def test_c_framing_settings_probe_tolerates_crlf_and_half(framing):
    """设置页探测层（`mcp_probe`）同样容忍这两档（它走的是**另一条**读取实现）。"""
    tools, error = asyncio.run(mcp_probe.probe_server("stdio", _command(framing), ""))
    assert error == "", error
    assert [tool["name"] for tool in tools] == [TOOL]


# --------------------------------------------------------------------------
# 对称用例 · 不能容忍的一档：可读失败，不炸穿、不漏原文进可见面
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c_framing_malformed_discovery_degrades_without_crashing(monkeypatch, caplog):
    """坏 JSON ⇒ 发现层**零 specs、不抛**（I5），且**日志里留痕**（可排障）。

    ⚠️ 关键：`_adiscover` 不能把「原始 JSON」当正文交给模型 —— 这条正是 `回归修复`
    的另一半（原始 JSON 漏进可见正文）。零 specs ⇒ 模型清单里根本没有这条工具。
    """
    _allow(monkeypatch)
    with caplog.at_level("WARNING", logger="app.agent.mcp_transport"):
        outcome = await mcp_transport._adiscover(raw=_raw("malformed"))
    assert outcome.specs == []
    # 209 修复后 async 腿也记账：坏 JSON 走 except 分支 ⇒ 与同步腿同构的 discover_failed 条目。
    assert outcome.skipped == [{"pack": "fake", "reason": "discover_failed", "transport": "stdio"}]
    assert "发现失败" in caplog.text, caplog.text


def test_c_framing_malformed_probe_is_readable_and_bounded():
    """坏 JSON ⇒ 探测层 `ok=False` + **可读** error（不 500）；回显**有界**（不吐整条流）。"""
    tools, error = asyncio.run(mcp_probe.probe_server("stdio", _command("malformed"), ""))
    assert tools == []
    assert error and "不是 JSON" in error, error
    assert len(error) <= 200, f"设置页 error 必须有界（实测实现回显 text[:120]）：{len(error)}"
    assert "Traceback" not in error


@pytest.mark.asyncio
async def test_c_framing_normal_mode_is_the_untouched_control(monkeypatch):
    """正控：`normal` 档逐字等价于改动前（三档新行为不得污染既有那一档）。"""
    _allow(monkeypatch)
    outcome = await mcp_transport._adiscover(raw=_raw("normal"))
    assert [spec.name for spec in outcome.specs] == [f"mcp:fake:{TOOL}"]
    spec = outcome.specs[0]
    obs = await spec.handler(spec.parameters(message="hi"))
    assert obs.ok is True and fake_mcp_server.OK_TEXT in obs.text
