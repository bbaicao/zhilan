"""路由编排（阶段 1 · 条目 1.2）——对外唯一入口 ``route()``。

决策顺序（条目 1.2 规格）：

```
ROUTING_MODE=off   → 不路由（本阶段视为库外 external），source="off"
ROUTING_MODE=llm   → 直接问 LLM；失败按 ROUTING_LLM_FALLBACK 降级
ROUTING_MODE=regex → ① 正则命中 → internal_db（source="regex"，不再问模型）
                     ② 未命中   → LLM 兜底；失败按 ROUTING_LLM_FALLBACK 降级
```

**降级语义**（``ROUTING_LLM_FALLBACK``，命名冻结）：

| 取值     | 含义            | 降级结果      |
|----------|-----------------|---------------|
| `direct` | 默认分路=库外直答 | `external`    |
| `search` | 默认分路=去检索   | `internal_db` |

无论哪条路径，**本函数都不抛异常**：无 Key、模型报错、返回看不懂、配置坏值，
一律落到 fallback 并如实记录 ``source="fallback"``（I9 + 条目 1.2「禁止 500」）。

本阶段 ``off`` 的语义是「视为库外」；阶段 3+ 会改成「全部先检索」，
届时只改这一个分支（阶段 1 设计说明 条目 1.2 第 3 点）。
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.routing import llm as llm_mod
from app.routing import rules
from app.routing.decision import RouteDecision

logger = logging.getLogger(__name__)

VALID_MODES = ("regex", "llm", "off")
VALID_FALLBACKS = ("direct", "search")

# 配置坏值时的兜底（与 config.py 默认值一致）
DEFAULT_MODE = "regex"
DEFAULT_FALLBACK = "direct"


def resolve_mode(raw: str | None) -> str:
    """归一 ``ROUTING_MODE``；坏值回落默认并留 warning（配置是手改得动的）。"""
    mode = (raw or "").strip().lower()
    if mode in VALID_MODES:
        return mode
    if mode:
        logger.warning("ROUTING_MODE 取值不合法：%r，按默认 %s 处理", raw, DEFAULT_MODE)
    return DEFAULT_MODE


def resolve_fallback(raw: str | None) -> str:
    """归一 ``ROUTING_LLM_FALLBACK``；坏值回落默认并留 warning。"""
    value = (raw or "").strip().lower()
    if value in VALID_FALLBACKS:
        return value
    if value:
        logger.warning(
            "ROUTING_LLM_FALLBACK 取值不合法：%r，按默认 %s 处理", raw, DEFAULT_FALLBACK
        )
    return DEFAULT_FALLBACK


def _fallback_decision(fallback: str, reason: str) -> RouteDecision:
    """按 ``ROUTING_LLM_FALLBACK`` 造降级决策（source 恒为 ``fallback``）。"""
    source_type = "internal_db" if fallback == "search" else "external"
    return RouteDecision(source_type=source_type, source="fallback", reason=reason)


async def route(
    text: str,
    *,
    classifier: llm_mod.Classifier | None = None,
    mode: str | None = None,
    fallback: str | None = None,
) -> RouteDecision:
    """判断一句提问该走库内还是库外。

    参数都可注入（``classifier`` / ``mode`` / ``fallback``），默认读配置——
    单测因此不需要改全局 settings，也不会连真模型。

    返回值恒为 ``RouteDecision``，**不抛异常**。
    """
    message = (text or "").strip()
    if not message:
        # 空输入不该走到路由（1.5 会先按 400「请输入问题」拦掉）；
        # 这里给一个安全结论，避免上层拿到 None 再炸。
        return _fallback_decision(
            resolve_fallback(fallback if fallback is not None else settings.ROUTING_LLM_FALLBACK),
            "空输入，按默认分路",
        )

    active_mode = resolve_mode(mode if mode is not None else settings.ROUTING_MODE)
    active_fallback = resolve_fallback(
        fallback if fallback is not None else settings.ROUTING_LLM_FALLBACK
    )

    if active_mode == "off":
        return RouteDecision(
            source_type="external",
            source="off",
            reason="ROUTING_MODE=off：本阶段不路由，视为库外",
        )

    if active_mode == "regex":
        hit = rules.match_rule(message)
        if hit is not None:
            return RouteDecision(
                source_type="internal_db",
                source="regex",
                reason=f"正则命中规则 {hit.name}：{hit.description}",
                matched_rule=hit.name,
            )

    # 走到这里：llm 模式，或 regex 模式未命中 → LLM 兜底
    clf = classifier if classifier is not None else llm_mod.build_classifier()
    if clf is None:
        return _fallback_decision(active_fallback, "LLM 分类器不可用（未配置或装配失败），按默认分路")

    try:
        label = await clf.classify(message)
    except Exception as exc:  # noqa: BLE001 —— 分类器实现可能不守约，兜底到底
        logger.warning("LLM 分类抛出异常（按默认分路降级）：%s: %s", exc.__class__.__name__, exc)
        return _fallback_decision(active_fallback, f"LLM 分类异常（{exc.__class__.__name__}），按默认分路")

    if label == llm_mod.INTERNAL:
        return RouteDecision(
            source_type="internal_db", source="llm", reason="LLM 分类判定需要查知识库"
        )
    if label == llm_mod.EXTERNAL:
        return RouteDecision(
            source_type="external", source="llm", reason="LLM 分类判定不需要查知识库"
        )
    return _fallback_decision(active_fallback, "LLM 分类未给出可识别结果，按默认分路")


__all__ = ["DEFAULT_FALLBACK", "DEFAULT_MODE", "VALID_FALLBACKS", "VALID_MODES", "resolve_fallback", "resolve_mode", "route"]
