"""联网搜索与网页抓取（阶段 4 · 条目 4.1；行为基线 = 老仓库 ``app/rag/web_tools.py``）。

## 安全口径（老基线 + R2-12 的 SSRF 白名单）

只做**搜索 + 抓取只读网页**：不执行脚本、不绕登录墙、不做批量爬取；
一次搜索最多取 ``WEB_MAX_RESULTS`` 条，每页正文截到 ``WEB_MAX_CONTENT``；
超时 / 非 HTML / 反爬失败一律转成**可读中文** :class:`WebToolError`
——它的 message 就是用户看到的文案，所以不夹英文异常名与堆栈。

**R2-12 起本模块的 7 处出网 client 一律过守卫**：它们全部经 :func:`_make_client`，
而它默认装 ``event_hooks={"request": [_guard_hook]}`` + ``max_redirects=_MAX_REDIRECTS``
⇒ 每个请求（含**重定向每一跳**）都过 :func:`app.agent.web.ssrf.assert_url_is_safe`
的白名单判定，:func:`_maybe_pin` 再把判定到的地址钉进本次连接。

⚠️ **守卫范围只有 web 工具面**：本仓另有 8 处出网点**不过守卫**——``vector/embed.py`` /
``retrieval/rerank.py`` / ``api/translate_spec.py`` / ``api/settings_routes.py``（模型探测）
/ ``api/mcp_probe.py`` / ``vector/qdrant_store.py``（是 ``QdrantClient``，非 httpx）
+ LLM 与 MCP 两条 ``httpx2`` 腿。**不得**据此声称「全部出网面受保护」。

逃生门 ``WEB_SSRF_ALLOW_ANY_HOST`` **默认关**、只走 ``.env``（设置页的 ``web`` 段仍是 400）
⇒ 开启时**每次放行都留一条 WARNING**；pinning 侧另有「代理在场 ⇒ 降级 + WARNING」一条腿。

## 两条渠道（``WEB_CHANNEL``）

本模块只实装 ``api``（传统搜索 API）。``llm``（大模型搜索）在条目 4.1 **有意不实装**
（`接口冻结 §十一` 偏差登记 2），被选中时**显式抛错**而不是静默退回 api 渠道——
静默退回会让「设置里明明选了 llm」的用户以为自己选的服务商坏了，排查方向全错。

## 门控不在本模块

``WEB_ENABLE`` 的总开关在运行器侧（``ToolRegistry.for_model(web_enabled=...)``）：
不联网就**根本不把联网工具暴露给模型**。到本模块的调用已经过了那道闸，
所以这里不再读 ``WEB_ENABLE``——两处都判会分叉出两种门控语义（老基线是工具内判，
本仓按条目 4.1 收口到注册表一处）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.core import derived_counters
from app.core.config import settings
from app.retrieval.types import Material, web_chunk_id

from app.agent.web import pinning, ssrf
from app.agent.web.spec import (
    CHANNEL_API,
    CHANNEL_LLM,
    normalize_provider,
    provider_spec,
)

# 本模块的日志通道（专项工具升级-2 · R2-10 新建：此前本文件**零 logger**）。
# pinning 降级与逃生门放行都走它。判据侧一律**按 logger 名过滤**：``caplog`` 是
# **全 logger** 收集，不过滤就会把 httpx / httpcore / asyncio 的 WARNING 收进来（假红）。
logger = logging.getLogger(__name__)

#: 重定向跳数上限：**显式写死**（httpx 的现值就是 20 ⇒ 零行为变更，只是不再依赖上游默认）。
#: ⚠️ httpx 的判定是 ``len(history) > self.max_redirects`` ⇒ 20 允许 21 次请求后拒。
_MAX_REDIRECTS = 20

#: `回归修复`：字节上限相对**字符上限**的余量倍数。原始 body 字节与提取后正文字符
#: 是两个量纲（UTF-8 汉字 3 字节/字 + HTML 标记/实体开销），取 6 倍保「正文顶格」的
#: 页面不被字节侧误伤；超字节的 body 仍会被尾部 ``_clip`` 按字符上限裁。
_FETCH_BYTES_HEADROOM = 6

# 保守 UA：部分站点对默认的 ``python-httpx`` UA 直接 403
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# 剥正文时要**整棵丢**的标签（导航/脚本/表单对摘要无益，还常含重复噪声）
_SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "svg",
        "iframe",
        "form",
        "nav",
        "footer",
        "header",
        "aside",
        "button",
        "select",
        "option",
        "template",
    }
)

# 这些标签前后要换行，否则「标题正文标题正文」会粘成一句，摘要检索全失效
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "br",
        "hr",
        "blockquote",
        "pre",
        "td",
        "th",
    }
)


class WebToolError(Exception):
    """联网工具的可读中文错误（消息即用户可见文案）。"""


@dataclass
class WebHit:
    """一条搜索结果（搜索是**元数据**，正文另抓；见 :func:`app.agent.web.client.fetch_page`）。"""

    title: str
    url: str
    snippet: str


# --------------------------------------------------------------------------
# HTML → 正文（纯函数，stdlib 自研：不引入解析依赖，且行为可逐字断言）
# --------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """把 HTML 剥成正文：丢掉 script/style/nav 等，块级标签换行，``<title>`` 单独收集。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = (tag or "").lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = (tag or "").lower()
        if tag in _SKIP_TAGS:
            # 用 max(0, ...) 兜住「只见到闭合、没见到开标签」的残缺 HTML，别让 skip 卡死
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0 or not data:
            return
        if self._in_title:
            self.title += data
            return
        self._parts.append(data)

    def text(self) -> str:
        return _normalize_whitespace("".join(self._parts))


def _normalize_whitespace(text: str) -> str:
    """按行归一：行内连续空白压成一个空格，丢掉空行（供模型读的正文不需要空行噪声）。"""
    lines = []
    for line in (text or "").splitlines():
        cleaned = re.sub(r"[ \t\u3000\u00a0]+", " ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def html_to_text(html: str) -> tuple[str, str]:
    """HTML → ``(title, 正文)``。**纯函数**：同一输入两次调用逐字节相同。

    解析异常时退回「粗暴去标签」而不是把异常抛给调用方：抓到的页面千奇百怪，
    为此让整次抓取失败不值得（正文质量差还有下面的「几乎没正文」闸兜底）。
    """
    parser = _TextExtractor()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:  # noqa: BLE001 —— HTMLParser 对畸形输入可能抛，兜住即可
        rough = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
        rough = re.sub(r"(?s)<[^>]+>", " ", rough)
        # 回归修复（S18）：正文清洗降级 ⇒ 拿到的是「粗暴去标签」的粗糙文本（原先连 log 都没有）。
        derived_counters.bump("web_fetch_degrade")
        return "", _normalize_whitespace(rough)
    return parser.title.strip(), parser.text()


def _clip(text: str, limit: int) -> str:
    """截断加省略号（与 ``app.agent.types.clip`` 同形；本模块不 import agent 以保持低耦合）。"""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def _title_from_url(url: str) -> str:
    """从 URL 猜个标题（搜索页偶尔不给标题；空白标题在引用卡片上很难看）。"""
    try:
        path = urlparse(url).path.rstrip("/")
        name = unquote(path.rsplit("/", 1)[-1]) if path else ""
    except Exception:  # noqa: BLE001 —— urlparse 对畸形串可能抛，标题不值得让抓取失败
        name = ""
        # 回归修复（S18）：标题提取降级 ⇒ 回落 netloc/URL（原先静默）。
        derived_counters.bump("web_fetch_degrade")
    return name or urlparse(url).netloc or url


# --------------------------------------------------------------------------
# 出网 client 的唯一构造点（专项工具升级-2 · R2-10）
# --------------------------------------------------------------------------


def _maybe_pin(client: httpx.AsyncClient) -> None:
    """把「判定时解析到的地址」钉进本次连接（关掉 TOCTOU / DNS rebinding，R2-10）。

    ⚠️ **null-safe 是硬要求**：既有用例把 ``web_client.httpx.AsyncClient`` 整体换成假件，
    而假件**没有** ``_transport``（``tests/agent/test_web_client.py`` 的 ``_FakeAsyncClient``
    只有 ``_script`` / ``_log`` / ``opts``）⇒ 这里一旦直取属性，既有 60 余条会集体
    ``AttributeError``、A26 当场不成立。拿不到接缝就**跳过**（绝不抛，只留一条 debug）。

    ⚠️ **代理在场必须降级，而且降级判定要看 ``client._mounts``**（「代理」= ``urllib.request.getproxies()``
    的结果，Windows/macOS 会回退读**系统代理设置**，不止环境变量）：实测本机带
    ``HTTP(S)_PROXY`` 时 ``client._transport._pool`` **仍是** ``AsyncConnectionPool``，
    代理是挂在 ``client._mounts`` 上的 ⇒ 只看 ``_pool`` 会在真代理下**静默失效、连
    warning 都没有**。降级 = 不 pin + warning（**fail-open，如实登记**）。

    ⚠️ **不要**改用 ``AsyncClient(transport=...)`` 来换通道：它会**静默关掉环境代理**
    （``httpx/_client.py``：``allow_env_proxies = trust_env and transport is None``）。
    """
    if getattr(client, "_mounts", None):
        # ⚠️ 「代理」= `urllib.request.getproxies()` 的结果（Windows/macOS 会回退读**系统代理设置**），
        # 不止 HTTP(S)_PROXY 环境变量 —— 对抗审计实测：只设注册表代理时 `_mounts` 也会非空。
        logger.warning("检测到代理（环境变量或系统代理设置）：DNS pinning 降级为不固定（本次连接不做地址绑定）")
        # 回归修复（S19）：代理在场 ⇒ fail-open 不 pin（安全面降级，原先只有一条日志）。
        derived_counters.bump("web_fetch_degrade")
        return
    pool = getattr(getattr(client, "_transport", None), "_pool", None)
    inner = getattr(pool, "_network_backend", None)
    if pool is None or inner is None:
        # 假件（既有用例是常态）或**上游改了接缝形状** ⇒ 不做地址绑定。
        # 这里**不留 warning**（否则既有假件用例会被刷屏），但留一条 debug：
        # 「上游升级导致接缝消失」时开 DEBUG 就能看见（对抗审计 c-2：不静默）。
        logger.debug("未找到可注入的传输接缝（假件或上游改名）：本次连接不做地址绑定")
        return
    pool._network_backend = pinning.make_backend(inner)  # noqa: SLF001 —— httpcore 无公开接缝


async def _guard_hook(request: httpx.Request) -> None:
    """出站请求守卫：**每个请求、每个重定向跳**都过同一道白名单判定（R2-11）。

    ⚠️ **必须是 ``async def``**：httpx 对 ``request`` 钩子**无条件 ``await``**，写成同步
    函数会抛 ``TypeError: object NoneType can't be used in 'await' expression``（实测）
    ⇒ 钩子腿全废、而且是在**真实请求时**才炸。

    ⚠️ **为什么装钩子而不是「入口前置解析」**：入口前置会让**断网机器**上既有
    ``fetch_page`` 用例（真调 ``fetch_page`` 的那 10 条）在解析失败时成片红，与 A26
    「既有用例全绿」结构性互斥；装钩子则相反 —— 既有假件（``_FakeAsyncClient``）
    **从不调用钩子** ⇒ 既有用例不碰 DNS、断网全绿。

    ⚠️ 重定向的每一跳都会**再调一次本钩子**（httpx 在 ``_send_single_request`` 之前调，
    实测 302 时钩子被调 2 次、钩子抛异常后 handler 只被调 1 次）⇒ 「每跳重验」天然成立，
    **不必**自写重定向环。
    """
    allow_any_host = bool(getattr(settings, "WEB_SSRF_ALLOW_ANY_HOST", False))
    try:
        # 回归修复：白名单判定 → resolve_host → ``socket.getaddrinfo`` 是**阻塞**调用，
        # 而本钩子跑在事件循环线程上 ⇒ 直接调会把整个后端卡到 DNS 超时（钩子每请求每跳
        # 各一次）。照 ``pinning.py`` 自立的纪律「解析一律丢线程」执行；异常语义不变
        # （线程内抛的 ``UrlNotAllowed`` 在 await 处原样重现，仍由 except 转 ``WebToolError``）。
        await asyncio.to_thread(
            ssrf.assert_url_is_safe, str(request.url), allow_any_host=allow_any_host
        )
    except ssrf.UrlNotAllowed as exc:
        raise WebToolError(str(exc)) from exc
    if allow_any_host:
        # 逃生门开启时**每次放行都留痕**（默认关 ⇒ 这条分支不存在）
        logger.warning(
            "SSRF 逃生门已开启（WEB_SSRF_ALLOW_ANY_HOST）：放行 %s", request.url.host
        )


def _make_client(**kwargs) -> httpx.AsyncClient:
    """出网 ``AsyncClient`` 的**唯一**构造点（A26′ 的机器判据就数它）。

    ``kwargs`` **逐字透传**给 ``httpx.AsyncClient``（``timeout`` / ``headers`` /
    ``follow_redirects`` 等一律照旧 ⇒ 既有 14 处 ``opts`` 断言逐键读，不受影响）。

    **R2-12 起守卫与重定向上限在这里统一带上**（7 处一并生效，不必逐处写）：
    漏接任何一处都会被 A26′ 的 AST 判据抓住（它数「直构 == 1」与「``_make_client`` 调用 == 7」）。
    """
    hooks = {name: list(callbacks) for name, callbacks in (kwargs.pop("event_hooks", None) or {}).items()}
    hooks["request"] = [_guard_hook] + [
        callback for callback in hooks.get("request", []) if callback is not _guard_hook
    ]
    max_redirects = kwargs.pop("max_redirects", _MAX_REDIRECTS)
    client = httpx.AsyncClient(
        event_hooks=hooks, max_redirects=max_redirects, **kwargs
    )
    _maybe_pin(client)
    return client


# --------------------------------------------------------------------------
# 搜索：六家 api 渠道服务商（请求形状/鉴权/解析逐条对齐老基线）
# --------------------------------------------------------------------------


def _ddg_redirect_url(href: str) -> str:
    """DuckDuckGo HTML 页的链接常是 ``//duckduckgo.com/l/?uddg=<encoded>``，还原真地址。"""
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlparse(href)
    except Exception:  # noqa: BLE001
        return href
    if "duckduckgo.com" in (parsed.netloc or "") and parsed.path.startswith("/l/"):
        qs = parse_qs(parsed.query)
        target = (qs.get("uddg") or qs.get("ud") or [""])[0]
        if target:
            return unquote(target)
    return href


class _DdgResultParser(HTMLParser):
    """从 ``html.duckduckgo.com`` 结果页抠出 ``title/url/snippet``。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[WebHit] = []
        self._in_result = False
        self._in_a = False
        self._in_snippet = False
        self._href = ""
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        cls = attr.get("class", "")
        tag = (tag or "").lower()
        if tag == "div" and "result" in cls.split() and "results_links" in cls:
            self._flush()
            self._in_result = True
            self._href = ""
            self._title_parts = []
            self._snippet_parts = []
        if not self._in_result:
            return
        if tag == "a" and ("result__a" in cls or not self._href):
            href = attr.get("href", "")
            if href and ("result__a" in cls or "result__url" in cls or "uddg=" in href):
                self._in_a = True
                self._href = href
        if tag == "a" and "result__snippet" in cls:
            self._in_snippet = True
        if tag == "td" and "result__snippet" in cls:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        tag = (tag or "").lower()
        if tag == "a":
            self._in_a = False
            self._in_snippet = False
        if tag in ("td", "div") and self._in_snippet:
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if not self._in_result or not data:
            return
        if self._in_a:
            self._title_parts.append(data)
        elif self._in_snippet:
            self._snippet_parts.append(data)

    def _flush(self) -> None:
        """把攒到的一条结果落下来。**下一个结果块开始前**调用（末条在 :meth:`close`）。"""
        if self._href:
            url = _ddg_redirect_url(self._href)
            title = _normalize_whitespace("".join(self._title_parts))
            snippet = _normalize_whitespace("".join(self._snippet_parts))
            if url.startswith("http"):
                self.results.append(
                    WebHit(title=title or _title_from_url(url), url=url, snippet=snippet)
                )
        self._in_result = False
        self._in_a = False
        self._in_snippet = False

    def close(self) -> None:  # type: ignore[override]
        super().close()
        self._flush()


async def _search_duckduckgo(query: str, *, timeout: float) -> list[WebHit]:
    """免 Key 服务商：抓 DuckDuckGo 的 HTML 结果页（非官方接口，可能被限流）。"""
    url = "https://html.duckduckgo.com/html/"
    async with _make_client(
        timeout=timeout, follow_redirects=True, headers={"User-Agent": _UA}
    ) as client:
        resp = await client.post(url, data={"q": query, "b": ""})
    if resp.status_code >= 400:
        raise WebToolError(
            f"DuckDuckGo 搜索失败：HTTP {resp.status_code}（可能被限流，请稍后再试或换服务商）"
        )
    body = resp.text or ""
    # 反爬页会返回 200；不认出来的话会静默变成「没有结果」，用户只会以为搜不到
    if "anomaly" in body.lower() and "challenge" in body.lower():
        raise WebToolError("DuckDuckGo 返回了反爬验证页，请稍后再试，或到设置里换成 Serper / Tavily。")
    parser = _DdgResultParser()
    parser.feed(body)
    parser.close()
    return parser.results


async def _search_serper(
    query: str, api_key: str, *, timeout: float, max_results: int
) -> list[WebHit]:
    async with _make_client(
        timeout=timeout, headers={"X-API-KEY": api_key, "User-Agent": _UA}
    ) as client:
        resp = await client.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": max(3, int(max_results))},
        )
    if resp.status_code in (401, 403):
        raise WebToolError("Serper API Key 无效或已过期，请到设置 → 联网检索里核对。")
    if resp.status_code >= 400:
        raise WebToolError(f"Serper 搜索失败：HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise WebToolError("Serper 返回的不是 JSON，搜索未完成。") from exc
    out: list[WebHit] = []
    for item in data.get("organic") or []:
        if not isinstance(item, dict):
            continue
        link = str(item.get("link") or "").strip()
        if not link.startswith("http"):
            continue
        out.append(
            WebHit(
                title=str(item.get("title") or _title_from_url(link)).strip(),
                url=link,
                snippet=str(item.get("snippet") or "").strip(),
            )
        )
    return out


async def _search_tavily(
    query: str, api_key: str, *, timeout: float, max_results: int
) -> list[WebHit]:
    async with _make_client(timeout=timeout, headers={"User-Agent": _UA}) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": max(1, int(max_results)),
                "include_raw_content": False,
            },
        )
    if resp.status_code in (401, 403):
        raise WebToolError("Tavily API Key 无效或已过期，请到设置 → 联网检索里核对。")
    if resp.status_code >= 400:
        raise WebToolError(f"Tavily 搜索失败：HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise WebToolError("Tavily 返回的不是 JSON，搜索未完成。") from exc
    out: list[WebHit] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        link = str(item.get("url") or "").strip()
        if not link.startswith("http"):
            continue
        out.append(
            WebHit(
                title=str(item.get("title") or _title_from_url(link)).strip(),
                url=link,
                snippet=_clip(str(item.get("content") or ""), 400),
            )
        )
    return out


async def _search_bocha(
    query: str, api_key: str, *, timeout: float, max_results: int
) -> list[WebHit]:
    """博查搜索：``POST https://api.bochaai.com/v1/web-search``（Bearer 鉴权）。"""
    async with _make_client(
        timeout=timeout,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": _UA,
        },
    ) as client:
        resp = await client.post(
            "https://api.bochaai.com/v1/web-search",
            json={"query": query, "summary": True, "count": max(1, int(max_results))},
        )
    if resp.status_code in (401, 403):
        raise WebToolError("博查 API Key 无效或已过期，请到设置 → 联网检索里核对。")
    if resp.status_code >= 400:
        raise WebToolError(f"博查搜索失败：HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise WebToolError("博查返回的不是 JSON，搜索未完成。") from exc

    pages = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
    out: list[WebHit] = []
    for item in pages:
        if not isinstance(item, dict):
            continue
        link = str(item.get("url") or "").strip()
        if not link.startswith("http"):
            continue
        out.append(
            WebHit(
                title=str(item.get("name") or _title_from_url(link)).strip(),
                url=link,
                snippet=_clip(str(item.get("snippet") or item.get("content") or ""), 400),
            )
        )
    return out


def _parse_baidu_items(data: dict) -> list[WebHit]:
    """兼容多种百度智能云返回结构：``webPages.value`` / ``result.list`` / ``data.items``。

    百度这套端点在各地/各版本返回结构不一，写死一种就会「明明 200 却零结果」。
    """
    candidates: list = []
    web_pages = (data.get("webPages") or {}).get("value")
    if isinstance(web_pages, list):
        candidates.extend(web_pages)
    result_list = (data.get("result") or {}).get("list")
    if isinstance(result_list, list):
        candidates.extend(result_list)
    data_items = (data.get("data") or {}).get("items")
    if isinstance(data_items, list):
        candidates.extend(data_items)
    if isinstance(data.get("items"), list):  # 兜底：顶层直接是 list
        candidates.extend(data["items"])

    out: list[WebHit] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        link = str(item.get("url") or item.get("link") or item.get("href") or "").strip()
        if not link.startswith("http") or link in seen:
            continue
        seen.add(link)
        out.append(
            WebHit(
                title=str(item.get("title") or item.get("name") or _title_from_url(link)).strip(),
                url=link,
                snippet=_clip(
                    str(
                        item.get("snippet")
                        or item.get("summary")
                        or item.get("content")
                        or item.get("abstract")
                        or ""
                    ),
                    400,
                ),
            )
        )
    return out


async def _search_baidu(
    query: str, api_key: str, *, timeout: float, max_results: int
) -> list[WebHit]:
    """百度智能云搜索：端点与鉴权以官方文档为准，这里做兼容解析。

    同时带 ``Bearer`` 与 ``X-API-KEY`` 两种常见鉴权头：官方文档改口径时少一次线上事故。
    """
    base_url = "https://aip.baidubce.com/rest/2.0/search/v1/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
        "User-Agent": _UA,
    }
    async with _make_client(timeout=timeout, headers=headers) as client:
        resp = await client.post(
            base_url,
            json={"query": query, "count": max(1, int(max_results))},
        )
    if resp.status_code in (401, 403):
        raise WebToolError(
            "百度 API Key 无效或已过期，请到设置 → 联网检索里核对（需百度智能云搜索服务）。"
        )
    if resp.status_code >= 400:
        raise WebToolError(
            f"百度搜索失败：HTTP {resp.status_code}（请核对端点与鉴权，参考百度智能云文档）"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise WebToolError("百度返回的不是 JSON，搜索未完成。") from exc
    if not isinstance(data, dict):
        raise WebToolError("百度返回格式异常，搜索未完成。")
    return _parse_baidu_items(data)


async def _search_bing(
    query: str, api_key: str, *, timeout: float, max_results: int
) -> list[WebHit]:
    """必应搜索：``GET https://api.bing.microsoft.com/v7.0/search?q=...&count=...``。"""
    url = "https://api.bing.microsoft.com/v7.0/search"
    async with _make_client(
        timeout=timeout,
        headers={"Ocp-Apim-Subscription-Key": api_key, "User-Agent": _UA},
    ) as client:
        resp = await client.get(url, params={"q": query, "count": max(1, int(max_results))})
    if resp.status_code in (401, 403):
        raise WebToolError("必应 API Key 无效或已过期，请到设置 → 联网检索里核对。")
    if resp.status_code >= 400:
        raise WebToolError(f"必应搜索失败：HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise WebToolError("必应返回的不是 JSON，搜索未完成。") from exc
    pages = (data.get("webPages") or {}).get("value") or []
    out: list[WebHit] = []
    for item in pages:
        if not isinstance(item, dict):
            continue
        link = str(item.get("url") or "").strip()
        if not link.startswith("http"):
            continue
        out.append(
            WebHit(
                title=str(item.get("name") or _title_from_url(link)).strip(),
                url=link,
                snippet=_clip(str(item.get("snippet") or ""), 400),
            )
        )
    return out


#: 服务商 id → 请求实现。免 Key 的 ``duckduckgo`` 少一个 ``api_key`` 位置参数，
#: 故 :func:`search_web` 分发时把它单列（其余五家签名一致，统一带 key）
_SEARCH_IMPLS = {
    "duckduckgo": _search_duckduckgo,
    "serper": _search_serper,
    "tavily": _search_tavily,
    "bocha": _search_bocha,
    "baidu": _search_baidu,
    "bing": _search_bing,
}


async def search_web(
    query: str,
    *,
    provider: str | None = None,
    api_key: str | None = None,
    max_results: int | None = None,
    timeout: float | None = None,
) -> list[WebHit]:
    """联网搜索，返回 :class:`WebHit` 列表（可能为空列表 = 真的没结果）。

    四个参数为 ``None`` 时全部从 :data:`app.core.config.settings` 读
    （``WEB_SEARCH_PROVIDER`` / ``WEB_SEARCH_API_KEY`` / ``WEB_MAX_RESULTS`` /
    ``WEB_FETCH_TIMEOUT``）；形参存在是为了让工具层与单测能显式覆盖，
    **不是为了在业务代码里各处传配置**。

    失败抛 :class:`WebToolError`（中文可读）——空列表才是「没搜到」，两者不可混淆。
    """
    query = (query or "").strip()
    if not query:
        return []

    channel = str(settings.WEB_CHANNEL or CHANNEL_API).strip().lower()
    if channel == CHANNEL_LLM:
        from app.agent.web.llm import search
        return await search(
            query,
            max_results=max(1, int(max_results if max_results is not None else settings.WEB_MAX_RESULTS)),
            timeout=float(timeout if timeout is not None else settings.WEB_FETCH_TIMEOUT),
        )

    wanted = provider if provider is not None else settings.WEB_SEARCH_PROVIDER
    provider_id = normalize_provider(wanted)
    spec = provider_spec(provider_id)
    if spec is None:
        # 不回落 duckduckgo：静默换服务商会让用户按错的方向排查（见 spec.normalize_provider）
        raise WebToolError(f"未登记的搜索服务商：{provider_id or '（空）'}，请到设置里重新选择。")

    key = str(api_key if api_key is not None else (settings.WEB_SEARCH_API_KEY or "")).strip()
    if spec.needs_key and not key:
        raise WebToolError(f"「{spec.label}」需要 API Key：{spec.key_hint}")

    limit = max(1, int(max_results if max_results is not None else settings.WEB_MAX_RESULTS))
    # 超时按「设置优先、空则用服务商登记的建议值」取（老基线同款口径）
    wait = float(
        timeout if timeout is not None else (settings.WEB_FETCH_TIMEOUT or spec.request_timeout)
    )

    impl = _SEARCH_IMPLS[provider_id]
    try:
        if provider_id == "duckduckgo":
            hits = await impl(query, timeout=wait)
        else:
            hits = await impl(query, key, timeout=wait, max_results=limit)
    except WebToolError:
        raise
    except httpx.TimeoutException as exc:
        # httpx 的 str() 常是空串：不在这里接住的话，上层只能给出「搜索失败：」这种没有原因的半句话
        raise WebToolError(
            f"搜索请求超时（{wait:.0f}s）：搜索引擎连不上。请稍后再试，或到设置里换一个搜索服务商。"
        ) from exc
    except httpx.HTTPError as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise WebToolError(
            f"搜索请求失败（网络错误）：{detail}。请检查本机网络，或到设置里换一个搜索服务商。"
        ) from exc

    return hits[:limit]


# --------------------------------------------------------------------------
# 抓取：URL → (标题, 正文)
# --------------------------------------------------------------------------


def _looks_like_html(content_type: str, body: str) -> bool:
    """判是不是可读网页：有 content-type 就信它；没有/是 text/plain 时看开头几个标签。

    很多站点不带 content-type 或写 ``text/plain``，一刀切拒掉会把能读的页面判成不可读。
    """
    ct = (content_type or "").lower()
    if "html" in ct or "xml" in ct:
        return True
    if not ct or ct.startswith("text/plain"):
        head = (body or "")[:200].lstrip().lower()
        return head.startswith("<!doctype") or head.startswith("<html") or "<body" in head[:500]
    return False


async def fetch_page(url: str, *, timeout: float | None = None) -> tuple[str, str]:
    """抓取网页并清洗为 ``(title, 正文)``；失败抛 :class:`WebToolError`（中文可读）。

    ``timeout=None`` 时取 ``settings.WEB_FETCH_TIMEOUT``。
    正文上限取 ``WEB_MAX_CONTENT``（下限 200：太小会把所有页面都判成「几乎没正文」）。
    🔴 `回归修复`：**流式读取**——headers 到手先做状态码四连拒 + content-length 预拒，
    body 按**字节上限**累积到量即断；不再「整页下载并解码进内存后才判大小」。
    """
    url = (url or "").strip()
    if not url.startswith("http"):
        raise WebToolError(f"不是合法的网页地址：{url[:80]}")
    wait = float(timeout if timeout is not None else settings.WEB_FETCH_TIMEOUT)
    # 🔴 `回归修复`：字节上限 = 字符上限 × 余量（常量注释见 `_FETCH_BYTES_HEADROOM`）。
    max_bytes = max(200, int(settings.WEB_MAX_CONTENT or 4000)) * _FETCH_BYTES_HEADROOM
    try:
        async with _make_client(
            timeout=wait,
            follow_redirects=True,
            headers={
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            },
        ) as client:
            # `回归修复`：`build_request + send(stream=True)` 取代整页 `client.get`；
            # 守卫 event_hooks 挂在 `_make_client` 上、照常逐跳触发，本处不碰。
            req = client.build_request("GET", url)
            resp = await client.send(req, stream=True)
            try:
                # 三种最常见的拒绝各有各的话说：用户看到「被反爬」和「不存在」的处置完全不同
                # （headers 已到手即判，不再等整页下载 —— 回归修复）
                if resp.status_code == 403:
                    raise WebToolError(f"对方拒绝访问（HTTP 403，可能是反爬）：{url}")
                if resp.status_code == 401:
                    raise WebToolError(f"需要登录才能查看（HTTP 401）：{url}")
                if resp.status_code == 404:
                    raise WebToolError(f"页面不存在（HTTP 404）：{url}")
                if resp.status_code >= 400:
                    raise WebToolError(f"抓取失败（HTTP {resp.status_code}）：{url}")
                # content-length 预拒（头存在且超字节上限 ⇒ 一个 body 字节都不读就拒）
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise WebToolError(
                        f"页面过大（content-length {int(declared)} 字节，超过上限 {max_bytes}）：{url}"
                    )
                chunks: list[bytes] = []
                received = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    received += len(chunk)
                    if received >= max_bytes:
                        break  # 🔴 字节上限即断：不再无限累积（回归修复 的本体）
                body_bytes = b"".join(chunks)
            finally:
                # stream=True 的响应**必须**显式关闭（四连拒/预拒/正常/异常所有出口路径都过这里）
                await resp.aclose()
    except httpx.TimeoutException as exc:
        raise WebToolError(f"抓取超时（{wait:.0f}s）：{url}") from exc
    except httpx.InvalidURL as exc:
        # `回归修复`：`httpx.InvalidURL` 的 MRO 是 `InvalidURL → Exception`，**不是** `HTTPError`
        # （实测 httpx 0.28.1）⇒ 不单独接住的话它会穿到工具层，用户看到
        # `Invalid IPv4 address: '0177.0.0.1'` 这种英文内部串。判据腿见 tests/api/test_web_ssrf_r2.py。
        raise WebToolError(f"不是合法的网页地址：{url[:80]}") from exc
    except httpx.HTTPError as exc:
        raise WebToolError(f"抓取失败（网络错误）：{url}") from exc

    # 🔴 流式半读后 `resp.text` 不可用（httpx 语义：流式响应只允许消费一次流）
    # ⇒ 自行解码（`errors="replace"` 兜住字节上限截断处的半个多字节字符）。
    content_type = resp.headers.get("content-type", "")
    body = body_bytes.decode(resp.encoding or "utf-8", errors="replace")
    if "application/pdf" in content_type.lower():
        raise WebToolError(f"这是 PDF 链接，本工具只抓网页正文：{url}")
    if not _looks_like_html(content_type, body):
        raise WebToolError(f"不是可读的网页（content-type: {content_type or '未知'}）：{url}")

    title, text = html_to_text(body)
    title = title or _title_from_url(url)
    text = _clip(text, max(200, int(settings.WEB_MAX_CONTENT or 4000)))
    if len(text) < 40:
        # 40 字以下基本是「全是脚本的空壳」或反爬壳；放进去只会污染资料区与引用编号
        raise WebToolError(f"页面几乎没有可读正文（可能被反爬或全是脚本）：{url}")
    return title, text


# --------------------------------------------------------------------------
# 网页 → 可引用材料
# --------------------------------------------------------------------------


def web_material(
    title: str, url: str, text: str, *, snippet: str = "", score: float = 0.55
) -> Material:
    """网页 → 可引用材料（``Material`` 带 ``url``，见 `接口冻结 §七`）。

    两个非显然的决定：

    1. ``chunk_id`` 用共享助手 :func:`app.retrieval.types.web_chunk_id`（sha1(url) 派生的
       **负数**），而不是 ``hash(url)`` 或自增序号：
       - 网页材料没有 ``chunks`` 行，而文档材料的 ``chunk_id`` **就是** ``chunks.id``
         （正整数自增）⇒ 负数保证**永不与真实分块号相撞**，否则引用归因会把网页
         算到某个真实分块头上；
       - ``hash()`` 带进程级随机盐（``PYTHONHASHSEED``），重启就变号 ⇒ 引用编号在
         重启前后漂移、``chunk_key`` 去重失效。sha1 跨进程稳定（老基线
         ``web_tools._chunk_id`` 也用 sha1）。
       与 ``Material.chunk_key``（``web:<sha1[:16]>``）**同源同文件**：两种身份来自同一处，
       不会出现「去重按一个公式、归因按另一个公式」的静默漂移。
    2. ``snippet`` 非空且不在正文里时拼在正文前（老基线 ``web_retrieve`` 同款）：
       抓取失败退回摘要时它就是全部内容；抓取成功时它常含正文没有的关键句。

    ``score`` 默认 0.55：网页不是本机权威资料，略低于库内主命中（老基线同值）。
    """
    body = str(text or "")
    brief = str(snippet or "").strip()
    if brief and brief not in body:
        body = f"{brief}\n\n{body}" if body.strip() else brief
    return Material(
        chunk_id=web_chunk_id(url),
        document_id=0,
        content=body,
        filename=title,
        kb_id=1,
        block_ordinal=0,
        score=score,
        url=url,  # 接口冻结 §七 的只增字段：文档材料恒 None，网页材料必须带
    )


__all__ = [
    "WebHit",
    "WebToolError",
    "fetch_page",
    "html_to_text",
    "search_web",
    "web_material",
]
