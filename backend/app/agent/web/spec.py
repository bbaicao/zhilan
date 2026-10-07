"""联网搜索服务商登记表（阶段 4 · 条目 4.1；数据表，端口自老基线 ``app/rag/web_spec.py``）。

**为什么单独成模块**：服务商清单是「设置页 ↔ 工具层」共用的**唯一真相源**——
界面照这张表渲染「是否需要 Key / Key 提示」，:mod:`app.agent.web.client` 照它取
鉴权口径与端点提示。四处硬编码服务商文案，迟早出现「界面说要填 A，工具却说 B」。

**本表只登记 ``api`` 渠道**（传统搜索 API）的六家：``duckduckgo``（免 Key）/ ``serper`` /
``tavily`` / ``bocha`` / ``baidu`` / ``bing``。``llm`` 渠道（大模型搜索）在条目 4.1
**有意不实装**（见 `接口冻结 §十一` 的偏差登记 2），故这里只留渠道描述 :data:`CHANNELS`
给设置页显示、**不登记大模型服务商**——登记了就会有人以为能选。

字段刻意只有八项（老基线里的 ``cost`` / ``note`` / ``source`` 未搬）：本条目的读者只有
工具层（要 ``label`` / ``needs_key`` / ``key_hint`` / ``request_timeout``）与后续的设置页；
没有读者的字段搬过来只会腐烂，等设置域（4.5.3）真要渲染计费说明时再补。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WebProvider:
    """一条联网搜索服务商（注册表的一行数据，不含行为）。"""

    id: str
    label: str
    aliases: tuple[str, ...] = ()
    summary: str = ""
    needs_key: bool = True
    #: 缺 Key 时给用户看的一句话（照它到哪去拿）；``needs_key=False`` 时为空
    key_hint: str = ""
    #: 端点提示（设置页展示用；真端点写死在 :mod:`app.agent.web.client` 的请求分支里）
    base_url_hint: str = ""
    #: 该服务商的建议超时（秒）：``WEB_FETCH_TIMEOUT`` 为空/0 时回落它
    request_timeout: float = 12.0


WEB_PROVIDERS: tuple[WebProvider, ...] = (
    WebProvider(
        id="duckduckgo",
        label="DuckDuckGo（无需 Key）",
        aliases=("ddg", "duck"),
        summary="抓取 DuckDuckGo 的 HTML 结果页，不需要注册、不需要 API Key。",
        needs_key=False,
        key_hint="",
        base_url_hint="https://html.duckduckgo.com/html/",
        request_timeout=12.0,
    ),
    WebProvider(
        id="serper",
        label="Serper.dev（Google）",
        aliases=("google", "serp"),
        summary="调用 Serper.dev 的 Google 搜索 API，结果稳定、含标题/链接/摘要。",
        needs_key=True,
        key_hint="在 https://serper.dev 注册后拿到的 X-API-KEY",
        base_url_hint="https://google.serper.dev/search",
        request_timeout=12.0,
    ),
    WebProvider(
        id="tavily",
        label="Tavily（面向 RAG）",
        aliases=(),
        summary="Tavily 搜索 API，结果自带正文摘录，可少抓几次原网页。",
        needs_key=True,
        key_hint="在 https://tavily.com 注册后拿到的 API Key",
        base_url_hint="https://api.tavily.com/search",
        request_timeout=12.0,
    ),
    WebProvider(
        id="bocha",
        label="博查 Bocha",
        aliases=("bc",),
        summary="博查搜索 API，中文结果友好，国内 AI 应用里常见。",
        needs_key=True,
        key_hint="在 https://open.bochaai.com 注册后拿到的 API Key",
        base_url_hint="https://api.bochaai.com/v1/web-search",
        request_timeout=12.0,
    ),
    WebProvider(
        id="baidu",
        label="百度搜索",
        aliases=("bd",),
        summary="百度智能云搜索 API，请按官方文档核对端点与鉴权方式。",
        needs_key=True,
        key_hint="百度智能云 API Key（到百度智能云控制台申请搜索服务）",
        base_url_hint="https://aip.baidubce.com/rest/2.0/search/v1/search",
        request_timeout=12.0,
    ),
    WebProvider(
        id="bing",
        label="必应 Bing",
        aliases=("msn",),
        summary="微软 Bing 搜索 API，英文结果稳定，含标题/链接/摘要。",
        needs_key=True,
        key_hint="Azure / Bing Search APIs 的 Ocp-Apim-Subscription-Key",
        base_url_hint="https://api.bing.microsoft.com/v7.0/search",
        request_timeout=12.0,
    ),
)

#: 免 Key 的默认服务商（``config.WEB_SEARCH_PROVIDER`` 的默认值也是它）
DEFAULT_PROVIDER = "duckduckgo"


def provider_spec(value: str | None) -> WebProvider | None:
    """按 ``id`` 或别名取服务商行；未登记返回 ``None``（**不回落**，回落会掩盖配置错）。"""
    key = str(value or "").strip().lower()
    if not key:
        return None
    for item in WEB_PROVIDERS:
        if key == item.id or key in item.aliases:
            return item
    return None


def normalize_provider(value: str | None) -> str:
    """别名 → 规范 id；未登记时返回「小写去空格的原值」（调用方据此报可读错误）。

    与老基线的 ``normalize_provider(provider, fallback="duckduckgo")`` 有意不同：
    老版把**任何**拼错的服务商静默换成 duckduckgo，用户以为在用 Serper 实际走了 DDG
    （排查方向全错）。本仓按接口冻结的口径——未登记就留给调用方报错。
    """
    spec = provider_spec(value)
    if spec is not None:
        return spec.id
    return str(value or "").strip().lower()


# --- 搜索渠道（传统搜索 API / 大模型搜索）-----------------------------------

CHANNEL_API = "api"
CHANNEL_LLM = "llm"

#: 渠道描述（设置页照此渲染；条目 4.1 只实装 ``api``）
CHANNELS: tuple[dict[str, str], ...] = (
    {
        "id": CHANNEL_API,
        "label": "传统搜索 API",
        "summary": (
            "调用 DuckDuckGo / Serper / Tavily / 博查 / 百度 / 必应等搜索接口，"
            "结果为标题 + 链接 + 摘要。"
        ),
    },
    {
        "id": CHANNEL_LLM,
        "label": "大模型搜索",
        "summary": (
            "走大模型对话接口做联网检索，由模型整理出若干条带链接的摘要"
            "（如 DeepSeek 网页搜索）。本版本尚未实装。"
        ),
    },
)

__all__ = [
    "CHANNELS",
    "CHANNEL_API",
    "CHANNEL_LLM",
    "DEFAULT_PROVIDER",
    "WEB_PROVIDERS",
    "WebProvider",
    "normalize_provider",
    "provider_spec",
]
