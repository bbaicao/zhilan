"""DeepL 翻译规格模块（条目 23；阶段 4 · 条目 `4.5.3` 移植到 v2）—— 一处真相。

产品口径（任务清单 23，与老基线逐条一致）：

- 选中文本 / 笔记 / 回答走 DeepL 翻译；**只做中英互译**（``"zh" → "ZH"``、``"en" → "EN-US"``）；
- Key 只存本机 ``.env``（``DEEPL_API_KEY``），接口层**绝不回传 Key 本体**（条目 2 的密钥策略）；
- 免费端点与官方端点按官方口径分流：Key 以 ``:fx`` 结尾 = 免费版，走
  ``https://api-free.deepl.com``，否则走 ``https://api.deepl.com``；
- 单次翻译字符上限 ``MAX_TRANSLATE_CHARS``（超长由路由层 400，本模块不截断）；
- 所有失败（Key 未配置 / 超时 / 网络错误 / HTTP 非 2xx / 返回体不认识）一律抛
  :class:`DeepLError`，message 都是用户能看懂的中文。

HTTP 状态映射由路由层做：:class:`DeepLError.kind` →
``not_configured``/``bad_request`` → 400、``auth``（403 鉴权失败）→ 401、
``timeout`` → 504、其余（429 限频 / 456 配额用尽 / 网络错误 / 其他状态码）→ 502。

**为什么放 `app/api/`**（与 `rerank_spec.py` 同一理由）：v2 没有 `app/rag/` 那一层；
翻译只有这一个端点消费，规格模块与路由同层（`api → core`），不引入新的反向依赖。
"""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.errors import RagError

#: 单次翻译的字符上限（契约口径；超长由路由层 400，这里只登记常量）
MAX_TRANSLATE_CHARS = 20000

#: 中英互译的目标语言映射：值是 DeepL 官方目标语言代码（英文按美式拼写 EN-US）
TARGET_LANGS = {"zh": "ZH", "en": "EN-US"}

#: 调 DeepL 的超时（秒）：官方接口通常秒级返回，30s 已是宽限
TRANSLATE_TIMEOUT = 30.0

NOT_CONFIGURED_HINT = "尚未配置 DeepL Key，请到设置 → 翻译完成配置"
AUTH_HINT = "DeepL 鉴权失败：Key 无效或没有权限，请到设置检查 DeepL Key"
QUOTA_HINT = "DeepL 翻译配额已用尽"
RATE_HINT = "翻译请求过于频繁，请稍后再试"

#: kind 取值：接口层据此映射 HTTP 状态码
KIND_BAD_REQUEST = "bad_request"
KIND_NOT_CONFIGURED = "not_configured"
KIND_AUTH = "auth"
KIND_TIMEOUT = "timeout"
KIND_OTHER = "other"


class DeepLError(RagError):
    """DeepL 翻译失败：message 必须是可读中文；``kind`` 供接口层映射 HTTP 状态码。"""

    def __init__(self, message: str, *, kind: str = KIND_OTHER) -> None:
        super().__init__(message)
        self.kind = kind


def deepl_base_url(key: str) -> str:
    """按 Key 形态分流 DeepL 官方端点（免费 Key 以 ``:fx`` 结尾）。"""
    key = (key or "").strip()
    return "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"


def target_lang(target: str) -> str | None:
    """目标语言码归一（大小写不敏感）；不在中英互译范围内返回 None。"""
    return TARGET_LANGS.get((target or "").strip().lower())


async def translate_text(text: str, target: str) -> dict:
    """把 ``text`` 翻译成 ``target``（``"zh"`` | ``"en"``）。

    返回 ``{"text": 译文, "detected_source_language": "EN"|"ZH"|...}``；
    Key 取自 ``settings.DEEPL_API_KEY``。任何失败抛 :class:`DeepLError`（可读中文）。
    """
    text = (text or "").strip()
    if not text:
        raise DeepLError("请提供要翻译的文本", kind=KIND_BAD_REQUEST)
    key = (settings.DEEPL_API_KEY or "").strip()
    if not key:
        raise DeepLError(NOT_CONFIGURED_HINT, kind=KIND_NOT_CONFIGURED)
    lang = target_lang(target)
    if lang is None:
        raise DeepLError("目标语言仅支持 zh（中文）或 en（英文）", kind=KIND_BAD_REQUEST)

    url = f"{deepl_base_url(key)}/v2/translate"
    try:
        async with httpx.AsyncClient(timeout=TRANSLATE_TIMEOUT) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"DeepL-Auth-Key {key}"},
                json={"text": [text], "target_lang": lang},
            )
    except httpx.TimeoutException as exc:
        raise DeepLError("翻译请求超时：DeepL 服务连不上，请稍后再试。", kind=KIND_TIMEOUT) from exc
    except httpx.HTTPError as exc:
        # httpx 网络错的 str() 常为空串，至少给出异常类型名，别让提示变成半句话
        detail = str(exc).strip() or type(exc).__name__
        raise DeepLError(
            f"翻译请求失败（网络错误）：{detail}。请检查本机网络后重试。", kind=KIND_OTHER
        ) from exc

    if resp.status_code == 403:
        raise DeepLError(AUTH_HINT, kind=KIND_AUTH)
    if resp.status_code == 456:
        raise DeepLError(QUOTA_HINT, kind=KIND_OTHER)
    if resp.status_code == 429:
        raise DeepLError(RATE_HINT, kind=KIND_OTHER)
    if not (200 <= resp.status_code < 300):
        raise DeepLError(
            f"翻译请求失败（HTTP {resp.status_code}）：DeepL 服务返回异常，请稍后再试。",
            kind=KIND_OTHER,
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise DeepLError("DeepL 返回的内容无法解析，翻译未完成。", kind=KIND_OTHER) from exc
    translations = data.get("translations") if isinstance(data, dict) else None
    first = translations[0] if isinstance(translations, list) and translations else None
    if not isinstance(first, dict):
        raise DeepLError("DeepL 返回的内容无法解析，翻译未完成。", kind=KIND_OTHER)

    return {
        "text": str(first.get("text") or ""),
        "detected_source_language": str(first.get("detected_source_language") or ""),
    }


__all__ = [
    "AUTH_HINT",
    "KIND_AUTH",
    "KIND_BAD_REQUEST",
    "KIND_NOT_CONFIGURED",
    "KIND_OTHER",
    "KIND_TIMEOUT",
    "MAX_TRANSLATE_CHARS",
    "NOT_CONFIGURED_HINT",
    "QUOTA_HINT",
    "RATE_HINT",
    "TARGET_LANGS",
    "TRANSLATE_TIMEOUT",
    "DeepLError",
    "deepl_base_url",
    "target_lang",
    "translate_text",
]
