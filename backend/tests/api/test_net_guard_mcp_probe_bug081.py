"""`回归修复` 判据载体：**mcp/probe 的 HTTP 通道先过守卫再出网**。

## 本件判什么（D-01 … D-05，逐条可判真假 · **零真出网**）

| 判据 | 腿 | 用例 |
|---|---|---|
| **D-01** | 正控：公网 URL（假 resolver）⇒ 真走到出网（httpx 构造计数 > 0） | `test_d01_*` |
| **D-02** | 拒绝腿逐条各一例：`http://169.254.169.254` / `http://127.0.0.1:9527` / `file:///c:/windows` ⇒ `(tools=[], error=中文)`，**不抛** | `test_d02_*` |
| **D-03** | **零出网腿**：拒绝路径 `httpx.AsyncClient` **构造次数 = 0** | `test_d03_*` |
| **D-04** | 逃生门腿：开 ⇒ 放行 + WARNING | `test_d04_*` |
| **D-05** | **stdio 不回归**：`probe_server(transport="stdio", …)` 行为与改前逐字一致（含 `sse` 走「暂不支持的传输通道」） | `test_d05_*` |

（**D-06** 端点面 `contract_diff` = `53-53-53` 的判定已落退出码（面 ①②③ 任一**未登记**偏离 ⇒ FAIL/退 `1`；PASS 退 `0`；输入类错误退 `2`），本件不落腿。）

## 🔴 为什么 D-03 要断「`AsyncClient` 构造次数」而不只是「桩 post 没被调」

审计回灌 R-D 的实测读数逐字是：把 `http://169.254.169.254/latest/meta-data/` 喂进
`list_tools_http` ⇒ 当时代码 **`REACHED_HTTPX`**、`event_hooks=None`。
⇒ 「拒绝路径不得再 reach httpx」是这条发现的**原语**，所以本件的桩装在**工厂**上：
`httpx.AsyncClient` 每被**构造**一次就 +1。若只桩 `post`，一个「先构造 client 再判」的实现
照样能让 `post` 计数为 0（客户端都建好了，就是没发）—— 那是**假绿**。

## 🔴 为什么 D-01 是**必需**的正控

D-02/D-03 全是「拒绝」类断言。若守卫写成 `raise`（无差别全拒），
D-02/D-03 会**全绿**而功能整体不可用。D-01 把「公网地址**真放行**」钉住 ⇒ 两侧成对。

## 🔴 D-05 判的是「**没动**」——怎么判才不是空靶

「行为逐字一致」不能靠「跑一遍不报错」。本件对 stdio 面三条断言：
① **零 httpx 构造**（stdio 通道与 HTTP 客户端无关）；
② 真起子进程跑通（用真 MCP 假 server，断言工具名）；
③ `sse` / 未知通道仍回「暂不支持的传输通道」且**零子进程**（`回归修复` 的既有口径）。

## 纪律

- **零真出网**：`httpx.AsyncClient` 被换成**计数工厂**，它要么直接抛（拒绝腿），
  要么返回一个只记录调用的假客户端（正控腿）。⇒ 本件**不可能**真连出去。
- resolver 一律 `monkeypatch` 到 `ssrf.resolve_host`（**进程级全局**；直接赋值会漏给后续用例），
  并带 autouse **复位**夹具（照 `tests/api/test_web_ssrf_r2.py:51` 的先例）。
- 逃生门用 `monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)`（**自动还原**）。
- 所有断言**一律无条件**（禁条件式断言）。

## 📌 变异记录（锚点先校验；三段式：改前绿 / 变异红 / 还原绿）

| # | 锚点 | 变异形态 | 必须变红 |
|---|---|---|---|
| **`M-081-C1`** | `mcp_probe.py` 的 `assert_outbound_url_safe(url, purpose=…)` 调用行 | 去掉守卫调用 | D-02 / D-03 |
| **`M-081-C2`** | 同上（把守卫块**挪到** `post` 之后） | 先出网再判 | D-03 |
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import sys

import httpx
import pytest

from app.agent.web import ssrf
from app.api import mcp_probe
from app.core import net_guard
from app.core.config import settings

#: 文件导入时抓一份**真**解析器（本件的前提是它**永不**被真调用）。
_REAL_RESOLVER = ssrf.resolve_host

#: 一个内网可信赖的假 MCP server（只为 **stdio** 腿用；stdio 不起网络连接）。
_MCP_SERVER = """
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2024-11-05"}}), flush=True)
    elif msg.get("method") == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{"name": "echo", "description": "回显"}]}}), flush=True)
"""

#: 探针桩记录到的调用（每次用例重建）。
_probe: dict = {}


@pytest.fixture(autouse=True)
def _isolate_resolver(monkeypatch):
    """把模块级解析器钉回真实现：一条用例留下的桩不得漏给下一条（先例 `test_web_ssrf_r2.py:51`）。"""
    monkeypatch.setattr(ssrf, "resolve_host", _REAL_RESOLVER)


@pytest.fixture(autouse=True)
def _counting_httpx(monkeypatch):
    """把 `httpx.AsyncClient` 换成**工厂桩**：只记「被构造了几次」，绝不真出网。

    🔴 装在这一层而不是桩 `post` 上：D-03 的原语是「拒绝路径不得 **reach httpx**」
    （审计回灌 R-D 的实测读数就是 `REACHED_HTTPX`）⇒ 必须能数**构造**次数。
    `post` 一旦被真调到 ⇒ 直接断言失败（本件的任何腿都不该发出请求）。
    """
    state = {"constructed": 0, "posted": 0}
    _probe.clear()
    _probe.update(state)

    class _FakeClient:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            state["constructed"] += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):  # noqa: ANN002
            return False

        async def post(self, url, **kwargs):  # noqa: ANN001, ANN003
            state["posted"] += 1
            raise AssertionError(f"本件不得真发请求：{url}")

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return state


def _stub_resolver(*addresses: str):
    """桩解析器：**域名**解析成给定地址；**字面地址原样返回**。

    🔴 「字面地址原样返回」不是洁癖，是**必须**照 `getaddrinfo` 的语义来（实测踩到）：
    本件第一版是「一律返回给定地址」⇒ 把 `http://169.254.169.254/…` 这个**字面内网地址**
    洗成了 `8.8.8.8` ⇒ 守卫**正确地**放行了 ⇒ D-03 那条腿红在自己造的假件上。
    先例与理由逐字见 `tests/api/test_web_ssrf_r2.py:68` 的 `_resolver_for_names`。
    """

    def resolve(host: str) -> list[str]:  # noqa: ANN001
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return list(addresses)
        return [host]

    return resolve


def _run(coro):
    """同步跑一次协程（本仓 `pytest.ini` 无 `asyncio_mode` ⇒ 不用 async 用例，更省事）。"""
    return asyncio.run(coro)


# ── D-01 正控：公网 URL ⇒ 真走到出网 ────────────────────────────────────────


def test_d01_public_url_reaches_the_http_client(monkeypatch, _counting_httpx):  # noqa: ANN001
    """**D-01**：公网地址（假 resolver 解析成 `8.8.8.8`）⇒ 守卫放行 ⇒ **真走到 httpx**。

    ⚠️ 本腿**必须**存在：否则「守卫无差别全拒」也能让 D-02/D-03 全绿（那是假绿）。
    构造：`post` 桩会抛 `AssertionError`（本件不允许真发包）⇒ 这条腿断言的是
    「**走到了 post** 并因此被我们的桩拦下」，而不是「探测成功」。
    """
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("8.8.8.8"))

    with pytest.raises(AssertionError) as excinfo:
        _run(mcp_probe.list_tools_http("http://mcp.example.com/mcp"))
    assert "本件不得真发请求" in str(excinfo.value)
    assert _counting_httpx["constructed"] == 1, "公网地址没走到 httpx ⇒ 守卫把正控也拒了"
    assert _counting_httpx["posted"] == 1, "没走到 post ⇒ 本腿不成立"


# ── D-02 拒绝腿（逐条各一例）+ D-03 零出网腿 ────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # 云元数据（审计回灌 R-D 的原读数）
        "http://127.0.0.1:9527/mcp",                # 回环
        "file:///c:/windows/win.ini",               # 非 http(s) scheme
    ],
)
def test_d02_d03_refused_urls_return_readable_error_and_never_reach_httpx(
    url, monkeypatch, _counting_httpx
):  # noqa: ANN001
    """**D-02 + D-03**：拒绝 ⇒ `(tools=[], error=可读中文)`、**不抛**、**零 httpx 构造**。

    🔴 **不抛**是端点契约：`probe_server` 把一切收成 `error` 字符串，设置页拿到的
    必须是「探测失败」而不是 500（本模块既有 docstring 逐字：「不抛 500」）。
    🔴 `constructed == 0` 是 R-D 那条实测读数的**翻绿判据**。
    """
    # 字面地址不走 DNS；给个桩只是为了让本腿**不依赖**这个事实（真解析器也永不真出网）
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("10.0.0.5"))

    tools, error = _run(mcp_probe.probe_server("http", [], url))

    assert tools == [], f"拒绝路径不该有工具：{tools!r}"
    assert error, f"拒绝必须给可读中文 error，实测空串：{error!r}"
    assert "已拒绝访问" in error or "不是合法的网页地址" in error, (
        f"拒绝文案不是守卫给的可读中文：{error!r}"
    )
    assert "MCP 服务器探测" in error, f"拒绝文案必须写明 purpose（是哪件事被拒）：{error!r}"
    # D-03：拒绝路径**根本没碰 httpx**
    assert _counting_httpx["constructed"] == 0, (
        f"拒绝路径仍然构造了 httpx 客户端（R-D 的 REACHED_HTTPX 未翻绿）：{_counting_httpx!r}"
    )
    assert _counting_httpx["posted"] == 0, "拒绝路径不该发出任何请求"


def test_d03_refusal_happens_before_httpx_is_even_importable(monkeypatch, _counting_httpx):  # noqa: ANN001
    """**D-03（结构性补强）**：`list_tools_http` **直接**抛 `McpError`，不经 `probe_server` 兜底。

    为什么要有这条：`probe_server` 的 `except Exception` 会把**任何**异常收成字符串
    ⇒ 「守卫拒绝了」与「守卫炸了」在它的返回值上**看不出区别**（都只是 error 非空）。
    本腿绕过那层兜底，直接断 `list_tools_http` 抛的是 `McpError`（而非任意异常）
    ⇒ 证明拒绝是**受控路径**，不是意外。
    """
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("8.8.8.8"))

    with pytest.raises(mcp_probe.McpError) as excinfo:
        _run(mcp_probe.list_tools_http("http://169.254.169.254/latest/meta-data/"))
    assert "已拒绝访问" in str(excinfo.value)
    assert _counting_httpx["constructed"] == 0, "受控拒绝路径不该构造 httpx 客户端"


# ── D-04 逃生门 ────────────────────────────────────────────────────────────


def test_d04_escape_hatch_passes_and_logs_a_warning(monkeypatch, caplog, _counting_httpx):  # noqa: ANN001
    """**D-04**：`WEB_SSRF_ALLOW_ANY_HOST=True` ⇒ 放行（走到出网）+ 留一条 WARNING。

    ⚠️ 断言按 **logger 名**过滤：`caplog` 是**全 logger** 收集，不过滤会把别的 WARNING
    也收进来（`net_guard` 自己的 docstring 就点名了这个假红）。
    """
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", True)
    # 不桩 resolver：逃生门在**解析之后、判定之前**返回，字面内网地址也能过
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("169.254.169.254"))

    with caplog.at_level(logging.WARNING, logger="app.core.net_guard"):
        with pytest.raises(AssertionError) as excinfo:
            _run(mcp_probe.list_tools_http("http://169.254.169.254/latest/meta-data/"))
    assert "本件不得真发请求" in str(excinfo.value), "逃生门开启时守卫不该拦"
    assert _counting_httpx["constructed"] == 1, "逃生门放行后应真走到 httpx"
    warnings = [r for r in caplog.records if r.name == "app.core.net_guard"]
    assert warnings, "逃生门放行必须留 WARNING（零留痕 = 出事时查不出放过什么）"
    assert any("逃生门已开启" in r.getMessage() for r in warnings), (
        f"WARNING 文案不含逃生门口径：{[r.getMessage() for r in warnings]!r}"
    )


def test_d04_escape_hatch_off_is_the_default(monkeypatch, _counting_httpx):  # noqa: ANN001
    """**D-04（负控）**：逃生门**关**（默认）⇒ 同样 URL 一律拒。

    与上一条成对：没有它，「逃生门」可能只是被写死成常开（那 D-02 就红了，
    但反过来，若 D-02 与 D-04 用不同 URL 构造，单看 D-02 看不出开关是否**真**可读）。
    """
    monkeypatch.setattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False)
    monkeypatch.setattr(ssrf, "resolve_host", _stub_resolver("169.254.169.254"))

    tools, error = _run(
        mcp_probe.probe_server("http", [], "http://169.254.169.254/latest/meta-data/")
    )
    assert tools == [] and error, (tools, error)
    assert _counting_httpx["constructed"] == 0


# ── D-05 stdio 不回归 ──────────────────────────────────────────────────────


def test_d05_stdio_still_works_and_never_touches_httpx(_counting_httpx):  # noqa: ANN001
    """**D-05**：stdio 通道**逐字不变** —— 真起子进程跑通，且与 httpx 一点关系都没有。

    ⚠️ 本腿的 `command` 是**真 python + 假 MCP server**（不走网络）；它证明的是
    「本笔没把守卫套到 stdio 上」——若有人顺手在 `probe_server` 里加一道 URL 判定，
    stdio 腿会因为 `url=""` 被拒而**当场红**。
    """
    tools, error = _run(
        mcp_probe.probe_server("stdio", [sys.executable, "-c", _MCP_SERVER], "")
    )
    assert error == "", f"stdio 通道不该受影响：{error!r}"
    assert [t["name"] for t in tools] == ["echo"], f"stdio 探测结果变了：{tools!r}"
    assert _counting_httpx["constructed"] == 0, "stdio 通道不该碰 httpx"


@pytest.mark.parametrize("transport", ["sse", "websocket", "grpc", "SSE"])
def test_d05_unsupported_transport_keeps_its_wording(transport, _counting_httpx):  # noqa: ANN001
    """**D-05**：`sse` / 未知通道仍回「暂不支持的传输通道」（`回归修复` 既有口径不动）。

    ⚠️ 与 `test_skills_4_5_3.py:428` 同型，但本腿额外钉「**零 httpx 构造**」——
    防本笔把守卫顺手加进了 `probe_server` 的通道分发处（那会让未知通道也走一遍 URL 判定）。
    """
    tools, error = _run(mcp_probe.probe_server(transport, [sys.executable, "-c", _MCP_SERVER], ""))
    assert tools == []
    assert "不支持" in error, error
    assert transport.lower() in error.lower(), error
    assert _counting_httpx["constructed"] == 0, "未知通道不该构造 httpx 客户端"


# ── 结构面：守卫调用点唯一（防「在两处各写一份」） ─────────────────────────


def test_guard_call_site_is_single_and_lives_in_list_tools_http():
    """**结构性判据**：本模块里的守卫调用点**恰为一处**，且在 `list_tools_http` 里。

    为什么需要它：本笔的交付是「接线」——若有人后来在 `probe_server` 里再加一道
    （两处各判一次），行为上未必立刻出错，但「判定入口唯一」这条结构性质就丢了
    （`net_guard.py` 的 docstring 把这条写成了它的设计目的之一）。
    """
    import ast
    from pathlib import Path

    source = Path(mcp_probe.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) and not isinstance(node, ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                if inner.func.id == "assert_outbound_url_safe":
                    calls.append((node.name, inner.lineno))
    assert len(calls) == 1, f"守卫调用点必须恰为一处，实得 {calls!r}"
    assert calls[0][0] == "list_tools_http", f"守卫调用点落错了函数：{calls!r}"


def test_module_does_not_reimplement_the_judgement():
    """**结构面**：本模块**不重判** —— 不出现地址黑名单字面量、不自行 import `ssrf`。

    「判定本体只有一份」是 `回归修复` 的硬纪律①。若有人图省事在这里写一串
    `169.254.169.254` / `127.0.0.1` 字面量或自己 `import ssrf`，本腿当场红。
    """
    import ast
    import io
    import tokenize
    from pathlib import Path

    source = Path(mcp_probe.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # ⚠️ 必须**剥掉注释与 docstring** 再扫：本模块的 docstring 里**刻意**写了
    # `169.254.169.254` 作为「改前会被真发出去的地址」的例子 —— 那是**说明**，不是判定。
    # 只扫注释/文档会让这条腿变成「docstring 里不许提地址」的荒诞约束（实测踩到）。
    docstrings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    comment_lines: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comment_lines.add(token.start[0])
    # 代码面 = 所有**字符串常量**（排除 docstring）+ 非注释行的源码
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in docstrings:
                continue
            literals.append(node.value)
    for literal in ("169.254.169.254", "127.0.0.1", "0.0.0.0", "is_global", "not_public"):
        assert all(literal not in item for item in literals), (
            f"本模块不得重写判定（代码里出现地址/判据字面量 {literal}）"
        )
    # import 面同样只看**代码行**（注释里提到 `ssrf` 是允许的：要说明「不直连」）
    code_lines = [
        line
        for index, line in enumerate(source.splitlines(), start=1)
        if index not in comment_lines
    ]
    code_text = "\n".join(code_lines)
    assert "from app.agent.web import ssrf" not in code_text, (
        "判定必须经 net_guard 薄壳，不得直连 ssrf"
    )
    assert "from app.core import net_guard" in code_text, "必须经薄壳调用"


def test_outbound_blocked_is_a_value_error_so_api_layer_maps_it():
    """**契约面**：`OutboundBlocked` 是 `ValueError`（core 不依赖 fastapi），本模块转 `McpError`。

    为什么判这条：接线方（api 层）自己决定怎么映射异常。若本模块让 `OutboundBlocked`
    **原样穿出** `probe_server`，`skill_routes` 的 `except Exception` 兜不住 ⇒ 500。
    """
    assert issubclass(net_guard.OutboundBlocked, ValueError)
    assert issubclass(mcp_probe.McpError, RuntimeError)
    assert not issubclass(mcp_probe.McpError, net_guard.OutboundBlocked), (
        "McpError 必须是本模块自己的错误类型，不得直接复用 OutboundBlocked（那会把 courier 层的类型泄漏到 api 层）"
    )
