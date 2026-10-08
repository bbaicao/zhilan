"""Provider-native search; only provider citation metadata becomes sources."""

from urllib.parse import urlparse

import httpx

from app.core.config import settings

MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"


def _official_endpoint(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.netloc.lower() in {
        "api.xiaomimimo.com", "api.xiaomimimo.com:443",
    }


def resolved_config() -> tuple[str, str, str, str]:
    provider = (settings.WEB_LLM_PROVIDER or "").strip().lower()
    base = (settings.WEB_LLM_BASE_URL or "").strip().rstrip("/")
    key = (settings.WEB_LLM_API_KEY or "").strip()
    model = (settings.WEB_LLM_MODEL or "").strip()
    try:
        chat = settings.chat_config
    except ValueError:
        chat = None
    # Custom OpenAI-compatible services may identify MiMo by its official host.
    if chat and provider == "mimo" and _official_endpoint(chat.base_url):
        target = base or MIMO_BASE_URL
        if _official_endpoint(target):
            key = key or chat.api_key
            model = model or chat.model
    return provider, base or (MIMO_BASE_URL if provider == "mimo" else ""), key, model


def configuration_error() -> str:
    provider, base, key, model = resolved_config()
    if provider != "mimo":
        return "大模型搜索目前支持 MiMo，请选择 MiMo 或改用传统搜索 API。"
    if (settings.WEB_LLM_SEARCH_TOOL or "").strip() != "web_search":
        return "请开启服务商联网插件；普通对话接口不会自动联网。"
    if not base or not key or not model:
        return "请配置 MiMo 搜索的接口地址、API Key 和型号，或绑定官方 MiMo 对话模型。"
    return ""


async def search(query: str, *, max_results: int, timeout: float):
    from app.agent.web.client import WebHit, WebToolError, _make_client

    error = configuration_error()
    if error:
        raise WebToolError(error)
    _provider, base, key, model = resolved_config()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "请使用联网搜索查找相关资料，概括检索结果并保留来源引用。"},
            {"role": "user", "content": query},
        ],
        "tools": [{"type": "web_search", "force_search": True,
                   "max_keyword": max(1, min(6, int(settings.WEB_LLM_MAX_SEARCHES)))}],
        "stream": False,
    }
    try:
        async with _make_client(timeout=timeout, headers={"Authorization": f"Bearer {key}"}) as client:
            response = await client.post(f"{base}/chat/completions", json=body)
        if response.status_code != 200:
            raise WebToolError(f"MiMo 联网搜索失败（HTTP {response.status_code}），请检查型号、权限及额度。")
        payload = response.json()
        message = payload["choices"][0]["message"]
        summary = str(message.get("content") or "")[:4000]
        hits = []
        seen = set()
        for entry in message.get("annotations") or []:
            if not isinstance(entry, dict) or entry.get("type") != "url_citation":
                continue
            citation = entry.get("url_citation") or entry
            if not isinstance(citation, dict):
                continue
            url = str(citation.get("url") or "").strip()
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or url in seen:
                continue
            seen.add(url)
            snippet = str(citation.get("summary") or summary)[:4000]
            hits.append(WebHit(title=str(citation.get("title") or url), url=url, snippet=snippet))
        if not hits:
            raise WebToolError("MiMo 未返回可核验的搜索来源，请检查该型号是否支持联网搜索。")
        return hits[:max_results]
    except WebToolError:
        raise
    except httpx.TimeoutException:
        raise WebToolError("MiMo 联网搜索超时，请稍后重试。") from None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        # Provider response bodies and exception URLs may contain credentials.
        raise WebToolError("MiMo 联网搜索请求失败或响应格式无效，请检查网络和配置。") from None
