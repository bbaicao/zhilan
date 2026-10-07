"""契约工具单测（阶段 4 · 条目 4.1）：``web_search`` / ``fetch_page`` 的观察、材料与失败口径。

**绝不真出网**：两个工具的 ``build_spec(search=...)`` / ``build_spec(fetch=...)`` 注入点
就是为此存在的——用例全部注入假件，连 ``httpx`` 都不碰。
另加一条结构断言：注入点必须**真被调用**（否则「注入了却用真实现」会静默出网）。
"""
from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from app.agent.registry import ToolRegistry
from app.agent.tools import fetch_page as fetch_tool
from app.agent.tools import web_search as search_tool
from app.agent.web.client import WebHit, WebToolError
from app.core.config import settings


@pytest.fixture(autouse=True)
def _neutral_settings(monkeypatch):
    """钉住与材料上限相关的配置：用例只测口径，不测本机 .env。"""
    monkeypatch.setattr(settings, "WEB_MAX_CONTENT", 4000)


def _hits(*items: tuple[str, str, str]) -> list[WebHit]:
    return [WebHit(title=t, url=u, snippet=s) for t, u, s in items]


def _search_fn(hits: list[WebHit]):
    """假搜索实现（签名同 ``search_web`` 的最简形态：一个位置参数 query）。"""

    async def fake_search(query: str) -> list[WebHit]:
        return hits

    return fake_search


async def _fetch_body(url: str):
    """假抓取：只回正文，不回标题（不触发「页面标题更长就换」的分支）。"""
    return "", f"{url} 的正文内容"


def _fetch_returning(title: str, body: str):
    async def fake_fetch(url: str):
        return title, body

    return fake_fetch


# --------------------------------------------------------------------------
# 工具声明形状（注册表契约：参数模型 / needs_web / async handler）
# --------------------------------------------------------------------------


def test_web_search_tool_declaration():
    spec = search_tool.build_spec()

    assert spec.name == "web_search"
    assert spec.description == "联网搜索并抓取网页，返回网页片段（带 URL，可作为引用来源）"
    assert spec.dangerous is False
    assert spec.needs_web is True  # 联网工具：web_enabled=False 时不得进清单
    assert spec.parameters is search_tool.WebSearchArgs
    assert spec.parameters_schema == search_tool.WebSearchArgs.model_json_schema()
    assert spec.parameters_schema["required"] == ["query"]
    # handler 统一形状：async def handler(args) —— 一个位置参数
    assert inspect.iscoroutinefunction(spec.handler)
    assert list(inspect.signature(spec.handler).parameters) == ["args"]


def test_fetch_page_tool_declaration():
    spec = fetch_tool.build_spec()

    assert spec.name == "fetch_page"
    assert spec.description == "抓取指定网页的正文（只读），适合补全搜索结果里的关键页面"
    assert spec.dangerous is False
    assert spec.needs_web is True
    assert spec.parameters is fetch_tool.FetchPageArgs
    assert spec.parameters_schema["required"] == ["url"]
    assert inspect.iscoroutinefunction(spec.handler)
    assert list(inspect.signature(spec.handler).parameters) == ["args"]


def test_both_tools_are_gated_by_web_enabled():
    """端到端门控：注册进 registry 后，web_enabled=False 时两个工具都不进模型清单。"""
    registry = ToolRegistry()
    registry.register(search_tool.build_spec())
    registry.register(fetch_tool.build_spec())

    assert [s.name for s in registry.for_model(web_enabled=False)] == []
    assert sorted(s.name for s in registry.for_model(web_enabled=True)) == ["fetch_page", "web_search"]


def test_query_and_url_are_required_by_schema():
    with pytest.raises(ValidationError):
        search_tool.WebSearchArgs()
    with pytest.raises(ValidationError):
        fetch_tool.FetchPageArgs()
    assert search_tool.WebSearchArgs(query="q").query == "q"


# --------------------------------------------------------------------------
# web_search：正常路径
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_search_observation_and_materials():
    hits = _hits(("标题一", "https://e.com/1", "摘要一"), ("标题二", "https://e.com/2", "摘要二"))
    spec = search_tool.build_spec(search=_search_fn(hits), fetch=_fetch_body)

    obs = await spec.handler(search_tool.WebSearchArgs(query="问题"))

    assert obs.ok is True and obs.warnings == []
    # 观察文本：每条「[n] 标题 + 摘要前 300 字」，块间空行
    assert obs.text == "[1] 标题一\n摘要一\n\n[2] 标题二\n摘要二"
    assert [m.filename for m in obs.materials] == ["标题一", "标题二"]
    assert [m.url for m in obs.materials] == ["https://e.com/1", "https://e.com/2"]
    # 材料正文 = 抓到的网页正文（摘要拼在前，因为摘要不在正文里）
    assert obs.materials[0].content == "摘要一\n\nhttps://e.com/1 的正文内容"
    assert all(m.chunk_id < 0 for m in obs.materials)  # 网页材料：负号，绝不与 chunks.id 相撞


@pytest.mark.asyncio
async def test_web_search_prefers_longer_page_title():
    """抓到的页面标题更长时用它：搜索结果的标题常被截断（老基线同款）。"""
    hits = _hits(("短", "https://e.com/1", "摘要"))
    spec = search_tool.build_spec(
        search=_search_fn(hits),
        fetch=_fetch_returning("完整页面标题更长一些", "正文正文正文"),
    )

    obs = await spec.handler(search_tool.WebSearchArgs(query="q"))

    assert obs.text.startswith("[1] 完整页面标题更长一些")
    assert obs.materials[0].filename == "完整页面标题更长一些"


@pytest.mark.asyncio
async def test_web_search_keeps_search_title_when_page_title_is_shorter():
    """页面标题更短（常见于「首页」类标题）时不换：换了反而丢信息。"""
    hits = _hits(("搜索结果的完整标题", "https://e.com/1", "摘要"))
    spec = search_tool.build_spec(
        search=_search_fn(hits), fetch=_fetch_returning("首页", "正文正文正文")
    )

    obs = await spec.handler(search_tool.WebSearchArgs(query="q"))

    assert obs.materials[0].filename == "搜索结果的完整标题"


@pytest.mark.asyncio
async def test_web_search_empty_result():
    spec = search_tool.build_spec(search=_search_fn([]))

    obs = await spec.handler(search_tool.WebSearchArgs(query="q"))

    assert obs.text == "没有检索到相关资料。"
    assert obs.materials == [] and obs.warnings == [] and obs.ok is True


@pytest.mark.asyncio
async def test_web_search_dedups_urls_and_skips_empty_url():
    hits = _hits(
        ("A", "https://e.com/1", "s1"),
        ("重复", "https://e.com/1", "s2"),
        ("空", "", "s3"),
    )
    spec = search_tool.build_spec(search=_search_fn(hits), fetch=_fetch_body)

    obs = await spec.handler(search_tool.WebSearchArgs(query="q"))

    assert obs.text.startswith("[1] A\ns1")
    assert len(obs.materials) == 1 and obs.materials[0].url == "https://e.com/1"


@pytest.mark.asyncio
async def test_web_search_observation_snippet_is_clipped_to_300():
    hits = _hits(("A", "https://e.com/1", "字" * 400))
    spec = search_tool.build_spec(search=_search_fn(hits), fetch=_fetch_body)

    obs = await spec.handler(search_tool.WebSearchArgs(query="q"))

    body = obs.text.split("\n", 1)[1]
    assert len(body) == 301 and body.endswith("…")  # 300 字 + 省略号


@pytest.mark.asyncio
async def test_web_search_material_body_is_capped(monkeypatch):
    monkeypatch.setattr(settings, "WEB_MAX_CONTENT", 10000)  # 配置写大也不能撑爆上下文
    hits = _hits(("A", "https://e.com/1", ""))
    spec = search_tool.build_spec(
        search=_search_fn(hits), fetch=_fetch_returning("", "长" * 9000)
    )

    obs = await spec.handler(search_tool.WebSearchArgs(query="q"))

    assert search_tool.MATERIAL_CLIP == 4000
    assert len(obs.materials[0].content) == search_tool.MATERIAL_CLIP


# --------------------------------------------------------------------------
# web_search：失败与降级
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_search_tool_error_becomes_failed_observation():
    async def boom(query: str):
        raise WebToolError("对方拒绝访问（HTTP 403，可能是反爬）：https://e.com")

    spec = search_tool.build_spec(search=boom)

    obs = await spec.handler(search_tool.WebSearchArgs(query="q"))

    # 可读中文同时进观察与警告（警告会追加到最终回答末尾，用户才看得到失败原因）
    assert obs.ok is False
    assert obs.text == "对方拒绝访问（HTTP 403，可能是反爬）：https://e.com"
    assert obs.warnings == [obs.text]
    assert obs.materials == []


@pytest.mark.asyncio
async def test_web_search_unexpected_error_never_raises():
    async def boom(query: str):
        raise RuntimeError("")

    spec = search_tool.build_spec(search=boom)

    obs = await spec.handler(search_tool.WebSearchArgs(query="q"))

    assert obs.ok is False
    # 回归修复（D-C7 升级为双断言）：形状固定；异常 str 为空时仍给出**类名**
    assert obs.text == "联网搜索 执行失败（RuntimeError）。"
    assert obs.warnings == [obs.text]


@pytest.mark.asyncio
async def test_web_search_missing_query():
    spec = search_tool.build_spec(search=_search_fn([]))

    obs = await spec.handler(search_tool.WebSearchArgs(query="   "))

    assert obs.ok is False and obs.materials == []
    assert obs.text == '缺少 query 参数：{"query": "检索词"}'


@pytest.mark.asyncio
async def test_web_search_falls_back_to_snippet_when_fetch_fails():
    async def bad_fetch(url: str):
        raise WebToolError(f"对方拒绝访问（HTTP 403，可能是反爬）：{url}")

    hits = _hits(("A", "https://e.com/1", "摘要一"), ("B", "https://e.com/2", "摘要二"))
    spec = search_tool.build_spec(search=_search_fn(hits), fetch=bad_fetch)

    obs = await spec.handler(search_tool.WebSearchArgs(query="q"))

    assert obs.ok is True  # 抓取失败不终止循环：结果仍可引用（退回摘要）
    assert [m.content for m in obs.materials] == ["摘要一", "摘要二"]
    assert obs.warnings == [
        "网页抓取失败：https://e.com/1",
        "网页抓取失败：https://e.com/2",
        "搜索有结果，但网页都抓取失败，未能并入上下文。",
    ]


@pytest.mark.asyncio
async def test_web_search_partial_fetch_failure_lists_only_that_url():
    async def flaky(url: str):
        if url.endswith("/1"):
            raise WebToolError("抓取失败（HTTP 500）：https://e.com/1")
        return "页面标题", "正文内容"

    hits = _hits(("A", "https://e.com/1", "摘要一"), ("B", "https://e.com/2", "摘要二"))
    spec = search_tool.build_spec(search=_search_fn(hits), fetch=flaky)

    obs = await spec.handler(search_tool.WebSearchArgs(query="q"))

    assert obs.warnings == ["网页抓取失败：https://e.com/1"]  # 只有一条，且没有「全抓挂」总账
    assert obs.materials[0].content == "摘要一"
    assert obs.materials[1].content == "摘要二\n\n正文内容"


@pytest.mark.asyncio
async def test_web_search_injected_hooks_are_really_used(monkeypatch):
    """注入点必须真被调用：注入的假件若不生效，用例会静默真出网（老仓库踩过）。"""
    calls: list[str] = []

    async def spy_search(query: str):
        calls.append(f"search:{query}")
        return _hits(("A", "https://e.com/1", "s"))

    async def spy_fetch(url: str):
        calls.append(f"fetch:{url}")
        return "标题", "正文"

    spec = search_tool.build_spec(search=spy_search, fetch=spy_fetch)
    await spec.handler(search_tool.WebSearchArgs(query="问题"))

    assert calls == ["search:问题", "fetch:https://e.com/1"]


def test_default_spec_uses_real_client_functions():
    """不注入时绑定真实现（``spec`` 是给运维/注册表看的默认声明）。"""
    assert search_tool.spec.name == "web_search"
    assert fetch_tool.spec.name == "fetch_page"


# --------------------------------------------------------------------------
# fetch_page：正常路径与失败
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_page_observation_and_material():
    async def fake_fetch(url: str):
        return "示例页", "正文" * 800  # 1600 字

    spec = fetch_tool.build_spec(fetch=fake_fetch)

    obs = await spec.handler(fetch_tool.FetchPageArgs(url="https://e.com/a"))

    assert obs.ok is True and obs.warnings == []
    assert obs.text == "已抓取「示例页」，正文如下：\n" + "正文" * 600  # 前 1200 字
    assert len(obs.materials) == 1
    material = obs.materials[0]
    assert (material.url, material.filename) == ("https://e.com/a", "示例页")
    assert material.content == "正文" * 800  # 1600 < 4000，全文进材料
    assert material.chunk_id < 0


@pytest.mark.asyncio
async def test_fetch_page_material_body_is_capped():
    async def fake_fetch(url: str):
        return "标题", "长" * 5000

    spec = fetch_tool.build_spec(fetch=fake_fetch)

    obs = await spec.handler(fetch_tool.FetchPageArgs(url="https://e.com/a"))

    prefix = "已抓取「标题」，正文如下：\n"
    assert fetch_tool.MATERIAL_CLIP == 4000 and fetch_tool.OBSERVATION_CLIP == 1200
    assert len(obs.materials[0].content) == fetch_tool.MATERIAL_CLIP
    assert len(obs.text) == len(prefix) + fetch_tool.OBSERVATION_CLIP


@pytest.mark.asyncio
async def test_fetch_page_title_falls_back_to_url():
    async def fake_fetch(url: str):
        return "", "正文内容" * 20

    spec = fetch_tool.build_spec(fetch=fake_fetch)

    obs = await spec.handler(fetch_tool.FetchPageArgs(url="https://e.com/a"))

    assert "「https://e.com/a」" in obs.text
    assert obs.materials[0].filename == "https://e.com/a"


@pytest.mark.asyncio
async def test_fetch_page_tool_error_is_readable():
    async def boom(url: str):
        raise WebToolError("页面不存在（HTTP 404）：https://e.com/x")

    spec = fetch_tool.build_spec(fetch=boom)

    obs = await spec.handler(fetch_tool.FetchPageArgs(url="https://e.com/x"))

    assert obs.ok is False and obs.materials == [] and obs.warnings == []
    assert obs.text == "页面不存在（HTTP 404）：https://e.com/x"


@pytest.mark.asyncio
async def test_fetch_page_unexpected_error_never_raises():
    async def boom(url: str):
        raise ValueError("坏数据")

    spec = fetch_tool.build_spec(fetch=boom)

    obs = await spec.handler(fetch_tool.FetchPageArgs(url="https://e.com/x"))

    # 回归修复（D-C7 升级为双断言）：形状固定 + 原文「坏数据」不外泄
    assert obs.ok is False and obs.text == "抓取网页 执行失败（ValueError）。"
    assert "坏数据" not in obs.text


@pytest.mark.asyncio
async def test_fetch_page_missing_url():
    async def never(url: str):  # pragma: no cover —— 缺参时不该走到抓取
        raise AssertionError("缺 url 时不应调用抓取")

    spec = fetch_tool.build_spec(fetch=never)

    obs = await spec.handler(fetch_tool.FetchPageArgs(url="  "))

    assert obs.ok is False and obs.text == '缺少 url 参数：{"url": "https://…"}'


@pytest.mark.asyncio
async def test_fetch_page_injected_hook_is_really_used():
    calls: list[str] = []

    async def spy(url: str):
        calls.append(url)
        return "标题", "正文" * 20

    spec = fetch_tool.build_spec(fetch=spy)
    await spec.handler(fetch_tool.FetchPageArgs(url="https://e.com/1"))

    assert calls == ["https://e.com/1"]
