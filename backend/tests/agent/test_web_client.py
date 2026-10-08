"""``app/agent/web`` 单测（阶段 4 · 条目 4.1）：纯函数 / 六家服务商 / 抓取 / 材料。

**绝不真出网**：``httpx.AsyncClient`` 一律换成「回放脚本里的响应或异常、并记录请求」
的替身（:func:`_install_httpx`），所以这些用例在断网机器上也必须全绿——
工具层一旦偷偷出网，这里就是第一道红线（老仓库踩过「测试静默真出网」的坑）。
"""
from __future__ import annotations

import hashlib

import httpx
import pytest

from app.agent.web import client as web_client
from app.agent.web import spec as web_spec
from app.agent.web.client import (
    WebHit,
    WebToolError,
    fetch_page,
    html_to_text,
    search_web,
    web_material,
)
from app.agent.web.spec import CHANNELS, CHANNEL_API, CHANNEL_LLM, normalize_provider, provider_spec
from app.core.config import settings
from app.retrieval import types as web_types
from app.retrieval.types import Material

# --------------------------------------------------------------------------
# 假 httpx（测试不出网的唯一手段）
# --------------------------------------------------------------------------


class _FakeResponse:
    """回放用响应：只实现被测代码真正用到的形状。

    🔴 `回归修复` 起 `fetch_page` 改走 ``build_request + send(stream=True)``
    ⇒ 本替身补齐流式三件（``aiter_bytes`` / ``aclose`` / ``encoding``），并带
    ``served_bytes`` 计数（载体用它断言「实际只读到字节上限附近」）；
    ``get``/``post`` 直取 ``.text`` 的既有路径一字不动。
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        json_body: dict | None = None,
        content_type: str = "text/html; charset=utf-8",
        content_length: int | None = None,
        chunk_size: int = 0,
    ) -> None:
        self.status_code = status_code
        # 🔴 text 是**计数属性**：修前 `client.get` 整页路径只消费 `.text`（不经
        # aiter_bytes）⇒ 两条路径统一用 `served_bytes` 这把尺度量「实际被消费的字节」，
        # 载体的字节断言对修前修后都有判别力（新流式路径在 aiter_bytes 里计）。
        self._text = text
        self._text_served = False
        self.headers = {"content-type": content_type}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self._json_body = json_body
        self._chunk_size = chunk_size
        self.served_bytes = 0
        self.closed = False

    @property
    def text(self) -> str:
        if not self._text_served:
            self._text_served = True
            self.served_bytes += len(self._text.encode("utf-8"))
        return self._text

    def json(self) -> dict:
        if self._json_body is None:
            raise ValueError("No JSON")
        return self._json_body

    @property
    def encoding(self):
        # 真 httpx 会从 content-type 解析 charset；替身统一交 None ⇒ 产品侧回落 utf-8
        # （本替身的 text 恒按 utf-8 编码，语义一致）。
        return None

    async def aiter_bytes(self):
        data = self._text.encode("utf-8")
        if self._chunk_size <= 0:
            self.served_bytes += len(data)
            yield data
            return
        for start in range(0, len(data), self._chunk_size):
            chunk = data[start : start + self._chunk_size]
            self.served_bytes += len(chunk)
            yield chunk

    async def aclose(self):
        self.closed = True


class _FakeRequest:
    """``build_request`` 的产物：只带 ``send`` 需要的三样（method / url / kwargs）。"""

    def __init__(self, method: str, url: str, kwargs: dict) -> None:
        self.method = method
        self.url = url
        self.kwargs = kwargs


class _FakeAsyncClient:
    """``httpx.AsyncClient`` 替身：按脚本回放，记录每次请求（含构造参数）。

    🔴 `回归修复`：补 ``build_request`` + ``send(req, stream=True)``（回放同一份
    脚本、记录形状与 ``get``/``post`` 逐字同形 —— 既有 14 处 opts 记录断言逐键读，
    不得变形）。
    """

    def __init__(self, script: list, log: list, **opts) -> None:
        self._script = script
        self._log = log
        self.opts = opts

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def _respond(self, method: str, url: str, **kwargs):
        self._log.append({"method": method, "url": url, "opts": self.opts, **kwargs})
        item = self._script.pop(0) if self._script else _FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item

    def build_request(self, method: str, url: str, **kwargs):
        return _FakeRequest(method, url, kwargs)

    async def send(self, request, *, stream: bool = False):  # noqa: ARG002 —— 替身不分流
        return await self._respond(request.method, request.url, **request.kwargs)

    async def get(self, url: str, **kwargs):
        return await self._respond("GET", url, **kwargs)

    async def post(self, url: str, **kwargs):
        return await self._respond("POST", url, **kwargs)


def _install_httpx(monkeypatch, *script) -> list[dict]:
    """装替身并返回请求记录；``script`` 每项是 :class:`_FakeResponse` 或待抛异常实例。"""
    log: list[dict] = []
    items = list(script)

    def factory(**opts):
        return _FakeAsyncClient(items, log, **opts)

    monkeypatch.setattr(web_client.httpx, "AsyncClient", factory)
    return log


@pytest.fixture(autouse=True)
def _neutral_settings(monkeypatch):
    """把联网相关配置钉成确定值：用例只测「参数/配置怎么被用」，不测本机 .env 是什么。"""
    monkeypatch.setattr(settings, "WEB_CHANNEL", CHANNEL_API)
    monkeypatch.setattr(settings, "WEB_SEARCH_PROVIDER", "duckduckgo")
    monkeypatch.setattr(settings, "WEB_SEARCH_API_KEY", "")
    monkeypatch.setattr(settings, "WEB_MAX_RESULTS", 4)
    monkeypatch.setattr(settings, "WEB_FETCH_TIMEOUT", 12.0)
    monkeypatch.setattr(settings, "WEB_MAX_CONTENT", 4000)


# --------------------------------------------------------------------------
# 服务商登记表
# --------------------------------------------------------------------------


def test_provider_table_covers_six_api_providers():
    ids = [item.id for item in web_spec.WEB_PROVIDERS]
    assert ids == ["duckduckgo", "serper", "tavily", "bocha", "baidu", "bing"]
    # 免 Key 的只有 duckduckgo（其余五家缺 Key 必须报错，不能空着发请求）
    assert [item.id for item in web_spec.WEB_PROVIDERS if not item.needs_key] == ["duckduckgo"]
    for item in web_spec.WEB_PROVIDERS:
        assert item.label and item.summary and item.base_url_hint and item.request_timeout > 0
        assert bool(item.key_hint) is bool(item.needs_key)


def test_channels_cover_api_and_llm():
    assert CHANNEL_API == "api" and CHANNEL_LLM == "llm"
    assert [item["id"] for item in CHANNELS] == [CHANNEL_API, CHANNEL_LLM]


def test_normalize_provider_maps_alias_and_keeps_unknown_readable():
    assert normalize_provider("DDG") == "duckduckgo"
    assert normalize_provider(" google ") == "serper"
    assert normalize_provider("bocha") == "bocha"
    # 未登记：原样小写返回，交给调用方报可读错误（**不**静默回落 duckduckgo）
    assert normalize_provider(" Nope ") == "nope"
    assert normalize_provider(None) == ""
    assert provider_spec("ddg") is not None and provider_spec("nope") is None


# --------------------------------------------------------------------------
# html_to_text（纯函数）
# --------------------------------------------------------------------------


def test_html_to_text_keeps_title_and_drops_noise():
    html = """
    <html><head><title>  知澜 · 示例  </title>
      <style>body{color:red}</style>
      <script>var a = 1;</script>
    </head>
    <body>
      <nav>导航项</nav>
      <h1>大标题</h1>
      <p>第一段   正文</p>
      <div>第二段</div>
      <footer>页脚版权</footer>
    </body></html>
    """
    title, text = html_to_text(html)
    assert title == "知澜 · 示例"
    assert "大标题" in text and "第一段 正文" in text and "第二段" in text
    # 噪声整棵丢：脚本/样式/导航/页脚都不该进正文（否则摘要检索全是垃圾）
    for noise in ("var a = 1", "color:red", "导航项", "页脚版权"):
        assert noise not in text
    assert text == "大标题\n第一段 正文\n第二段"


def test_html_to_text_is_pure_and_tolerates_junk():
    html = "<p>甲乙</p><p>丙丁</p>"
    first = html_to_text(html)
    assert first == html_to_text(html)  # 纯函数：同输入同输出
    assert html_to_text("") == ("", "")
    assert html_to_text("不是 HTML 的纯文本") == ("", "不是 HTML 的纯文本")


def test_html_to_text_collapses_ideographic_and_nbsp_spaces():
    _title, text = html_to_text("<p>甲\u3000\u3000乙&nbsp;丙</p>")
    assert text == "甲 乙 丙"


# --------------------------------------------------------------------------
# search_web
# --------------------------------------------------------------------------

_DDG_HTML = """
<html><head><title>示例搜索页</title></head><body>
<div class="result results_links results_links_deep web-result">
  <a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Falpha&amp;rut=x">Alpha 标题</a>
  <a class="result__snippet" href="#">Alpha 摘要</a>
</div>
<div class="result results_links web-result">
  <a rel="nofollow" class="result__a" href="https://beta.example.com/page">Beta 标题</a>
  <td class="result__snippet">Beta 摘要</td>
</div>
</body></html>
"""


@pytest.mark.asyncio
async def test_search_web_duckduckgo_parses_html_results(monkeypatch):
    log = _install_httpx(monkeypatch, _FakeResponse(text=_DDG_HTML))
    hits = await search_web("知澜 是什么", provider="ddg", max_results=4, timeout=5)

    assert [(h.title, h.url, h.snippet) for h in hits] == [
        ("Alpha 标题", "https://example.com/alpha", "Alpha 摘要"),
        ("Beta 标题", "https://beta.example.com/page", "Beta 摘要"),
    ]
    assert all(isinstance(h, WebHit) for h in hits)
    call = log[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://html.duckduckgo.com/html/"  # 免 Key 的 HTML 端点
    assert call["data"] == {"q": "知澜 是什么", "b": ""}
    assert call["opts"]["timeout"] == 5
    assert call["opts"]["follow_redirects"] is True


@pytest.mark.asyncio
async def test_search_web_duckduckgo_reports_challenge_page(monkeypatch):
    """反爬页返回 200：必须报错而不是静默变成「没有结果」（用户会以为真搜不到）。"""
    _install_httpx(monkeypatch, _FakeResponse(text="<html>anomaly challenge</html>"))
    with pytest.raises(WebToolError) as excinfo:
        await search_web("q", provider="duckduckgo")
    assert "反爬验证页" in str(excinfo.value)


@pytest.mark.asyncio
async def test_search_web_duckduckgo_reports_http_status(monkeypatch):
    _install_httpx(monkeypatch, _FakeResponse(status_code=503, text="busy"))
    with pytest.raises(WebToolError) as excinfo:
        await search_web("q", provider="duckduckgo")
    assert "HTTP 503" in str(excinfo.value)


@pytest.mark.asyncio
async def test_search_web_serper_request_shape_and_parsing(monkeypatch):
    log = _install_httpx(
        monkeypatch,
        _FakeResponse(
            json_body={
                "organic": [
                    {"title": "标题", "link": "https://example.com/1", "snippet": "摘要"},
                    {"title": "", "link": "https://example.com/2", "snippet": ""},
                    {"title": "坏链接", "link": "ftp://x", "snippet": "跳过"},
                    "不是字典",
                ]
            }
        ),
    )
    hits = await search_web("问题", provider="serper", api_key="K1", max_results=2, timeout=6)

    assert [(h.title, h.url) for h in hits] == [
        ("标题", "https://example.com/1"),
        ("2", "https://example.com/2"),  # 无标题时从 URL 末段猜
    ]
    call = log[0]
    assert call["method"] == "POST" and call["url"] == "https://google.serper.dev/search"
    assert call["opts"]["headers"]["X-API-KEY"] == "K1"
    assert call["json"] == {"q": "问题", "num": 3}  # 老基线：至少取 3 条
    assert call["opts"]["timeout"] == 6


@pytest.mark.asyncio
async def test_search_web_tavily_request_shape_and_parsing(monkeypatch):
    log = _install_httpx(
        monkeypatch,
        _FakeResponse(
            json_body={
                "results": [
                    {"title": "T", "url": "https://e.com/t", "content": "正文摘录"},
                    {"title": "S", "url": "not-a-url", "content": "跳过"},
                ]
            }
        ),
    )
    hits = await search_web("q", provider="tavily", api_key="K2", max_results=5)

    assert [(h.title, h.url, h.snippet) for h in hits] == [("T", "https://e.com/t", "正文摘录")]
    call = log[0]
    assert call["url"] == "https://api.tavily.com/search"
    assert call["json"] == {
        "api_key": "K2",
        "query": "q",
        "max_results": 5,
        "include_raw_content": False,
    }


@pytest.mark.asyncio
async def test_search_web_bocha_request_shape_and_parsing(monkeypatch):
    log = _install_httpx(
        monkeypatch,
        _FakeResponse(
            json_body={
                "data": {
                    "webPages": {
                        "value": [{"name": "博查标题", "url": "https://b.com/1", "snippet": "摘要"}]
                    }
                }
            }
        ),
    )
    hits = await search_web("q", provider="bocha", api_key="K3", max_results=3)

    assert [(h.title, h.url, h.snippet) for h in hits] == [("博查标题", "https://b.com/1", "摘要")]
    call = log[0]
    assert call["url"] == "https://api.bochaai.com/v1/web-search"
    assert call["opts"]["headers"]["Authorization"] == "Bearer K3"
    assert call["json"] == {"query": "q", "summary": True, "count": 3}


@pytest.mark.asyncio
async def test_search_web_baidu_parses_three_shapes_and_dedups(monkeypatch):
    """百度返回结构各地不一：三种候选位置都要认，且同一 URL 去重。"""
    log = _install_httpx(
        monkeypatch,
        _FakeResponse(
            json_body={
                "webPages": {"value": [{"title": "A", "url": "https://a.com/1", "summary": "s1"}]},
                "result": {"list": [{"name": "B", "href": "https://a.com/2", "abstract": "s2"}]},
                "items": [{"title": "重复", "url": "https://a.com/1", "snippet": "dup"}],
            }
        ),
    )
    hits = await search_web("q", provider="baidu", api_key="K4", max_results=4)

    assert [(h.title, h.url, h.snippet) for h in hits] == [
        ("A", "https://a.com/1", "s1"),
        ("B", "https://a.com/2", "s2"),
    ]
    call = log[0]
    assert call["url"] == "https://aip.baidubce.com/rest/2.0/search/v1/search"
    assert call["opts"]["headers"]["Authorization"] == "Bearer K4"
    assert call["opts"]["headers"]["X-API-KEY"] == "K4"  # 两种鉴权头都带，兼容官方口径变更
    assert call["json"] == {"query": "q", "count": 4}


@pytest.mark.asyncio
async def test_search_web_bing_uses_get_with_query_params(monkeypatch):
    log = _install_httpx(
        monkeypatch,
        _FakeResponse(
            json_body={"webPages": {"value": [{"name": "Bing 标题", "url": "https://c.com/1", "snippet": "s"}]}}
        ),
    )
    hits = await search_web("q", provider="bing", api_key="K5", max_results=2)

    assert [h.title for h in hits] == ["Bing 标题"]
    call = log[0]
    assert call["method"] == "GET" and call["url"] == "https://api.bing.microsoft.com/v7.0/search"
    assert call["opts"]["headers"]["Ocp-Apim-Subscription-Key"] == "K5"
    assert call["params"] == {"q": "q", "count": 2}


@pytest.mark.asyncio
async def test_search_web_reads_settings_when_args_are_none(monkeypatch):
    monkeypatch.setattr(settings, "WEB_SEARCH_PROVIDER", "bing")
    monkeypatch.setattr(settings, "WEB_SEARCH_API_KEY", "from-settings")
    monkeypatch.setattr(settings, "WEB_MAX_RESULTS", 2)
    monkeypatch.setattr(settings, "WEB_FETCH_TIMEOUT", 7.0)
    log = _install_httpx(monkeypatch, _FakeResponse(json_body={"webPages": {"value": []}}))

    await search_web("q")

    call = log[0]
    assert call["opts"]["timeout"] == 7.0
    assert call["params"]["count"] == 2
    assert call["opts"]["headers"]["Ocp-Apim-Subscription-Key"] == "from-settings"


@pytest.mark.asyncio
async def test_search_web_truncates_to_max_results(monkeypatch):
    _install_httpx(monkeypatch, _FakeResponse(text=_DDG_HTML))
    hits = await search_web("q", provider="duckduckgo", max_results=1)
    assert [h.title for h in hits] == ["Alpha 标题"]


@pytest.mark.asyncio
async def test_search_web_empty_query_returns_empty_without_request(monkeypatch):
    log = _install_httpx(monkeypatch)
    assert await search_web("   ") == []
    assert log == []  # 空查询连请求都不发


@pytest.mark.asyncio
async def test_search_web_llm_channel_rejects_unsupported_provider(monkeypatch):
    monkeypatch.setattr(settings, "WEB_CHANNEL", CHANNEL_LLM)
    monkeypatch.setattr(settings, "WEB_LLM_PROVIDER", "deepseek")
    log = _install_httpx(monkeypatch)
    with pytest.raises(WebToolError) as excinfo:
        await search_web("q", provider="duckduckgo")
    assert "目前支持 MiMo" in str(excinfo.value)
    assert log == []


@pytest.mark.asyncio
async def test_search_web_missing_api_key_names_the_provider(monkeypatch):
    log = _install_httpx(monkeypatch)
    with pytest.raises(WebToolError) as excinfo:
        await search_web("q", provider="serper", api_key="  ")
    assert str(excinfo.value) == "「Serper.dev（Google）」需要 API Key：在 https://serper.dev 注册后拿到的 X-API-KEY"
    assert log == []  # 缺 Key 不发请求


@pytest.mark.asyncio
async def test_search_web_unknown_provider_does_not_fall_back(monkeypatch):
    log = _install_httpx(monkeypatch)
    with pytest.raises(WebToolError) as excinfo:
        await search_web("q", provider="  Fancy ")
    assert "未登记的搜索服务商：fancy" in str(excinfo.value)
    assert log == []


@pytest.mark.asyncio
async def test_search_web_timeout_becomes_readable_error(monkeypatch):
    _install_httpx(monkeypatch, httpx.TimeoutException("timed out"))
    with pytest.raises(WebToolError) as excinfo:
        await search_web("q", provider="duckduckgo", timeout=12)
    assert "搜索请求超时（12s）：搜索引擎连不上。" in str(excinfo.value)


@pytest.mark.asyncio
async def test_search_web_network_error_becomes_readable_error(monkeypatch):
    _install_httpx(monkeypatch, httpx.ConnectError(""))
    with pytest.raises(WebToolError) as excinfo:
        await search_web("q", provider="duckduckgo")
    # httpx 的 str() 常为空：至少要给出异常类型名，不能是「搜索请求失败（网络错误）：」
    assert "搜索请求失败（网络错误）：ConnectError" in str(excinfo.value)


@pytest.mark.asyncio
async def test_search_web_provider_error_body_is_not_json(monkeypatch):
    _install_httpx(monkeypatch, _FakeResponse(status_code=200, text="<html>oops</html>"))
    with pytest.raises(WebToolError) as excinfo:
        await search_web("q", provider="tavily", api_key="K")
    assert str(excinfo.value) == "Tavily 返回的不是 JSON，搜索未完成。"


@pytest.mark.asyncio
async def test_search_web_key_rejected_message(monkeypatch):
    _install_httpx(monkeypatch, _FakeResponse(status_code=401, text=""))
    with pytest.raises(WebToolError) as excinfo:
        await search_web("q", provider="bing", api_key="bad")
    assert str(excinfo.value) == "必应 API Key 无效或已过期，请到设置 → 联网检索里核对。"


@pytest.mark.asyncio
async def test_search_web_uses_provider_timeout_when_settings_timeout_empty(monkeypatch):
    """``WEB_FETCH_TIMEOUT`` 为空/0 时回落服务商登记的建议超时（否则会「立刻超时」）。"""
    monkeypatch.setattr(settings, "WEB_FETCH_TIMEOUT", 0.0)
    log = _install_httpx(monkeypatch, _FakeResponse(text=_DDG_HTML))
    await search_web("q", provider="duckduckgo")
    assert log[0]["opts"]["timeout"] == 12.0


# --------------------------------------------------------------------------
# fetch_page
# --------------------------------------------------------------------------

_PAGE_HTML = (
    "<html><head><title>示例页</title></head><body><p>"
    + "正文内容" * 30
    + "</p></body></html>"
)


@pytest.mark.asyncio
async def test_fetch_page_ok(monkeypatch):
    log = _install_httpx(monkeypatch, _FakeResponse(text=_PAGE_HTML))
    title, text = await fetch_page("https://example.com/a", timeout=3)

    assert title == "示例页"
    assert text == "正文内容" * 30
    call = log[0]
    assert call["method"] == "GET" and call["url"] == "https://example.com/a"
    assert call["opts"]["timeout"] == 3
    assert call["opts"]["follow_redirects"] is True
    assert call["opts"]["headers"]["User-Agent"].startswith("Mozilla/5.0")


@pytest.mark.asyncio
async def test_fetch_page_title_falls_back_to_url(monkeypatch):
    _install_httpx(monkeypatch, _FakeResponse(text="<html><body>" + "无标题正文" * 20 + "</body></html>"))
    title, _text = await fetch_page("https://example.com/docs/report.pdf.html")
    assert title == "report.pdf.html"


@pytest.mark.asyncio
async def test_fetch_page_rejects_non_http_url_without_request(monkeypatch):
    log = _install_httpx(monkeypatch)
    with pytest.raises(WebToolError) as excinfo:
        await fetch_page("ftp://example.com/a")
    assert "不是合法的网页地址" in str(excinfo.value)
    assert log == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, "对方拒绝访问（HTTP 403，可能是反爬）："),
        (401, "需要登录才能查看（HTTP 401）："),
        (404, "页面不存在（HTTP 404）："),
        (500, "抓取失败（HTTP 500）："),
    ],
)
@pytest.mark.asyncio
async def test_fetch_page_status_messages(monkeypatch, status, expected):
    _install_httpx(monkeypatch, _FakeResponse(status_code=status, text=""))
    with pytest.raises(WebToolError) as excinfo:
        await fetch_page("https://example.com/x")
    assert str(excinfo.value) == f"{expected}https://example.com/x"


@pytest.mark.asyncio
async def test_fetch_page_pdf_is_rejected(monkeypatch):
    _install_httpx(
        monkeypatch,
        _FakeResponse(content_type="application/pdf", text="%PDF-1.4"),
    )
    with pytest.raises(WebToolError) as excinfo:
        await fetch_page("https://example.com/x.pdf")
    assert str(excinfo.value) == "这是 PDF 链接，本工具只抓网页正文：https://example.com/x.pdf"


@pytest.mark.asyncio
async def test_fetch_page_non_html_is_rejected(monkeypatch):
    _install_httpx(
        monkeypatch,
        _FakeResponse(content_type="application/json", text='{"a": 1}'),
    )
    with pytest.raises(WebToolError) as excinfo:
        await fetch_page("https://example.com/api")
    assert str(excinfo.value) == "不是可读的网页（content-type: application/json）：https://example.com/api"


@pytest.mark.asyncio
async def test_fetch_page_plain_text_sniffed_as_html(monkeypatch):
    """``content-type: text/plain`` 但正文是 HTML：靠开头几个标签认出来（别一刀切拒掉）。"""
    _install_httpx(
        monkeypatch,
        _FakeResponse(content_type="text/plain", text="<!DOCTYPE html><html><body>" + "字" * 80),
    )
    title, text = await fetch_page("https://example.com/a")
    assert title == "a" and len(text) >= 40


@pytest.mark.asyncio
async def test_fetch_page_almost_empty_body_is_rejected(monkeypatch):
    _install_httpx(monkeypatch, _FakeResponse(text="<html><body><p>短</p></body></html>"))
    with pytest.raises(WebToolError) as excinfo:
        await fetch_page("https://example.com/shell")
    assert str(excinfo.value) == "页面几乎没有可读正文（可能被反爬或全是脚本）：https://example.com/shell"


@pytest.mark.asyncio
async def test_fetch_page_timeout_and_network_errors(monkeypatch):
    _install_httpx(monkeypatch, httpx.TimeoutException("t"))
    with pytest.raises(WebToolError) as excinfo:
        await fetch_page("https://example.com/a", timeout=5)
    assert str(excinfo.value) == "抓取超时（5s）：https://example.com/a"

    _install_httpx(monkeypatch, httpx.ConnectError("nope"))
    with pytest.raises(WebToolError) as excinfo:
        await fetch_page("https://example.com/a")
    assert str(excinfo.value) == "抓取失败（网络错误）：https://example.com/a"


@pytest.mark.asyncio
async def test_fetch_page_clips_long_body(monkeypatch):
    monkeypatch.setattr(settings, "WEB_MAX_CONTENT", 200)
    _install_httpx(monkeypatch, _FakeResponse(text="<html><body><p>" + "长" * 5000 + "</p></body></html>"))
    _title, text = await fetch_page("https://example.com/a")
    assert len(text) == 201 and text.endswith("…")  # 200 字 + 省略号


# --------------------------------------------------------------------------
# web_material
# --------------------------------------------------------------------------


def test_web_material_carries_url_and_frozen_fields():
    material = web_material("标题", "https://example.com/a", "正文")

    assert material.url == "https://example.com/a"
    assert material.filename == "标题"
    assert material.content == "正文"
    assert material.document_id == 0 and material.kb_id == 1 and material.block_ordinal == 0
    assert material.score == 0.55
    assert material.chunk_id < 0


def test_web_material_chunk_id_is_stable_across_processes():
    """``chunk_id`` 必须由 sha1 派生：``hash()`` 带进程盐，重启就漂（引用编号会对不上）。"""
    url = "https://example.com/a"
    expected = -int(hashlib.sha1(url.encode("utf-8")).hexdigest()[:8], 16)

    assert web_material("t", url, "x").chunk_id == expected
    assert web_material("t", url, "x").chunk_id == expected  # 同 URL 同号
    assert web_material("t", "https://example.com/b", "x").chunk_id != expected


def test_web_material_is_recognised_as_web_source():
    """接口冻结 §七 的派生口径：``url`` 非空 ⇒ ``source_type=web``、``chunk_key`` 走 ``web:``。

    ``chunk_key`` 是运行器累积材料的去重键 ⇒ 错成文档形态会把不同网页合成一条。
    同时钉住「文档材料不受影响」（只增不改义的硬要求）。
    """
    material = web_material("t", "https://example.com/a", "x")

    assert material.is_web is True
    assert material.source_type == "web"
    assert material.chunk_key == web_types.web_chunk_key("https://example.com/a")
    assert material.chunk_key.startswith("web:")
    assert material.chunk_key == web_material("t", "https://example.com/a", "y").chunk_key

    doc = Material(chunk_id=7, document_id=3, content="x", block_ordinal=2)
    assert doc.source_type == "document" and doc.chunk_key == "kb:1:document:3:2"


def test_web_material_prepends_snippet_only_when_missing():
    with_snippet = web_material("t", "https://e.com/1", "正文段落", snippet="摘要句")
    assert with_snippet.content == "摘要句\n\n正文段落"

    # 摘要已在正文里 ⇒ 不重复拼（否则资料区里同句话出现两遍，白占上下文）
    already = web_material("t", "https://e.com/2", "开头就是摘要句，后面还有正文", snippet="摘要句")
    assert already.content == "开头就是摘要句，后面还有正文"

    # 抓取失败退回摘要：正文就是摘要本身 ⇒ 也不该拼成两遍
    fallback = web_material("t", "https://e.com/3", "只有摘要", snippet="只有摘要")
    assert fallback.content == "只有摘要"


def test_web_material_accepts_custom_score():
    assert web_material("t", "https://e.com/1", "x", score=0.9).score == 0.9


# --------------------------------------------------------------------------
# 回归修复：fetch_page 整页下载后才判大小 ⇒ 流式读取 + 字节上限 + content-length 预拒
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_page_huge_body_is_capped_streaming(monkeypatch):
    """**主腿**：超大 body 在**字节上限**处断流——实际读取字节数停在上限附近。

    修前：``client.get`` 整页进内存，替身的 body 会被**全量**消费（served == 全量）；
    修后：``aiter_bytes`` 累积到字节上限（200×6=1200）+ 一个 chunk 内即断，
    返回正文仍受 `_clip` 字符约束、尾部哨兵永远读不到。
    """
    monkeypatch.setattr(settings, "WEB_MAX_CONTENT", 200)
    body = "x" * 20000 + "SENTINEL-TAIL"
    resp = _FakeResponse(text=body, chunk_size=256)
    _install_httpx(monkeypatch, resp)
    _title, text = await fetch_page("https://example.com/huge")
    assert len(text) <= 201 and text.endswith("…"), f"正文仍受字符上限约束：len={len(text)}"
    assert "SENTINEL-TAIL" not in text, "尾部哨兵漏进正文 ⇒ 截断失效"
    # 🔴 判别力在本条：实际读取字节数 ≤ 上限 + 一个 chunk（修前 = 全量 20012 字节）
    assert resp.served_bytes <= 200 * 6 + 256, (
        f"实际读取 {resp.served_bytes} 字节 ⇒ 没有在字节上限断流（整页还在进内存）"
    )
    assert resp.served_bytes < len(body.encode("utf-8")), "读取量竟等于全量 ⇒ 未断流"
    assert resp.closed, "流式响应未被 aclose ⇒ 连接泄漏"


@pytest.mark.asyncio
async def test_fetch_page_content_length_over_cap_is_pre_rejected(monkeypatch):
    """**预拒腿**：content-length 头超字节上限 ⇒ 一个 body 字节都不读就拒。

    修前：无此判（先整页下载后才走拒绝链）；修后：headers 到手即拒，
    ``served_bytes == 0`` 证明 body 消费量为零。
    """
    monkeypatch.setattr(settings, "WEB_MAX_CONTENT", 200)
    resp = _FakeResponse(text="x" * 50000, content_length=999_999, chunk_size=256)
    _install_httpx(monkeypatch, resp)
    with pytest.raises(WebToolError) as excinfo:
        await fetch_page("https://example.com/too-big")
    assert "页面过大" in str(excinfo.value), f"预拒文案不符：{excinfo.value}"
    assert resp.served_bytes == 0, f"body 被消费了 {resp.served_bytes} 字节 ⇒ 不是预拒"
    assert resp.closed, "拒绝路径也必须 aclose"
