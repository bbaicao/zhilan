"""MCP 工具单测（阶段 4 · 条目 4.1.2 的第四类工具：``mcp:{slug}:{tool}``）。

测的是四件事——契约名 / 过滤（含 I6 危险工具零暴露）/ 坏配置不炸 / 调用钩子。
**隔离口径**：不出网、不起进程、不连数据库——4.1 根本不绑真 transport
（`_work-4.1接口冻结.md` §十一.4：真绑定随技能域 4.5.3 落地），所以这里所有 ``call_tool``
都是注入的假实现，包快照直接构造或走内存里的 JSON 字符串，不读 ``.env``、不碰真 server。
"""

from __future__ import annotations

import inspect
import json
import logging

import pytest

from app.agent.registry import ToolRegistry
from app.agent.tools import mcp
from app.agent.tools.mcp import (
    TOOL_PREFIX,
    McpPack,
    McpTool,
    McpToolArgs,
    build_specs,
    call_tool_hook,
    dangerous_by_default,
    load_packs,
    pack_tool_name,
)
from app.agent.types import ToolObservation


def _pack(**overrides) -> McpPack:
    """一份最小可用的包快照（stdio + 一条只读工具）。"""
    base = {
        "slug": "demo",
        "name": "演示服务",
        "transport": "stdio",
        "command": ("npx", "-y", "demo-server"),
        "url": "",
        "tools": (McpTool(name="read_file", description="读文件"),),
    }
    base.update(overrides)
    return McpPack(**base)


class _FakeTransport:
    """假传输通道：记下收到的 5 个参数，返回固定文本（或按需抛错）。**不出网。**"""

    def __init__(self, text: str = "工具返回：OK", exc: Exception | None = None) -> None:
        self.text = text
        self.exc = exc
        self.calls: list[tuple] = []

    async def __call__(self, transport, command, url, tool_name, args) -> str:
        self.calls.append((transport, command, url, tool_name, args))
        if self.exc is not None:
            raise self.exc
        return self.text


# --- 契约名 ------------------------------------------------------------------


def test_pack_tool_name_is_contract_frozen():
    assert TOOL_PREFIX == "mcp:"
    assert pack_tool_name("demo", "read_file") == "mcp:demo:read_file"
    name = pack_tool_name("my-server", "list_files")
    assert name.count(":") == 2  # 恰好两段分隔：mcp / slug / tool
    assert name.startswith("mcp:")
    assert not name.startswith("mcp_")  # 老写法 mcp_tool_* 是违约形状


def test_build_specs_names_use_the_contract_form():
    assert [s.name for s in build_specs([_pack()])] == ["mcp:demo:read_file"]


# --- dangerous 判定（I6 的输入端）--------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    [
        "delete_file",
        "remove_all",
        "drop_table",
        "write_file",
        "update_row",
        "create_note",
        "insert_one",
        "exec_sql",
        "open_shell",
        "run_command",
        "run",
        "rm_rf",
        "destroy_all",
        "modify_config",
        "send_mail",
        "post_form",
        "put_object",
        "patch_doc",
        "upload_file",
        "DELETE_ROW",
    ],
)
def test_dangerous_by_default_hits_high_risk_verbs(tool_name):
    assert dangerous_by_default(tool_name) is True


@pytest.mark.parametrize(
    "tool_name",
    ["read_file", "list_files", "search_docs", "get_weather", "describe_schema", "fetch_page"],
)
def test_dangerous_by_default_leaves_read_only_tools_alone(tool_name):
    assert dangerous_by_default(tool_name) is False


def test_dangerous_by_default_reads_description_too():
    # 工具名常常是包的私事（call / invoke），真语义在描述里 —— 描述命中同样判危险
    assert dangerous_by_default("call", "will upload the report to the vendor") is True
    assert dangerous_by_default("call", "returns the current weather") is False
    # 如实登记边界：正则只认英文动词，中文描述不命中（老基线正则逐字沿用，不擅自扩表）
    assert dangerous_by_default("call", "删除远端全部文件") is False


# --- build_specs：过滤规则 ----------------------------------------------------


@pytest.mark.parametrize(
    "pack",
    [
        _pack(enabled=False),
        _pack(available=False),
        _pack(slug=""),
        _pack(slug="   "),
    ],
)
def test_build_specs_skips_pack_level(pack):
    assert build_specs([pack]) == []


def test_build_specs_skips_tool_level():
    pack = _pack(
        tools=(
            McpTool(name="read_file"),
            McpTool(name=""),  # 空名
            McpTool(name="   "),  # 只有空白
            McpTool(name="list", enabled=False),
            McpTool(name="delete_file", dangerous=True),
        )
    )
    assert [s.name for s in build_specs([pack])] == ["mcp:demo:read_file"]


def test_build_specs_empty_inputs_are_not_a_crash():
    assert build_specs([]) == []
    assert build_specs([_pack(tools=())]) == []


def test_build_specs_also_accepts_raw_json_entries():
    """装配侧（build_registry(mcp_packs=...)）可能直接塞解析结果，别让 AttributeError 炸穿每轮注册。"""
    specs = build_specs(
        [
            {
                "slug": "Demo Server",
                "name": "演示服务",
                "tools": [{"name": "read_file", "description": "读文件"}, {"name": "drop_table"}],
            },
            42,  # 认不出的条目跳过，不抛
        ]
    )
    assert [s.name for s in specs] == ["mcp:demo-server:read_file"]
    assert specs[0].description == "演示服务 · read_file：读文件"


def test_build_specs_never_emits_dangerous_specs():
    """I6：危险工具连 ToolSpec 都不生成（不是靠上层 for_model 兜）。"""
    pack = _pack(
        tools=(
            McpTool(name="read_file"),
            McpTool(name="delete_file", dangerous=True),
            McpTool(name="upload", dangerous=True),
        )
    )
    specs = build_specs([pack])
    assert [s.name for s in specs] == ["mcp:demo:read_file"]
    assert all(s.dangerous is False for s in specs)


# --- build_specs：ToolSpec 形状 ----------------------------------------------


def test_spec_shape_description_and_parameters():
    spec = build_specs([_pack()])[0]
    assert spec.description == "演示服务 · read_file：读文件"  # 老基线措辞逐字
    assert spec.dangerous is False
    assert spec.needs_web is False  # MCP 的联网与否不归 WEB_ENABLE 管
    assert spec.parameters is McpToolArgs
    assert spec.parameters_schema == McpToolArgs.model_json_schema()
    assert spec.parameters_schema["additionalProperties"] is True  # 任意 JSON 对象都收
    assert inspect.iscoroutinefunction(spec.handler)


def test_spec_description_falls_back_to_slug_and_tolerates_empty_text():
    assert build_specs([_pack(name="")])[0].description.startswith("demo · read_file：")
    assert build_specs([_pack(tools=(McpTool(name="ping"),))])[0].description == "演示服务 · ping："


def test_specs_register_and_stay_dangerous_free():
    reg = ToolRegistry()
    for spec in build_specs(
        [
            _pack(),
            _pack(slug="wiki", tools=(McpTool(name="search"), McpTool(name="delete_page", dangerous=True))),
        ]
    ):
        reg.register(spec)
    assert [s.name for s in reg.list()] == ["mcp:demo:read_file", "mcp:wiki:search"]


# --- handler：成功 / 失败 / 未绑定 -------------------------------------------


@pytest.mark.asyncio
async def test_handler_forwards_args_and_returns_text():
    transport = _FakeTransport("工具返回：OK")
    spec = build_specs([_pack()], call_tool=transport)[0]

    obs = await spec.handler(McpToolArgs(query="天气", extra={"a": [1, 2]}))

    assert isinstance(obs, ToolObservation)
    assert obs.text == "工具返回：OK"
    assert obs.ok is True
    assert obs.warnings == []
    assert obs.materials == []  # 冻结 §六：MCP 永不产出材料，故不可作为引用来源
    got_transport, command, url, tool_name, args = transport.calls[0]
    assert (got_transport, command, url) == ("stdio", ("npx", "-y", "demo-server"), "")
    assert tool_name == "read_file"  # 真 server 只认包内原始名，契约名只在 ToolSpec.name 上
    assert args == {"query": "天气", "extra": {"a": [1, 2]}}


@pytest.mark.asyncio
async def test_handler_clips_oversized_return_text():
    """回归修复：MCP_CALL_TIMEOUT 只限速不限大小，超长返回文本必须在出口裁掉。

    裁剪尺度 = ``mcp.OBSERVATION_CLIP``（1200，照 ``fetch_page`` 观察尺度）；负控 =
    上面的 ``test_handler_forwards_args_and_returns_text``（短文本逐字不裁）。
    """
    transport = _FakeTransport("x" * 5000 + "SENTINEL-TAIL")
    spec = build_specs([_pack()], call_tool=transport)[0]

    obs = await spec.handler(McpToolArgs())

    assert obs.ok is True
    assert len(obs.text) <= 1201  # 1200（OBSERVATION_CLIP）+ 省略号 1 字
    assert obs.text.endswith("…")
    assert "SENTINEL-TAIL" not in obs.text  # 尾哨兵被裁掉 ⇒ 截断真发生


@pytest.mark.asyncio
async def test_handler_passes_http_transport_fields():
    transport = _FakeTransport()
    pack = _pack(transport="http", command=(), url="http://127.0.0.1:9/mcp")
    spec = build_specs([pack], call_tool=transport)[0]

    await spec.handler(McpToolArgs())

    assert transport.calls[0][:4] == ("http", (), "http://127.0.0.1:9/mcp", "read_file")


@pytest.mark.asyncio
async def test_handler_accepts_sync_call_tool():
    """注入点按位置传 5 参；同步实现也接受（默认 call_tool_hook 就是同步的）。"""

    def sync_call(transport, command, url, tool_name, args) -> str:
        return f"sync:{tool_name}"

    spec = build_specs([_pack()], call_tool=sync_call)[0]
    obs = await spec.handler(McpToolArgs())
    assert obs.text == "sync:read_file"
    assert obs.ok is True


@pytest.mark.asyncio
async def test_handler_turns_exception_into_failed_observation():
    transport = _FakeTransport(exc=RuntimeError("server 掉线"))
    spec = build_specs([_pack()], call_tool=transport)[0]

    obs = await spec.handler(McpToolArgs())  # 不抛（I5：工具失败不终止循环）

    assert obs.ok is False
    # 回归修复（D-C7 允许的既有断言改族）：**升级为双断言** —— ① 新的固定形状在场
    # ② 异常原文（str(exc)）不在观察文本 / warnings 里。原断言要的两件事都还在，没有放宽。
    assert obs.text == "MCP 工具 mcp:demo:read_file 执行失败（RuntimeError）。"
    assert "server 掉线" not in obs.text
    assert obs.warnings == ["MCP 工具 mcp:demo:read_file 执行失败（RuntimeError）。"]
    assert all("server 掉线" not in item for item in obs.warnings)
    assert obs.materials == []


@pytest.mark.asyncio
async def test_handler_never_raises_on_odd_args():
    transport = _FakeTransport()
    spec = build_specs([_pack()], call_tool=transport)[0]

    assert (await spec.handler(None)).text == "工具返回：OK"  # 参数缺失不该炸
    assert (await spec.handler({"query": "x"})).ok is True  # 直接给 dict 也认
    assert transport.calls[-1][4] == {"query": "x"}


def test_call_tool_hook_reports_unbound_transport_readably():
    """默认实现：可读中文、不抛异常、不假装成功（真绑定由后续条目替换）。"""
    text = call_tool_hook("stdio", ("npx",), "", "read_file", {})
    assert text == "MCP 工具 read_file 尚未绑定传输通道（真正的连接归技能域条目）。"


@pytest.mark.asyncio
async def test_default_hook_is_honest_not_successful():
    spec = build_specs([_pack()])[0]  # 不注入 call_tool ⇒ 走默认钩子
    obs = await spec.handler(McpToolArgs())
    assert obs.text == "MCP 工具 read_file 尚未绑定传输通道（真正的连接归技能域条目）。"
    assert obs.ok is False  # 没通道 ≠ 成功（types.py：ok=False = 未配置）
    assert obs.materials == []


# --- load_packs：配置解析 -----------------------------------------------------


def test_load_packs_parses_packs_tools_and_defaults():
    raw = json.dumps(
        [
            {
                "slug": "My Server!",
                "name": "演示服务",
                "transport": "stdio",
                "command": ["npx", "-y", "demo-server"],
                "tools": [
                    {"name": "read_file", "description": "读文件"},
                    {"name": "delete_file", "description": "删文件"},
                    {"name": "call", "description": "will upload a file"},
                    {"name": "ping", "enabled": False},
                    "list_files",
                    {"description": "没有名字"},  # 空名 → 丢掉
                ],
            },
            {"slug": "off", "name": "关掉的", "enabled": False, "tools": [{"name": "x"}]},
            {"slug": "gone", "name": "不可用", "available": False, "tools": [{"name": "x"}]},
        ],
        ensure_ascii=False,
    )

    packs = load_packs(raw=raw)

    assert [p.slug for p in packs] == ["my-server", "off", "gone"]
    pack = packs[0]
    assert pack.name == "演示服务"
    assert pack.transport == "stdio"
    assert pack.command == ("npx", "-y", "demo-server")
    assert (pack.url, pack.enabled, pack.available) == ("", True, True)
    tools = {t.name: t for t in pack.tools}
    assert set(tools) == {"read_file", "delete_file", "call", "ping", "list_files"}
    assert tools["read_file"].dangerous is False
    assert tools["delete_file"].dangerous is True  # 名字命中
    assert tools["call"].dangerous is True  # 描述命中
    assert tools["ping"].enabled is False
    assert tools["list_files"].dangerous is False  # 裸字符串写法
    assert packs[1].enabled is False
    assert packs[2].available is False

    # 一路到装配：停用的工具 / 高危工具 / 整包停用 / 整包不可用 全部零暴露
    assert [s.name for s in build_specs(packs)] == [
        "mcp:my-server:read_file",
        "mcp:my-server:list_files",
    ]


def test_load_packs_explicit_dangerous_false_wins_over_name_rule():
    """显式声明优先（老基线同口径）：调用方比正则更清楚这条工具干了什么。"""
    raw = '[{"slug":"demo","tools":[{"name":"delete_file","description":"删归档","dangerous":false}]}]'
    pack = load_packs(raw=raw)[0]
    assert pack.tools[0].dangerous is False
    assert [s.name for s in build_specs([pack])] == ["mcp:demo:delete_file"]


@pytest.mark.parametrize(
    ("raw_slug", "expected"),
    [
        ("My Cool Server!", "my-cool-server"),
        ("  spaced out  ", "spaced-out"),
        ("123abc", "s-123abc"),
        ("a" * 80, "a" * 64),
    ],
)
def test_load_packs_slugify_matches_old_baseline(raw_slug, expected):
    raw = f'[{{"slug": {json.dumps(raw_slug)}, "tools": []}}]'
    assert load_packs(raw=raw)[0].slug == expected


def test_load_packs_slug_falls_back_to_name_then_to_skill():
    assert load_packs(raw='[{"name": "Demo Server"}]')[0].slug == "demo-server"
    # 中文名过不了 [a-z0-9_-] 归一 ⇒ 老基线兜底 "skill"（口径逐字沿用，别改成随机串）
    assert load_packs(raw='[{"name": "我的服务"}]')[0].slug == "skill"


@pytest.mark.parametrize("raw_transport", ["stdio", "http", "sse", "STDIO", "  sse "])
def test_load_packs_keeps_known_transports(raw_transport):
    raw = f'[{{"slug":"demo","transport":{json.dumps(raw_transport)},"tools":[]}}]'
    assert load_packs(raw=raw)[0].transport in ("stdio", "http", "sse")


def test_load_packs_unknown_transport_falls_back_to_stdio():
    pack = load_packs(raw='[{"slug":"demo","transport":"websocket","tools":[]}]')[0]
    assert pack.transport == "stdio"


def test_load_packs_string_command_is_split():
    pack = load_packs(raw='[{"slug":"demo","command":"npx -y demo-server","tools":[]}]')[0]
    assert pack.command == ("npx", "-y", "demo-server")


def test_load_packs_stringy_booleans_follow_readable_intent():
    raw = '[{"slug":"demo","enabled":"false","available":"no","tools":[{"name":"x","enabled":"off"}]}]'
    pack = load_packs(raw=raw)[0]
    assert pack.enabled is False
    assert pack.available is False
    assert pack.tools[0].enabled is False


# --- load_packs：坏配置绝不炸 -------------------------------------------------


def test_load_packs_bad_json_returns_empty_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="app.agent.tools.mcp"):
        assert load_packs(raw="不是 JSON") == []
        assert load_packs(raw="[") == []
        assert load_packs(raw="{}") == []
        assert load_packs(raw='"just a string"') == []
    assert len([r for r in caplog.records if r.name == "app.agent.tools.mcp"]) == 4


def test_load_packs_empty_and_junk_entries_are_silent(caplog):
    with caplog.at_level(logging.WARNING, logger="app.agent.tools.mcp"):
        assert load_packs(raw="") == []
        assert load_packs(raw="   ") == []
        assert load_packs(raw="[1, null, \"x\", {\"slug\": \"ok\"}]")[0].slug == "ok"
    # 空配置是正常状态（首次启动就是空），不该刷 warning
    assert [r for r in caplog.records if r.name == "app.agent.tools.mcp"] == []


def test_load_packs_reads_settings_when_raw_is_none(monkeypatch):
    monkeypatch.setattr(mcp.settings, "MCP_SERVERS", '[{"slug":"demo","tools":[{"name":"read_file"}]}]')
    assert [s.name for s in build_specs(load_packs())] == ["mcp:demo:read_file"]

    monkeypatch.setattr(mcp.settings, "MCP_SERVERS", "")
    assert load_packs() == []


def test_module_stays_free_of_network_and_process_dependencies():
    """钉子：模块自己不得出网 / 起进程（真 transport 走注入点，归 4.5.3）。

    它挡的不是代码风格，而是 `_work-4.1接口冻结.md` §十一.4 的条目边界——4.1 只做
    命名 / 注册 / dangerous 过滤 / 调用钩子；模块里长出真通道就是「不可测的死代码」回来了。
    """
    source = inspect.getsource(mcp)
    for forbidden in ("subprocess", "socket", "httpx", "requests", "urllib"):
        assert forbidden not in source
