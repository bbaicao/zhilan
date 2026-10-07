"""专项 · 工具调用能力第 2 批 · **③ MCP 真调用**（A15–A21 含 A18′）。

许可先于发现 / transport 严格层 / schema 超子集不上线 / 两条注册路径互斥 /
isError→ok=False / 停止超时不残留。真 stdio 假 server 腿（业主已授权起子进程）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from app.agent import bootstrap, mcp_transport
from app.agent.registry import ToolRegistry
from app.core.config import settings

FAKE = Path(__file__).with_name("fake_mcp_server.py")
assert FAKE.is_file()


def _pack(mode: str = "normal", *, slug: str = "fake", transport: str = "stdio", **extra):
    item = {
        "slug": slug,
        "name": "假 MCP 包",
        "transport": transport,
        "enabled": True,
        "available": True,
        "command": [sys.executable, str(FAKE), "--mode", mode],
        "url": "",
        "tools": [{"name": "fake_mcp_ping", "enabled": True, "dangerous": False}],
    }
    item.update(extra)
    return item


def _raw(packs):
    return json.dumps(packs, ensure_ascii=False)


@pytest.fixture
def counters(monkeypatch):
    """打桩 loader 模块属性（D-k 要求顶层绑定）。"""
    calls = {"stdio": 0, "http": 0, "order": []}

    async def fake_stdio(command, args, **kwargs):
        calls["stdio"] += 1
        calls["order"].append("loader")

        class _T:
            name = "fake_mcp_ping"
            description = "ping"
            parameters = _Param
            execute = _exec

        class _R:
            tools = [_T()]
            server = None
            tool_infos = []

        return _R()

    async def fake_http(url, **kwargs):
        calls["http"] += 1
        calls["order"].append("loader-http")
        raise RuntimeError("no http in unit")

    class _Param:
        @classmethod
        def model_json_schema(cls):
            return {
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            }

    async def _exec(*a, **k):
        class _C:
            text = "FAKE_MCP_OK"
            type = "text"

        class _Res:
            content = [_C()]
            is_error = None
            details = None

        return _Res()

    monkeypatch.setattr(mcp_transport, "load_mcp_tools_stdio", fake_stdio)
    monkeypatch.setattr(mcp_transport, "load_mcp_tools_http", fake_http)
    return calls


# --------------------------------------------------------------------------
# A15 · 未许可不启动（每条配正控）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a15_unlicensed_stdio_never_calls_loader(counters, monkeypatch):
    monkeypatch.setattr(settings, "MCP_SERVERS", _raw([_pack()]))
    monkeypatch.setattr(settings, "MCP_ALLOW_SERVERS", "")
    monkeypatch.setattr(settings, "MCP_ALLOW_EGRESS", "")
    specs = await mcp_transport.adiscover_tools()
    assert specs is None  # 零许可 ⇒ None（不是 []）
    assert counters["stdio"] == 0


@pytest.mark.asyncio
async def test_a15_licensed_stdio_calls_loader_once(counters, monkeypatch):
    monkeypatch.setattr(settings, "MCP_SERVERS", _raw([_pack()]))
    monkeypatch.setattr(settings, "MCP_ALLOW_SERVERS", "fake")
    monkeypatch.setattr(settings, "MCP_ALLOW_EGRESS", "")
    specs = await mcp_transport.adiscover_tools()
    assert counters["stdio"] >= 1
    assert specs and any(s.name.startswith("mcp:fake:") for s in specs)


@pytest.mark.asyncio
async def test_a15_http_needs_egress(counters, monkeypatch):
    monkeypatch.setattr(
        settings, "MCP_SERVERS", _raw([_pack(transport="http", url="http://127.0.0.1:1/mcp")])
    )
    monkeypatch.setattr(settings, "MCP_ALLOW_SERVERS", "fake")
    monkeypatch.setattr(settings, "MCP_ALLOW_EGRESS", "")
    specs = await mcp_transport.adiscover_tools()
    assert counters["http"] == 0
    # 正控：加上 egress 后会调 loader
    monkeypatch.setattr(settings, "MCP_ALLOW_EGRESS", "fake")
    await mcp_transport.adiscover_tools()
    assert counters["http"] >= 1


@pytest.mark.asyncio
async def test_a15_zero_allow_keeps_legacy_injection(monkeypatch):
    """A15④：零许可 ⇒ build_registry 走老分支（mcp_packs 注入仍生效）。"""
    from app.agent.tools.mcp import McpPack, McpTool

    monkeypatch.setattr(settings, "MCP_ALLOW_SERVERS", "")
    reg = bootstrap.build_registry(
        mcp_packs=[
            McpPack(
                slug="legacy",
                name="旧注入",
                transport="stdio",
                command=("noop",),
                tools=(McpTool(name="t1", description="x"),),
            )
        ]
    )
    names = set()
    for attr in ("_by_name", "_specs", "_tools"):
        obj = getattr(reg, attr, None)
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(k, str):
                    names.add(k)
                nm = getattr(v, "name", None)
                if isinstance(nm, str):
                    names.add(nm)
    assert any("mcp:legacy:" in n for n in names), names


# --------------------------------------------------------------------------
# A16 · transport 严格层
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a16_http_uses_streamable_http(counters, monkeypatch):
    seen = {}

    async def fake_http(url, **kwargs):
        seen.update(kwargs)
        seen["url"] = url

        class _R:
            tools = []
            server = None
            tool_infos = []

        return _R()

    monkeypatch.setattr(mcp_transport, "load_mcp_tools_http", fake_http)
    monkeypatch.setattr(settings, "MCP_SERVERS", _raw([_pack(transport="http", url="http://x/m")]))
    monkeypatch.setattr(settings, "MCP_ALLOW_SERVERS", "fake")
    monkeypatch.setattr(settings, "MCP_ALLOW_EGRESS", "fake")
    await mcp_transport.adiscover_tools()
    assert seen.get("transport") == "streamable_http"


@pytest.mark.asyncio
async def test_a16_sse_and_unknown_never_load(counters, monkeypatch):
    for transport in ("sse", "websocket"):
        monkeypatch.setattr(settings, "MCP_SERVERS", _raw([_pack(transport=transport, url="http://x")]))
        monkeypatch.setattr(settings, "MCP_ALLOW_SERVERS", "fake")
        monkeypatch.setattr(settings, "MCP_ALLOW_EGRESS", "fake")
        await mcp_transport.adiscover_tools()
    assert counters["stdio"] == 0
    assert counters["http"] == 0


# --------------------------------------------------------------------------
# A17 · 许可在发现之前（有序日志）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a17_permission_before_loader(counters, monkeypatch):
    order = []
    real_parse = mcp_transport.parse_allowlist

    def spy_parse(raw):
        order.append("permit")
        return real_parse(raw)

    monkeypatch.setattr(mcp_transport, "parse_allowlist", spy_parse)
    monkeypatch.setattr(settings, "MCP_SERVERS", _raw([_pack()]))
    monkeypatch.setattr(settings, "MCP_ALLOW_SERVERS", "fake")
    monkeypatch.setattr(settings, "MCP_ALLOW_EGRESS", "")
    await mcp_transport.adiscover_tools()
    assert order and counters["order"]
    assert order[0] == "permit"
    assert counters["order"][0] == "loader"
    assert order.index("permit") < 1  # 许可在 loader 调用之前已发生


# --------------------------------------------------------------------------
# A18 / A18′ · 真 stdio + 互斥
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a18_real_stdio_success_and_call():
    monkeypatch_set = {
        "MCP_SERVERS": _raw([_pack("normal")]),
        "MCP_ALLOW_SERVERS": "fake",
        "MCP_ALLOW_EGRESS": "",
    }
    for k, v in monkeypatch_set.items():
        setattr(settings, k, v)
    try:
        outcome = await mcp_transport._adiscover()
        assert outcome.specs, outcome.skipped
        spec = outcome.specs[0]
        schema = spec.parameters.model_json_schema()
        assert "message" in (schema.get("properties") or {})
        obs = await spec.handler(spec.parameters(message="hi"))
        assert obs.ok is True
        assert "FAKE_MCP_OK" in obs.text
    finally:
        settings.MCP_SERVERS = ""
        settings.MCP_ALLOW_SERVERS = ""
        settings.MCP_ALLOW_EGRESS = ""


@pytest.mark.asyncio
async def test_a18_prime_mutual_exclusion_and_warning(monkeypatch):
    from app.agent.tools.mcp import McpPack, McpTool

    fake_spec = ToolRegistry().register  # noqa: F841 —— 仅占位
    specs = []
    reg_legacy = bootstrap.build_registry(
        mcp_packs=[
            McpPack(slug="legacy", name="旧", transport="stdio", command=("x",), tools=(McpTool(name="t"),))
        ]
    )
    # 同时给 mcp_specs 与 mcp_packs：以 mcp_specs 为准，同一契约名恰一条
    injected = bootstrap.build_registry(
        mcp_packs=[
            McpPack(slug="legacy", name="旧", transport="stdio", command=("x",), tools=(McpTool(name="t"),))
        ],
        mcp_specs=[],
    )
    # mcp_specs=[] ⇒ 只注册空列表（不走 load_packs）⇒ 不应有 mcp:legacy
    names = set()
    for attr in ("_by_name", "_specs", "_tools"):
        obj = getattr(injected, attr, None)
        if isinstance(obj, dict):
            names |= set(obj)
            names |= {getattr(v, "name", "") for v in obj.values()}
    assert not any(n.startswith("mcp:legacy:") for n in names if n)


@pytest.mark.asyncio
async def test_a18_prime_registry_has_exactly_one_contract_name():
    monkeypatch_vals = {
        "MCP_SERVERS": _raw([_pack("normal")]),
        "MCP_ALLOW_SERVERS": "fake",
        "MCP_ALLOW_EGRESS": "",
    }
    for k, v in monkeypatch_vals.items():
        setattr(settings, k, v)
    try:
        specs = await mcp_transport.adiscover_tools()
        assert specs
        names = [s.name for s in specs]
        assert len(names) == len(set(names))
        # 非默认钩子：handler 不是 call_tool_hook 文案
        obs = await specs[0].handler(specs[0].parameters(message="x"))
        assert "尚未绑定传输通道" not in obs.text
    finally:
        settings.MCP_SERVERS = ""
        settings.MCP_ALLOW_SERVERS = ""
        settings.MCP_ALLOW_EGRESS = ""


def test_a18_prime_registry_keeps_specs_path_when_both_given(caplog):
    """A18′（2026-09-22 复核补判据）：两条注册路径互斥 —— `mcp_specs` 与 `mcp_packs`
    同时给 ⇒ **以 specs 为准** + warning、不抛错；注册表里 `mcp:*` 契约名唯一。

    ⚠️ 为什么补这条：原 `..._has_exactly_one_contract_name` 名字说的是注册表，实际只调
    `adiscover_tools()`、**从不调用 `build_registry`** ⇒ 针对 D-i′ 的变异在结构上不可能被它发现
    （第 2 批首版变异 ⑥ 因此是惰性变异，见 `证据/t2_mutations.py` 的 m6）。
    """
    from app.agent.tools import mcp as mcp_tools

    spec_side = mcp_tools.build_specs([_pack("normal", slug="fromspecs")], call_tool=None)
    assert spec_side, "specs 侧夹具没造出来"
    with caplog.at_level("WARNING", logger="app.agent.bootstrap"):
        registry = bootstrap.build_registry(
            mcp_specs=spec_side,
            mcp_packs=[_pack("normal", slug="frompacks")],
        )
    names = [s.name for s in registry.list(include_dangerous=True)]
    mcp_names = [n for n in names if n.startswith("mcp:")]
    assert mcp_names == [spec_side[0].name], mcp_names
    assert len(names) == len(set(names)), names
    assert "以 mcp_specs 为准" in caplog.text


# --------------------------------------------------------------------------
# A19 · 上线过滤
# --------------------------------------------------------------------------


def test_a19_schema_filter_reasons():
    from pydantic import BaseModel, Field

    class Nested(BaseModel):
        options: dict = Field(default_factory=dict)

    class Flat(BaseModel):
        message: str = ""

    class Empty(BaseModel):
        pass

    assert mcp_transport._schema_reason(Nested) in {"nested_object", "untyped", "composite"}
    assert mcp_transport._schema_reason(Flat) is None
    assert mcp_transport._schema_reason(Empty) == "no_fields"


@pytest.mark.asyncio
async def test_a19_nested_mode_skipped():
    for k, v in {
        "MCP_SERVERS": _raw([_pack("nested")]),
        "MCP_ALLOW_SERVERS": "fake",
        "MCP_ALLOW_EGRESS": "",
    }.items():
        setattr(settings, k, v)
    try:
        outcome = await mcp_transport._adiscover()
        assert not outcome.specs
        assert outcome.skipped and outcome.skipped[0]["reason"] in {
            "nested_object",
            "untyped",
            "composite",
            "no_fields",
        }
    finally:
        settings.MCP_SERVERS = ""
        settings.MCP_ALLOW_SERVERS = ""
        settings.MCP_ALLOW_EGRESS = ""


# --------------------------------------------------------------------------
# A20 · isError ⇒ ok=False
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a20_is_error_maps_to_ok_false():
    for k, v in {
        "MCP_SERVERS": _raw([_pack("is_error")]),
        "MCP_ALLOW_SERVERS": "fake",
        "MCP_ALLOW_EGRESS": "",
    }.items():
        setattr(settings, k, v)
    try:
        outcome = await mcp_transport._adiscover()
        assert outcome.specs
        obs = await outcome.specs[0].handler(outcome.specs[0].parameters(message="x"))
        assert obs.ok is False
    finally:
        settings.MCP_SERVERS = ""
        settings.MCP_ALLOW_SERVERS = ""
        settings.MCP_ALLOW_EGRESS = ""


# --------------------------------------------------------------------------
# A21 · 超时 / hang（真 stdio）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a21_hang_mode_times_out(monkeypatch):
    monkeypatch.setattr(mcp_transport, "MCP_CALL_TIMEOUT", 2.0)
    for k, v in {
        "MCP_SERVERS": _raw([_pack("hang")]),
        "MCP_ALLOW_SERVERS": "fake",
        "MCP_ALLOW_EGRESS": "",
    }.items():
        setattr(settings, k, v)
    try:
        outcome = await mcp_transport._adiscover()
        if not outcome.specs:
            pytest.skip("hang 模式发现阶段即挂起/超时 —— 已记为超时收尾")
        obs = await outcome.specs[0].handler(outcome.specs[0].parameters(message="x"))
        assert obs.ok is False
    finally:
        settings.MCP_SERVERS = ""
        settings.MCP_ALLOW_SERVERS = ""
        settings.MCP_ALLOW_EGRESS = ""


def test_parse_allowlist_rejects_star():
    assert mcp_transport.parse_allowlist("*") == frozenset()
    assert mcp_transport.parse_allowlist("a, *") == frozenset()
    assert mcp_transport.parse_allowlist("My Server") == frozenset({"my-server"})


# --------------------------------------------------------------------------
# 回归修复 · transport 出口（_make_live_handler）的成功文本也要裁
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_handler_clips_oversized_return_text(monkeypatch):
    """回归修复：MCP_CALL_TIMEOUT 只限速不限大小 ⇒ stdio/http 真发现路径的成功
    文本出口（``_make_live_handler`` 的 ``"\\n".join(texts)``）同样裁到观察尺度。

    选缝理由：仿 ``counters`` 夹具的桩 loader 形状（monkeypatch 模块属性、不碰真 loader、
    不出网、不起子进程）——这是让 5000 字符返回文本流经 ``_make_live_handler`` 出口的
    **最小改动**缝：发现阶段放行一条假工具，其 ``execute`` 返回大文本 content 块。
    断言尺度与 ``tests/agent/test_mcp_tools.py`` 的同名腿一致（1200 + 省略号）。
    ⚠️ ``parameters`` 须是 pydantic 模型（真发现路径的 ``_Param`` 是占位类、不能带参实例化，
    而 ``spec.parameters(message=...)`` + handler 内 ``model_dump()`` 都要求真模型形状）。
    """
    from pydantic import BaseModel

    big_text = "y" * 5000 + "TRANSPORT-SENTINEL-TAIL"

    class _Param(BaseModel):
        message: str = ""

    async def _exec(*a, **k):
        class _C:
            text = big_text
            type = "text"

        class _Res:
            content = [_C()]
            is_error = None
            details = None

        return _Res()

    class _T:
        name = "fake_mcp_ping"
        description = "ping"
        parameters = _Param
        execute = _exec

    async def fake_stdio(command, args, **kwargs):
        class _R:
            tools = [_T()]
            server = None
            tool_infos = []

        return _R()

    async def fake_http(url, **kwargs):
        raise RuntimeError("no http in unit")

    monkeypatch.setattr(mcp_transport, "load_mcp_tools_stdio", fake_stdio)
    monkeypatch.setattr(mcp_transport, "load_mcp_tools_http", fake_http)
    monkeypatch.setattr(settings, "MCP_SERVERS", _raw([_pack()]))
    monkeypatch.setattr(settings, "MCP_ALLOW_SERVERS", "fake")
    monkeypatch.setattr(settings, "MCP_ALLOW_EGRESS", "")

    specs = await mcp_transport.adiscover_tools()
    assert specs and specs[0].name.startswith("mcp:fake:")

    obs = await specs[0].handler(specs[0].parameters(message="x"))

    assert obs.ok is True
    assert len(obs.text) <= 1201  # OBSERVATION_CLIP(1200) + 省略号 1 字
    assert obs.text.endswith("…")
    assert "TRANSPORT-SENTINEL-TAIL" not in obs.text  # 尾哨兵被裁掉 ⇒ 截断真发生
    assert obs.materials == []
