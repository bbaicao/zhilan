"""专项收口 · ``回归修复``：**MCP 配置解析单一来源**（发现层 ↔ 注入面）。

MCP 配置必须在发现层与注入面保持以下语义：

- 同一份配置在**发现层**（``app/agent/mcp_transport.py``）与**注入面**（``app/agent/tools/mcp.py``）
  得到同一组语义（slug / enabled / available / command / dangerous）；
- ``"enabled": "false"`` / ``"available": "false"`` **关得掉**（改前的病根是 ``bool("false") is True``）；
- 字符串 ``command`` **按空白切分**，且**真发现能起来**（真 stdio 假 server 腿）；
- **sync（``discover_tools``）与 async（``_adiscover``）两腿都判** —— D-C5：漏一处 = 本笔不算通过。

⚠️ 本文件属 ``tests/api/`` ⇒ ``conftest.py`` 的 autouse 钉子生效（``pin_mcp_off`` 把
``MCP_SERVERS`` / ``MCP_ALLOW_SERVERS`` / ``MCP_ALLOW_EGRESS`` 三键钉空）⇒ 每个用例自己
``monkeypatch.setattr(settings, ...)`` 覆盖，否则「静默测了个寂寞」。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from app.agent import mcp_transport
from app.agent.tools import mcp as mcp_tools
from app.core.config import settings

#: 真 stdio 假 server（``tests/api/fake_mcp_server.py``；文件名不匹配 ``test_*.py`` ⇒ 不被收集）
FAKE = Path(__file__).with_name("fake_mcp_server.py")
assert FAKE.is_file()

#: 假 server ``--mode normal`` 暴露的工具名（与 ``test_mcp_runtime_t2.py`` 同源）
FAKE_TOOL = "fake_mcp_ping"


class _FlatArgs(BaseModel):
    """一条「能上线」的最小参数模型（``_schema_reason`` 只放行平铺标量字段）。"""

    message: str = ""


def _raw(pack: dict) -> str:
    return json.dumps([pack], ensure_ascii=False)


def _pack(**over) -> dict:
    """一份最小可用包配置（``command`` 默认给列表形态，各用例按需覆盖成字符串）。"""
    base = {
        "slug": "demo",
        "name": "演示服务",
        "transport": "stdio",
        "command": [sys.executable, "-c", "pass"],
        "url": "",
        "tools": [],
    }
    base.update(over)
    return base


def _allow(monkeypatch, slug: str = "demo") -> None:
    """把三键从 autouse 钉子手里拿回来（零许可 = default-deny = 发现层直接返回）。"""
    monkeypatch.setattr(settings, "MCP_ALLOW_SERVERS", slug)
    monkeypatch.setattr(settings, "MCP_ALLOW_EGRESS", "")


def _fake_loader(tool_specs: list[tuple[str, str]], calls: list):
    """造一个假 loader（签名与 ``load_mcp_tools_stdio`` 同形）：**不起子进程、不出网**。"""

    class _Tool:
        def __init__(self, name: str, description: str) -> None:
            self.name = name
            self.description = description
            self.parameters = _FlatArgs

    class _Result:
        def __init__(self) -> None:
            self.tools = [_Tool(n, d) for n, d in tool_specs]

    async def loader(command, args, **kwargs):  # noqa: ANN001
        calls.append((str(command), tuple(str(a) for a in args)))
        return _Result()

    return loader


def _sync_leg_names(raw: str) -> list[str]:
    """跑**同步腿**（``discover_tools``）并返回契约名清单。

    ``discover_tools`` 内部是 ``asyncio.get_event_loop().run_until_complete(...)`` ⇒ 在 async
    用例里直接调会撞「This event loop is already running」。这里把同步腿放进**独立线程**、
    给它自己的新事件环（真实同步调用方的处境）—— 于是同一个助手在同步 / 异步用例里都能用，
    两腿对拍才有意义；且**不碰**用例所在线程的事件环（否则会与 pytest-asyncio 的环打架）。
    """
    box: dict[str, list[str]] = {}

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            box["names"] = [spec.name for spec in mcp_transport.discover_tools(raw=raw).specs]
        finally:
            loop.close()

    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    return box["names"]


async def _async_leg_names(raw: str) -> list[str]:
    return [spec.name for spec in (await mcp_transport._adiscover(raw=raw)).specs]


# --------------------------------------------------------------------------
# A-C1① · 字符串布尔：包级 enabled / available（改前 bool("false") is True ⇒ 关不掉）
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["enabled", "available"])
def test_a_c1_stringy_false_closes_the_pack_in_both_legs(key, monkeypatch):
    """``"false"`` / ``"0"`` / ``0`` 都关得掉；``True`` 仍开着（**两腿**各自成对）。"""
    calls: list = []
    monkeypatch.setattr(mcp_transport, "load_mcp_tools_stdio", _fake_loader([("read_file", "读文件")], calls))
    _allow(monkeypatch)

    for off in ("false", "0", 0):
        raw = _raw(_pack(**{key: off}))
        assert _sync_leg_names(raw) == [], f"同步腿：{key}={off!r} 没关掉"
        assert asyncio.run(_async_leg_names(raw)) == [], f"async 腿：{key}={off!r} 没关掉"

    raw_on = _raw(_pack(**{key: True}))
    assert _sync_leg_names(raw_on) == ["mcp:demo:read_file"]
    assert asyncio.run(_async_leg_names(raw_on)) == ["mcp:demo:read_file"]


def test_a_c1_stringy_false_closes_the_tool_in_both_legs(monkeypatch):
    """工具级 ``enabled`` 同样是可读意图（``"false"`` 关得掉、``"true"`` 开着）。"""
    calls: list = []
    monkeypatch.setattr(mcp_transport, "load_mcp_tools_stdio", _fake_loader([("read_file", "读文件")], calls))
    _allow(monkeypatch)

    raw_off = _raw(_pack(tools=[{"name": "read_file", "enabled": "false"}]))
    assert _sync_leg_names(raw_off) == []
    assert asyncio.run(_async_leg_names(raw_off)) == []

    raw_on = _raw(_pack(tools=[{"name": "read_file", "enabled": "true"}]))
    assert _sync_leg_names(raw_on) == ["mcp:demo:read_file"]
    assert asyncio.run(_async_leg_names(raw_on)) == ["mcp:demo:read_file"]


# --------------------------------------------------------------------------
# A-C1② · 字符串 command：按空白切分，且**真发现能起来**
# --------------------------------------------------------------------------


def test_a_c1_string_command_is_split_sync_leg(monkeypatch):
    """同步腿：``"python -c pass"`` 切成三段后真的起来了（假 loader 收到三段）。"""
    calls: list = []
    monkeypatch.setattr(mcp_transport, "load_mcp_tools_stdio", _fake_loader([("read_file", "读文件")], calls))
    _allow(monkeypatch)

    raw = _raw(_pack(command=f"{sys.executable} -c pass", tools=[{"name": "read_file"}]))
    assert _sync_leg_names(raw) == ["mcp:demo:read_file"]
    assert calls, "loader 根本没被调用"
    assert calls[0][0] == sys.executable, calls[0]
    assert calls[0][1] == ("-c", "pass"), calls[0]


@pytest.mark.asyncio
async def test_a_c1_string_command_is_split_and_real_stdio_works(monkeypatch):
    """async 腿 + **真 stdio 假 server**：字符串 command 切分后真发现能起来（D-C1②）。

    ``_as_command`` 按**空白**切分（老基线口径）⇒ 路径含空格时本机型不可判（如实 skip）。
    """
    if " " in sys.executable or " " in str(FAKE):
        pytest.skip("解释器 / 夹具路径含空格：按空白切分会切坏，本机型不可判")
    _allow(monkeypatch)

    raw = _raw(
        _pack(
            command=f"{sys.executable} {FAKE} --mode normal",
            tools=[{"name": FAKE_TOOL, "dangerous": False}],
        )
    )
    assert await _async_leg_names(raw) == [f"mcp:demo:{FAKE_TOOL}"]


# --------------------------------------------------------------------------
# A-C1③ · dangerous：**配置显式写了就听配置**（不得被名字/描述规则叠加覆盖）
# --------------------------------------------------------------------------


def test_a_c1_explicit_dangerous_false_wins_sync_leg(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        mcp_transport,
        "load_mcp_tools_stdio",
        _fake_loader([("delete_file", "删除远端全部文件")], calls),
    )
    _allow(monkeypatch)

    explicit_false = _raw(_pack(tools=[{"name": "delete_file", "dangerous": False}]))
    assert _sync_leg_names(explicit_false) == ["mcp:demo:delete_file"]

    unset = _raw(_pack(tools=[{"name": "delete_file"}]))
    assert _sync_leg_names(unset) == [], "没写 dangerous 时名字/描述规则必须仍然生效"

    explicit_true = _raw(_pack(tools=[{"name": "delete_file", "dangerous": True}]))
    assert _sync_leg_names(explicit_true) == []


@pytest.mark.asyncio
async def test_a_c1_explicit_dangerous_false_wins_async_leg(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        mcp_transport,
        "load_mcp_tools_stdio",
        _fake_loader([("delete_file", "删除远端全部文件")], calls),
    )
    _allow(monkeypatch)

    explicit_false = _raw(_pack(tools=[{"name": "delete_file", "dangerous": False}]))
    assert await _async_leg_names(explicit_false) == ["mcp:demo:delete_file"]

    unset = _raw(_pack(tools=[{"name": "delete_file"}]))
    outcome = await mcp_transport._adiscover(raw=unset)
    assert outcome.specs == []
    assert outcome.skipped == [{"tool": "delete_file", "reason": "dangerous"}], outcome.skipped

    explicit_true = _raw(_pack(tools=[{"name": "delete_file", "dangerous": True}]))
    assert await _async_leg_names(explicit_true) == []

    # 字符串布尔同样按可读意图解析（"false" 关掉高危标记 ⇒ 上线；"true" ⇒ 不上线）
    stringy_false = _raw(_pack(tools=[{"name": "delete_file", "dangerous": "false"}]))
    assert await _async_leg_names(stringy_false) == ["mcp:demo:delete_file"]
    stringy_true = _raw(_pack(tools=[{"name": "delete_file", "dangerous": "true"}]))
    assert await _async_leg_names(stringy_true) == []


# --------------------------------------------------------------------------
# A-C1④ · 两腿对拍 + 与**注入面**（tools/mcp.load_packs）对拍
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_c1_sync_and_async_legs_agree(monkeypatch):
    """同一份配置：sync 腿与 async 腿给出**同一组**契约名（含被拒集合）。"""
    calls: list = []
    monkeypatch.setattr(
        mcp_transport,
        "load_mcp_tools_stdio",
        _fake_loader([("read_file", "读文件"), ("delete_file", "删除远端全部文件")], calls),
    )
    _allow(monkeypatch)
    raw = _raw(
        _pack(
            tools=[
                {"name": "read_file", "enabled": "true"},
                {"name": "delete_file", "dangerous": False},
            ]
        )
    )
    sync_names = _sync_leg_names(raw)
    async_names = await _async_leg_names(raw)
    assert sync_names == async_names == ["mcp:demo:read_file", "mcp:demo:delete_file"]


@pytest.mark.asyncio
async def test_a_c1_discovery_agrees_with_injection_side(monkeypatch):
    """发现层与注入面对同一份配置的理解一致（**同一份实现**，不是两份近似副本）。

    注入面 = ``tools/mcp.load_packs``（``build_registry(mcp_packs=...)`` 那条路的入口）。
    """
    calls: list = []
    monkeypatch.setattr(
        mcp_transport,
        "load_mcp_tools_stdio",
        _fake_loader([("read_file", "读文件"), ("delete_file", "删除远端全部文件")], calls),
    )
    _allow(monkeypatch)
    raw = _raw(
        _pack(
            enabled="true",
            available="true",
            command=f"{sys.executable} -c pass",
            tools=[
                {"name": "read_file", "enabled": "false"},
                {"name": "delete_file", "dangerous": False, "description": "删除远端全部文件"},
            ],
        )
    )

    packs = mcp_tools.load_packs(raw=raw)
    assert len(packs) == 1
    pack = packs[0]
    # 注入面读到的语义
    assert pack.enabled is True and pack.available is True
    assert pack.command == (sys.executable, "-c", "pass"), pack.command
    injection_dangerous = {tool.name: tool.dangerous for tool in pack.tools}
    assert injection_dangerous == {"read_file": False, "delete_file": False}
    assert {tool.name: tool.enabled for tool in pack.tools} == {"read_file": False, "delete_file": True}

    # 发现面：同一份配置 ⇒ 与注入面逐条对齐（read_file 因 enabled="false" 不上线）
    outcome = await mcp_transport._adiscover(raw=raw)
    assert [spec.name for spec in outcome.specs] == ["mcp:demo:delete_file"], outcome.skipped
    assert injection_dangerous["delete_file"] is False, "注入面认定它不高危，发现面必须同判"


def test_a_c1_zero_allow_still_returns_nothing(monkeypatch):
    """零许可的正控对照：许可键为空 ⇒ **两腿都不发现**（别把「关掉」误判成本笔的功劳）。"""
    calls: list = []
    monkeypatch.setattr(mcp_transport, "load_mcp_tools_stdio", _fake_loader([("read_file", "读文件")], calls))
    monkeypatch.setattr(settings, "MCP_ALLOW_SERVERS", "")
    monkeypatch.setattr(settings, "MCP_ALLOW_EGRESS", "")
    raw = _raw(_pack(tools=[{"name": "read_file"}]))
    assert _sync_leg_names(raw) == []
    assert asyncio.run(_async_leg_names(raw)) == []
    assert calls == []


# --------------------------------------------------------------------------
# A-C2 · schema 子集：**数组内嵌套 object 必须拒**（``回归修复``）
# --------------------------------------------------------------------------


def test_a_c2_array_nested_object_is_rejected():
    """改前整类放行上线：``list[dict[str, Any]]`` / 裸 ``list``。

    加严的边界必须同样钉住：**平铺标量数组**（``list[str]``）是允许的，别把「加严」做成误伤。
    """

    class NestedList(BaseModel):
        entries: list[dict[str, Any]] = Field(default_factory=list)

    class BareList(BaseModel):
        entries: list = Field(default_factory=list)

    class ScalarList(BaseModel):
        tags: list[str] = Field(default_factory=list)

    assert mcp_transport._schema_reason(NestedList) == "nested_object"
    assert mcp_transport._schema_reason(BareList) == "untyped"
    assert mcp_transport._schema_reason(ScalarList) is None, "平铺标量数组没有理由被拒"


def test_a_c2_array_nested_object_tool_does_not_go_live(monkeypatch):
    """端到端腿：远端给一条「数组内嵌套 object」的工具 ⇒ 不上线 + 结构化原因。"""

    class NestedList(BaseModel):
        entries: list[dict[str, Any]] = Field(default_factory=list)

    class _Tool:
        name = "list_entries"
        description = "列出条目"
        parameters = NestedList

    class _Result:
        tools = [_Tool()]

    async def loader(command, args, **kwargs):  # noqa: ANN001
        return _Result()

    monkeypatch.setattr(mcp_transport, "load_mcp_tools_stdio", loader)
    _allow(monkeypatch)
    outcome = asyncio.run(
        mcp_transport._adiscover(raw=_raw(_pack(tools=[{"name": "list_entries"}])))
    )
    assert outcome.specs == []
    assert outcome.skipped == [{"tool": "list_entries", "reason": "nested_object"}], outcome.skipped


def test_a_c2_existing_reasons_are_unchanged():
    """既有四类原因串与放行例**逐字不变**（只加严、不放宽）。"""

    class Empty(BaseModel):
        pass

    class ObjField(BaseModel):
        options: dict = Field(default_factory=dict)

    class Composite(BaseModel):
        choice: int | str = 1

    class Untyped(BaseModel):
        blob: Any = None

    class Flat(BaseModel):
        message: str = ""

    assert mcp_transport._schema_reason(Empty) == "no_fields"
    assert mcp_transport._schema_reason(ObjField) == "nested_object"
    assert mcp_transport._schema_reason(Composite) == "composite"
    assert mcp_transport._schema_reason(Untyped) == "untyped"
    assert mcp_transport._schema_reason(Flat) is None
