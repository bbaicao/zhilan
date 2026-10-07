"""专项收口 · ``回归修复``：**工具异常原文不进观察正文**（10 处逐一 · 判据 A-C4）。

工具异常的公开观察正文必须满足以下约束：

- 10 处**逐一**：观察文本**不含** ``str(exc)`` 原文；
- **含异常类名** —— 形状 = ``{工具名} 执行失败（{异常类名}）。``（D-C6，不得自创）；
- ``ok=False`` 语义不变；``warnings`` **不携带**原文；
- 原文**只进日志**（改前它只在观察文本里，日志里什么都没有）。

10 处落点与观测点一一对应（本文件 10 个用例 + 1 条源码级钉子）：

| # | 落点 | 本文件用例 |
|---|---|---|
| 1 | ``runner.py``（``_run_tool_handler``） | ``test_a_c4_runner_wrapper_scrubs_tool_exception`` |
| 2 | ``mcp_transport.py``（真调用抛错腿） | ``..._mcp_live_handler_scrubs_exception`` |
| 3 | ``mcp_transport.py``（远端返回错误腿） | ``..._mcp_remote_error_text_is_scrubbed`` |
| 4 | ``tools/mcp.py``（注入钩子抛错腿，原文曾同时进 warnings） | ``..._mcp_tools_handler_scrubs_exception_and_warnings`` |
| 5 | ``knowledge_search.py`` | ``..._knowledge_search_scrubs_exception`` |
| 6 | ``list_documents.py`` | ``..._list_documents_scrubs_exception`` |
| 7 | ``read_document.py`` | ``..._read_document_scrubs_exception`` |
| 8 | ``web_search.py``（text 与 warnings 都曾带原文） | ``..._web_search_scrubs_exception_and_warnings`` |
| 9 | ``current_time.py`` | ``..._current_time_scrubs_exception`` |
| 10 | ``fetch_page.py`` | ``..._fetch_page_scrubs_exception`` |

⚠️ **同族但不同面**（本笔**不动**，各有一条如实登记）：``runner.py`` 的 note 步 / P&E 参数校验 ·
``mcp_probe.py`` 的设置页探测 · ``web_search.py`` 与 ``fetch_page.py`` 里「消息即用户文案」的
``WebToolError`` 有意直出（那里的消息本来就是给人看的中文）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import mcp_transport, runner
from app.agent.registry import ToolSpec
from app.agent.tools import current_time, fetch_page as fetch_page_mod
from app.agent.tools import knowledge_search, list_documents, read_document, web_search
from app.agent.tools.mcp import McpPack, McpTool, McpToolArgs, build_specs

BACKEND = Path(__file__).resolve().parents[2]

#: 10 处落点所属的**产品文件**（源码级钉子扫这个集合）
_TOOL_FILES = (
    "app/agent/runner.py",
    "app/agent/mcp_transport.py",
    "app/agent/tools/mcp.py",
    "app/agent/tools/knowledge_search.py",
    "app/agent/tools/list_documents.py",
    "app/agent/tools/read_document.py",
    "app/agent/tools/web_search.py",
    "app/agent/tools/current_time.py",
    "app/agent/tools/fetch_page.py",
)

#: 改前把异常/原因**插进工具观察正文**的写法（源码级钉子）。
#: ⚠️ 这里**故意不含**通用的 ``失败：{exc}`` —— 它在本轮改前还命中 `runner.py` 两处
#: **同族但不同面**的登记项（见下面那条反向钉子），收口包 §2 `064` 明文「不在此列、本笔不动」。
#: ``未执行：{exc}`` 一族则是**有意保留**的用户可读校验文案（范围求交冲突），同样不在清单里。
_LEAK_PATTERNS = (
    "执行失败：{exc}",
    "调用失败：{exc}",
    "失败：{reason}",
    "返回错误：{reason}",
    "知识库检索失败：{exc}",
    "文档列表失败：{exc}",
)

#: **同族但不同面**的登记项（收口包 §2 `064` 如实登记、本笔**不动**）⇒ 钉成「必须仍在」，
#: 防后继会话顺手改到它们（那是另一条口径、另立条目）。
_REGISTERED_OUT_OF_SCOPE = ("Agent 工具装配失败：{exc}", "参数校验失败：{exc}")


class _BoomDB:
    """一个「一查就炸」的会话替身（异常文本里带 SQL 与口令，用来验原文不出圈）。"""

    async def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("SECRET-208 select * from documents where dsn=mysql://root:pw@db")


# --------------------------------------------------------------------------
# 1 · runner.py（ReAct 与 P&E 的唯一汇合点）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_c4_runner_wrapper_scrubs_tool_exception(caplog):
    """``_run_tool_handler``：观察文本固定形状；原文只进日志。"""

    async def boom(args):  # noqa: ANN001
        raise RuntimeError("SECRET-1198 dsn=mysql://root:pw@127.0.0.1/zhilan")

    spec = ToolSpec(
        name="demo_tool",
        description="替身",
        parameters_schema={"type": "object", "properties": {}},
        handler=boom,
        dangerous=False,
        parameters=None,
        needs_web=False,
        needs_confirm=False,
    )
    with caplog.at_level("WARNING", logger="app.agent.runner"):
        obs = await runner._run_tool_handler(spec, None)

    assert obs.ok is False
    assert obs.text == "demo_tool 执行失败（RuntimeError）。"
    assert "SECRET-1198" not in obs.text and "root:pw" not in obs.text
    assert "SECRET-1198" in caplog.text, "原文必须仍然可排障（只进日志）"


# --------------------------------------------------------------------------
# 2 / 3 · mcp_transport.py（真调用抛错腿 + 远端返回错误腿）
# --------------------------------------------------------------------------


class _RaisingAgentTool:
    """上游 ``AgentTool.execute`` 抛错（模拟真 server 掉线 / 协议异常）。"""

    async def execute(self, call_id, args):  # noqa: ANN001
        raise RuntimeError("SECRET-162 conn=mysql://root:pw@10.0.0.9/zhilan_v2")


class _RemoteErrorAgentTool:
    """上游 ``is_error=True``：错误原文来自**远端**（堆栈 / 内部路径都可能在里面）。"""

    async def execute(self, call_id, args):  # noqa: ANN001
        class _Block:
            text = "SECRET-175 at org.example.Server.handle(Server.java:42)"

        class _Result:
            content = [_Block()]
            is_error = True
            details = None

        return _Result()


@pytest.mark.asyncio
async def test_a_c4_mcp_live_handler_scrubs_exception(caplog):
    handler = mcp_transport._make_live_handler(_RaisingAgentTool(), "mcp:demo:read", "read")
    with caplog.at_level("WARNING", logger="app.agent.mcp_transport"):
        obs = await handler(McpToolArgs())

    assert obs.ok is False
    assert obs.text == "MCP 工具 read 执行失败（RuntimeError）。"
    assert "SECRET-162" not in obs.text and "root:pw" not in obs.text
    assert "SECRET-162" in caplog.text


@pytest.mark.asyncio
async def test_a_c4_mcp_remote_error_text_is_scrubbed(caplog):
    handler = mcp_transport._make_live_handler(_RemoteErrorAgentTool(), "mcp:demo:read", "read")
    with caplog.at_level("WARNING", logger="app.agent.mcp_transport"):
        obs = await handler(McpToolArgs())

    assert obs.ok is False
    assert obs.text == "MCP 工具 read 执行失败（远端返回错误）。"
    assert "SECRET-175" not in obs.text
    assert "SECRET-175" in caplog.text, "远端原文也要留在日志里（可排障）"


# --------------------------------------------------------------------------
# 4 · tools/mcp.py（注入钩子抛错腿；**text 与 warnings 都曾带原文**）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_c4_mcp_tools_handler_scrubs_exception_and_warnings(caplog):
    async def boom(transport, command, url, tool_name, args):  # noqa: ANN001
        raise RuntimeError("SECRET-349 dsn=postgres://user:pw@db:5432/zhilan")

    spec = build_specs(
        [McpPack(slug="demo", name="演示", transport="stdio", command=("x",), tools=(McpTool(name="read_file"),))],
        call_tool=boom,
    )[0]
    with caplog.at_level("WARNING", logger="app.agent.tools.mcp"):
        obs = await spec.handler(McpToolArgs())

    assert obs.ok is False
    assert obs.text == "MCP 工具 mcp:demo:read_file 执行失败（RuntimeError）。"
    assert obs.warnings == [obs.text]
    assert all("SECRET-349" not in item and "user:pw" not in item for item in obs.warnings)
    assert "SECRET-349" in caplog.text


# --------------------------------------------------------------------------
# 5 / 6 / 7 · 三个本地工具（检索 / 列文档 / 读文档）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_c4_knowledge_search_scrubs_exception(caplog):
    async def boom(session, question, scope, *, stats=None):  # noqa: ANN001
        raise RuntimeError("SECRET-140 vector store 10.0.0.7:6333 refused")

    spec = knowledge_search.build_spec(db=object(), retrieve=boom)
    with caplog.at_level("WARNING", logger="app.agent.tools.knowledge_search"):
        obs = await spec.handler(knowledge_search.KnowledgeSearchArgs(query="q"))

    assert obs.ok is False
    assert obs.text == "知识库检索 执行失败（RuntimeError）。"
    assert "SECRET-140" not in obs.text
    assert obs.warnings == [] and obs.retrieval_stats is None
    assert "SECRET-140" in caplog.text


@pytest.mark.asyncio
async def test_a_c4_list_documents_scrubs_exception(caplog):
    spec = list_documents.build_spec(db=_BoomDB(), filters=None)
    with caplog.at_level("WARNING", logger="app.agent.tools.list_documents"):
        obs = await spec.handler(list_documents.Args())

    assert obs.ok is False
    assert obs.text == "文档列表 执行失败（RuntimeError）。"
    assert "SECRET-208" not in obs.text
    assert "SECRET-208" in caplog.text


@pytest.mark.asyncio
async def test_a_c4_read_document_scrubs_exception(caplog):
    spec = read_document.build_spec(db=_BoomDB(), filters=None)
    with caplog.at_level("WARNING", logger="app.agent.tools.read_document"):
        obs = await spec.handler(read_document.Args(document_id=1))

    assert obs.ok is False
    assert obs.text == "读取文档 执行失败（RuntimeError）。"
    assert "SECRET-208" not in obs.text
    assert "SECRET-208" in caplog.text


# --------------------------------------------------------------------------
# 8 / 9 / 10 · 联网两工具 + 时钟
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_c4_web_search_scrubs_exception_and_warnings(caplog):
    async def boom(query):  # noqa: ANN001
        raise ValueError("SECRET-93 https://内部地址/api?key=abcd1234")

    spec = web_search.build_spec(search=boom)
    with caplog.at_level("WARNING", logger="app.agent.tools.web_search"):
        obs = await spec.handler(web_search.WebSearchArgs(query="q"))

    assert obs.ok is False
    assert obs.text == "联网搜索 执行失败（ValueError）。"
    assert obs.warnings == [obs.text]
    assert "SECRET-93" not in obs.text and "key=abcd1234" not in obs.text
    assert all("SECRET-93" not in item for item in obs.warnings)
    assert "SECRET-93" in caplog.text


@pytest.mark.asyncio
async def test_a_c4_current_time_scrubs_exception(monkeypatch, caplog):
    def boom():
        raise RuntimeError("SECRET-57 clock broken: /dev/rtc not readable")

    monkeypatch.setattr(current_time, "_now", boom)
    with caplog.at_level("WARNING", logger="app.agent.tools.current_time"):
        obs = await current_time.build_spec().handler(current_time.CurrentTimeArgs())

    assert obs.ok is False
    assert obs.text == "读取本机时间 执行失败（RuntimeError）。"
    assert "SECRET-57" not in obs.text
    assert "SECRET-57" in caplog.text


@pytest.mark.asyncio
async def test_a_c4_fetch_page_scrubs_exception(caplog):
    async def boom(url):  # noqa: ANN001
        raise ValueError("SECRET-68 proxy=http://user:pw@10.0.0.1:8080")

    spec = fetch_page_mod.build_spec(fetch=boom)
    with caplog.at_level("WARNING", logger="app.agent.tools.fetch_page"):
        obs = await spec.handler(fetch_page_mod.FetchPageArgs(url="https://e.com/1"))

    assert obs.ok is False
    assert obs.text == "抓取网页 执行失败（ValueError）。"
    assert "SECRET-68" not in obs.text and "user:pw" not in obs.text
    assert obs.warnings == []
    assert "SECRET-68" in caplog.text


# --------------------------------------------------------------------------
# 源码级钉子：这 10 处落点不得再出现「把异常/原因插进观察文本」的写法
# --------------------------------------------------------------------------


def test_a_c4_no_observation_interpolates_the_raw_exception():
    """源码级：9 个文件里不得再有「把异常/原因插进**工具观察正文**」的写法。

    比逐用例断言更耐「新增分支」——新增一处泄露写法会被这条当场抓住（A-C4 的兜底腿）。
    另一半是**反向钉子**：两处同族但不同面的登记项必须仍然原样在（本笔明文不动）。
    """
    hits: list[str] = []
    for rel in _TOOL_FILES:
        source = (BACKEND / rel).read_text(encoding="utf-8")
        for pattern in _LEAK_PATTERNS:
            if pattern in source:
                hits.append(f"{rel}: {pattern}")
    assert hits == [], hits

    runner_source = (BACKEND / "app/agent/runner.py").read_text(encoding="utf-8")
    assert runner_source.count("失败：{exc}") == 2, "同族不同面的两处登记项数目变了（被顺手改了？）"
    for kept in _REGISTERED_OUT_OF_SCOPE:
        assert kept in runner_source, f"登记项被改动（本笔明文不动）：{kept}"
