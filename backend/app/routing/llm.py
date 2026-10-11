"""LLM 兜底分类（阶段 1 · 条目 1.2.2）。

正则没命中时，用小调用问一次模型「这句要不要去知识库找」。三条硬要求：

1. **无 Key / 未配置 / 调用失败 / 返回看不懂** → 一律**不抛异常**，交回 ``None``
   让上层按 ``ROUTING_LLM_FALLBACK`` 降级（条目 1.2 规格「禁止 500」）；
2. **可注入**：分类器是一个 ``Protocol``，单测注入 FauxProvider 驱动的实现，
   不连真模型（统筹 §3.4「单测：FauxProvider」）；
3. **小调用**：单轮、低 token、低温，且**只让模型回一个词**，不做自由生成。

真链路走 CubeLoop provider（与阶段 4 的 Agent 同一套底座），不另起一套 HTTP 客户端。
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# 让模型只回一个标签，便于解析；仍容忍多余空白/标点/大小写
SYSTEM_PROMPT = (
    "你是知澜的意图分类器。判断用户这句话是否需要查他自己的知识库"
    "（本地文档、笔记、资料）。"
    "只回一个词：internal_db 表示需要查知识库；external 表示不需要"
    "（闲聊、常识、写作、翻译、代码等）。"
    "不要解释，不要标点，不要多余文字。"
)

# 分类调用的小预算（小调用：低 token + 低温）
MAX_OUTPUT_TOKENS = 8
TEMPERATURE = 0.0

INTERNAL = "internal_db"
EXTERNAL = "external"

# 模型可能回这些等价写法，一并归一
_SYNONYMS: dict[str, str] = {
    "internal_db": INTERNAL,
    "internal": INTERNAL,
    "internaldb": INTERNAL,
    "kb": INTERNAL,
    "knowledge_base": INTERNAL,
    "knowledgebase": INTERNAL,
    "search": INTERNAL,
    "库内": INTERNAL,
    "知识库": INTERNAL,
    "external": EXTERNAL,
    "general": EXTERNAL,
    "direct": EXTERNAL,
    "库外": EXTERNAL,
    "闲聊": EXTERNAL,
}


@runtime_checkable
class Classifier(Protocol):
    """把一句话分成 ``internal_db`` / ``external``；判不了返回 ``None``。"""

    async def classify(self, text: str) -> str | None:
        ...


def parse_label(raw: str | None) -> str | None:
    """把模型输出归一成两个标签之一；看不懂返回 ``None``。

    宽容解析（模型爱加标点/换行/引号），但**不做猜测**：看不懂就是看不懂，
    交给上层降级，而不是硬塞一个结果。
    """
    if not raw:
        return None
    cleaned = raw.strip().strip("\"'“”`。，,.!！?？ \t\r\n").lower()
    if not cleaned:
        return None
    if cleaned in _SYNONYMS:
        return _SYNONYMS[cleaned]
    # 容忍「internal_db。」这类前后缀残留：按首个词再试一次
    first = cleaned.split()[0] if cleaned.split() else ""
    if first in _SYNONYMS:
        return _SYNONYMS[first]
    # 句子里明确出现了标签词（模型没听话时兜一下）
    for key in (INTERNAL, EXTERNAL):
        if key in cleaned:
            return key
    return None


class LlmClassifier:
    """用 CubeLoop bound model 做一次小调用。

    ``bound`` 是 ``provider.model(model_id)`` 的返回值（见 pitfalls：
    要 ``bound = provider.model(id)`` 再 ``await bound.generate(...)``）。
    """

    def __init__(self, bound, *, max_output_tokens: int = MAX_OUTPUT_TOKENS) -> None:
        self._bound = bound
        self._max_output_tokens = max_output_tokens

    async def classify(self, text: str) -> str | None:
        from cubeloop.providers.base import TextContent, UserMessage

        message = UserMessage(content=[TextContent(text=text)])
        try:
            from app.usage.instrumentation import usage_scope
            with usage_scope(role="routing"):
                result = await self._bound.generate(
                    [message],
                    system_prompt=SYSTEM_PROMPT,
                    max_output_tokens=self._max_output_tokens,
                    temperature=TEMPERATURE,
                )
        except Exception as exc:  # noqa: BLE001 —— 分类失败必须降级，不许炸穿
            logger.warning("意图分类调用失败（将按 fallback 降级）：%s: %s", exc.__class__.__name__, exc)
            return None

        if getattr(result, "stop_reason", "") == "error":
            logger.warning(
                "意图分类返回错误（将按 fallback 降级）：%s", getattr(result, "error_message", "")
            )
            return None

        texts = [
            getattr(block, "text", "")
            for block in getattr(result, "content", [])
            if getattr(block, "type", "") == "text"
        ]
        return parse_label("".join(texts))


def build_classifier() -> Classifier | None:
    """按当前配置装配真分类器；**没配好就返回 ``None``**（调用方据此降级）。

    不在这里抛异常：未配置模型是正常状态（桌面版首次启动就没 Key），
    必须走 ``ROUTING_LLM_FALLBACK`` 而不是 500。
    """
    from app.core.config import settings

    try:
        cfg = settings.chat_config
    except Exception as exc:  # noqa: BLE001 —— 配置坏数据同样按「没配好」处理
        logger.warning("读取对话模型配置失败（将按 fallback 降级）：%s", exc)
        return None
    if cfg is None or not (cfg.api_key or "").strip():
        return None

    try:
        from cubeloop.providers.openai import OpenAIProvider
        from cubeloop.providers.capability import CapabilityDescriptor
    except Exception as exc:  # noqa: BLE001 —— 缺依赖不致命，降级即可
        logger.warning("加载 LLM provider 失败（将按 fallback 降级）：%s", exc)
        return None

    try:
        provider = OpenAIProvider(
            api_key=cfg.api_key, base_url=cfg.base_url or None, provider_id=cfg.provider,
            capability=CapabilityDescriptor(),
        )
        from app.usage.instrumentation import observe_provider
        observe_provider(provider)
        return LlmClassifier(provider.model(cfg.model))
    except Exception as exc:  # noqa: BLE001
        logger.warning("装配意图分类器失败（将按 fallback 降级）：%s", exc)
        return None


__all__ = [
    "EXTERNAL",
    "INTERNAL",
    "MAX_OUTPUT_TOKENS",
    "SYSTEM_PROMPT",
    "Classifier",
    "LlmClassifier",
    "build_classifier",
    "parse_label",
]
