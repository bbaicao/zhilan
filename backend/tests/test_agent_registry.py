"""Tool Registry 单测：list / invoke / dangerous 过滤 / 联网门控 / 未知工具。

阶段 4 · 条目 4.1 起，``knowledge_search`` 从**阶段 0 的空桩**换成了真实现
（注入点由 ``set_search_impl`` 换成 ``build_spec(retrieve=...)``）——
原来那两条断言「桩返回 ``{"hits": [], "note": "stage0 stub"}``」的用例随之改写为
「无会话时报可读失败」+「注入的检索实现被真的调用」。
"""

from __future__ import annotations

import pytest

from app.agent.bootstrap import (
    build_default_registry,
    build_registry,
    get_default_registry,
    reset_default_registry,
)
from app.agent.registry import (
    ToolAlreadyRegisteredError,
    ToolNotFoundError,
    ToolRegistry,
    ToolSpec,
)
from app.agent.tools.knowledge_search import (
    TOOL_NAME,
    KnowledgeSearchArgs,
    build_spec as ks_build_spec,
)


def _echo_spec(name: str = "echo", *, dangerous: bool = False, needs_web: bool = False) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="echo",
        parameters_schema={"type": "object", "properties": {}},
        handler=lambda text: {"echo": text},
        dangerous=dangerous,
        needs_web=needs_web,
        needs_confirm=False,
    )


def test_default_registry_has_knowledge_search():
    reg = build_default_registry()
    names = [s.name for s in reg.list()]
    assert TOOL_NAME in names
    assert reg.get(TOOL_NAME).dangerous is False


def test_knowledge_search_declares_typed_parameters():
    """声明必须带参数模型：没有它，运行器就生成不出给模型看的 JSON Schema。"""
    spec = ks_build_spec()
    assert spec.parameters is KnowledgeSearchArgs
    assert spec.parameters_schema["properties"].keys() >= {"query", "limit", "filters"}
    assert spec.parameters_schema["required"] == ["query"]


def test_list_excludes_dangerous_by_default():
    reg = ToolRegistry()
    reg.register(_echo_spec("safe"))
    reg.register(_echo_spec("wipe", dangerous=True))
    assert [s.name for s in reg.list()] == ["safe"]
    assert [s.name for s in reg.list(include_dangerous=True)] == ["safe", "wipe"]


def test_auto_executable_excludes_dangerous():
    reg = ToolRegistry()
    reg.register(_echo_spec("safe"))
    reg.register(_echo_spec("wipe", dangerous=True))
    assert [s.name for s in reg.auto_executable()] == ["safe"]
    # get/list 仍可见 dangerous（审计），仅 auto_executable 排除
    assert reg.get("wipe").dangerous is True


def test_for_model_excludes_dangerous_and_gates_web():
    """条目 4.1.3：模型可见清单 = 非 dangerous **且**（联网工具仅当 web_enabled）。"""
    reg = ToolRegistry()
    reg.register(_echo_spec("safe"))
    reg.register(_echo_spec("wipe", dangerous=True))
    reg.register(_echo_spec("web_search", needs_web=True))
    reg.register(_echo_spec("web_wipe", dangerous=True, needs_web=True))

    assert [s.name for s in reg.for_model()] == ["safe"]
    assert [s.name for s in reg.for_model(web_enabled=True)] == ["safe", "web_search"]
    # dangerous 恒不在，哪怕联网开关打开
    assert "wipe" not in [s.name for s in reg.for_model(web_enabled=True)]
    # 运维视图仍看得见全部（含 dangerous）
    assert [s.name for s in reg.list(include_dangerous=True)] == [
        "safe",
        "wipe",
        "web_search",
        "web_wipe",
    ]


def test_build_registry_gates_web_tools_by_flag():
    """web_enabled=False 时联网工具**根本不进注册表**（不是「注册了再过滤」）。"""
    assert "web_search" not in [s.name for s in build_registry(web_enabled=False).list()]
    assert "fetch_page" not in [s.name for s in build_registry(web_enabled=False).list()]
    names = [s.name for s in build_registry(web_enabled=True).list()]
    assert {"knowledge_search", "web_search", "fetch_page"} <= set(names)
    # 联网声明必须带 needs_web（运行期兜底过滤靠它）
    assert build_registry(web_enabled=True).get("web_search").needs_web is True


def test_invoke_sync_handler():
    reg = ToolRegistry()
    reg.register(_echo_spec())
    assert reg.invoke("echo", text="hi") == {"echo": "hi"}


@pytest.mark.asyncio
async def test_ainvoke_sync_and_async_handlers():
    reg = ToolRegistry()

    async def ahandler(text: str):
        return {"aecho": text}

    reg.register(_echo_spec("sync_echo"))
    reg.register(
        ToolSpec(
            name="async_echo",
            description="",
            parameters_schema={},
            handler=ahandler,
            needs_confirm=False,
        )
    )
    assert await reg.ainvoke("sync_echo", text="x") == {"echo": "x"}
    assert await reg.ainvoke("async_echo", text="y") == {"aecho": "y"}


def test_unknown_tool_raises():
    reg = build_default_registry()
    with pytest.raises(ToolNotFoundError):
        reg.get("no_such_tool")
    with pytest.raises(ToolNotFoundError):
        reg.invoke("no_such_tool")


def test_duplicate_register_raises():
    reg = ToolRegistry()
    reg.register(_echo_spec())
    with pytest.raises(ToolAlreadyRegisteredError):
        reg.register(_echo_spec())


@pytest.mark.asyncio
async def test_knowledge_search_without_session_is_readable_failure():
    """没会话时报可读中文（**不是**「库里没有资料」——那是误导）。"""
    spec = ks_build_spec(db=None)
    obs = await spec.handler(KnowledgeSearchArgs(query="测试"))
    assert obs.ok is False
    assert "会话" in obs.text
    assert obs.materials == []


@pytest.mark.asyncio
async def test_knowledge_search_rejects_empty_query():
    spec = ks_build_spec(db=object())
    obs = await spec.handler(KnowledgeSearchArgs(query="   "))
    assert obs.ok is False
    assert "query" in obs.text


@pytest.mark.asyncio
async def test_knowledge_search_uses_injected_retrieval():
    """注入的检索实现被真的调用，且**范围 filters 一路传下去**（阶段 3 接线四条之 1）。"""
    from app.retrieval.types import Material

    calls: list[tuple] = []

    async def fake_retrieve(session, question, scope, *, stats=None):
        calls.append((session, question, scope))
        return (
            [Material(chunk_id=1, document_id=2, content="正文", filename="a.md")],
            "[1]（文档：a.md）\n正文",
            None,
        )

    marker = object()
    spec = ks_build_spec(db=marker, filters={"kb_ids": [7]}, retrieve=fake_retrieve)
    obs = await spec.handler(KnowledgeSearchArgs(query="蓝鲸"))
    assert obs.ok is True
    assert calls == [(marker, "蓝鲸", {"kb_ids": [7]})]
    assert len(obs.materials) == 1
    assert obs.materials[0].filename == "a.md"
    # 工具侧**不做**快照编号（那是运行器的事）⇒ 这里 ordinal 还是 0
    assert obs.materials[0].ordinal == 0


@pytest.mark.asyncio
async def test_knowledge_search_tool_filters_are_narrowed_into_turn_scope():
    """工具参数的 ``filters`` 只能**收窄**本轮范围（专项 F6-A / ``回归修复``）。

    ⚠️ **本条替换了原来的 ``..._tool_level_filters_override_turn_scope``**——旧断言写的是
    ``seen == [{"kb_ids": [9]}, {"kb_ids": [1]}]``，即「工具参数**整份替换**宿主范围」，
    而那正是本项要堵的越界（宿主选定的 ``[1]`` 被模型换成 ``[9]``）⇒ **判据反转**，
    不是为了过测试削弱新约束：三条分支（冲突 / 合法收窄 / 不给 filters）都留着断言，
    且多加了一条「冲突时**一次检索都不发**」。变更原因见 F6-A 验收报告。
    """
    seen: list = []

    async def fake_retrieve(session, question, scope, *, stats=None):
        seen.append(scope)
        return [], "", None

    spec = ks_build_spec(db=object(), filters={"kb_ids": [1, 2]}, retrieve=fake_retrieve)

    # ① 越界：与宿主范围没有交集 ⇒ 拒绝，且**不发出任何检索**
    denied = await spec.handler(KnowledgeSearchArgs(query="q", filters={"kb_ids": [9]}))
    assert denied.ok is False
    assert "知识库检索未执行" in denied.text
    assert seen == []

    # ② 合法收窄：收下两者之交
    ok = await spec.handler(KnowledgeSearchArgs(query="q", filters={"kb_ids": [2, 3]}))
    assert ok.ok is True
    assert seen == [{"kb_ids": [2]}]

    # ③ 不给 filters ⇒ 保留宿主范围（不是清空）
    await spec.handler(KnowledgeSearchArgs(query="q"))
    assert seen == [{"kb_ids": [2]}, {"kb_ids": [1, 2]}]


@pytest.mark.asyncio
async def test_knowledge_search_failure_is_observation_not_exception():
    """检索抛异常 → 转观察（I5：工具失败绝不炸穿）。"""

    async def boom(session, question, scope, *, stats=None):
        raise RuntimeError("向量库炸了")

    spec = ks_build_spec(db=object(), retrieve=boom)
    obs = await spec.handler(KnowledgeSearchArgs(query="q"))
    assert obs.ok is False
    # 回归修复：形状固定 + 异常原文不外泄
    assert obs.text == "知识库检索 执行失败（RuntimeError）。"
    assert "向量库炸了" not in obs.text


def test_default_registry_is_resettable_singleton():
    a = get_default_registry()
    b = get_default_registry()
    assert a is b
    c = reset_default_registry()
    assert c is not a
    assert TOOL_NAME in [s.name for s in c.list()]


# --------------------------------------------------------------------------
# A-C6 · MCP 契约名碰撞：**装配整轮不废**（回归修复）
# --------------------------------------------------------------------------


def _dup_packs():
    """两份 **slug 相同**的 MCP 包 ⇒ 契约名 ``mcp:dup:t1`` 必然重名（`mcp:{slug}:{tool}`）。"""
    from app.agent.tools.mcp import McpPack, McpTool

    return [
        McpPack(slug="dup", name="甲包", transport="stdio", command=("x",), tools=(McpTool(name="t1"),)),
        McpPack(
            slug="dup",
            name="乙包",
            transport="stdio",
            command=("y",),
            tools=(McpTool(name="t1"), McpTool(name="t2")),
        ),
    ]


def test_a_c6_duplicate_contract_name_is_skipped_not_fatal(caplog):
    """改前：`register` 抛 `ToolAlreadyRegisteredError` ⇒ **整轮装配作废**（连非 MCP 工具一起没）。

    修复口径（D-C9）：注册前按 `spec.name` 去重、后到者跳过 + warning；契约名唯一、先到者胜。
    """
    with caplog.at_level("WARNING", logger="app.agent.bootstrap"):
        registry = build_registry(mcp_packs=_dup_packs())

    names = [spec.name for spec in registry.list(include_dangerous=True)]
    assert "mcp:dup:t1" in names and "mcp:dup:t2" in names, names
    assert len(names) == len(set(names)), names
    assert TOOL_NAME in names, "整轮装配不得因一条重名契约作废"
    assert "MCP 契约名重复，已跳过：mcp:dup:t1" in caplog.text


def test_a_c6_specs_path_is_deduped_too(caplog):
    """`mcp_specs` 分支与 `mcp_packs` 分支**共用同一处实现** ⇒ 同样去重。"""
    from app.agent.tools.mcp import build_specs

    specs = build_specs(_dup_packs())
    assert [s.name for s in specs] == ["mcp:dup:t1", "mcp:dup:t1", "mcp:dup:t2"]  # 夹具自检
    with caplog.at_level("WARNING", logger="app.agent.bootstrap"):
        registry = build_registry(mcp_specs=specs)

    names = [spec.name for spec in registry.list(include_dangerous=True)]
    assert names.count("mcp:dup:t1") == 1, names
    assert "MCP 契约名重复，已跳过：mcp:dup:t1" in caplog.text


def test_a_c6_no_collision_is_silent_and_keeps_order(caplog):
    """反控：不冲突时逐字不变（全注册、顺序 = 装配顺序、零「契约名重复」warning）。

    ⚠️ 注册表**本体**的重名语义本笔**不动** —— 见本文件既有的
    ``test_duplicate_register_raises``（`ToolRegistry.register` 仍抛）。
    """
    from app.agent.tools.mcp import McpPack, McpTool

    packs = [
        McpPack(slug="a", name="甲", transport="stdio", command=("x",), tools=(McpTool(name="t1"),)),
        McpPack(slug="b", name="乙", transport="stdio", command=("y",), tools=(McpTool(name="t1"),)),
    ]
    with caplog.at_level("WARNING", logger="app.agent.bootstrap"):
        registry = build_registry(mcp_packs=packs)

    names = [spec.name for spec in registry.list(include_dangerous=True)]
    assert names[-2:] == ["mcp:a:t1", "mcp:b:t1"], names
    assert "契约名重复" not in caplog.text
