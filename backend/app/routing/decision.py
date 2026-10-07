"""路由决策的数据形态（阶段 1 · 条目 1.2.3，I9 的载荷口径）。

一次路由的完整留痕 = ``source_type``（走库内还是库外）+ ``source``（凭什么这么判）
+ ``reason``（人可读理由）+ ``matched_rule``（命中的规则名，没命中为 None）。

``source`` 取值即 I9 要求的「正则/LLM/兜底」三种来源，外加 ``off`` 与 ``clarify``：

| source    | 含义                                             |
|-----------|--------------------------------------------------|
| ``regex`` | 正则规则命中（ROUTING_MODE=regex 的第一层）      |
| ``llm``   | LLM 分类给出结论（ROUTING_MODE=regex 兜底 / llm）|
| ``fallback`` | LLM 不可用或失败，按 ``ROUTING_LLM_FALLBACK`` 降级 |
| ``off``   | ``ROUTING_MODE=off``：不路由（本阶段视为库外）   |
| ``clarify`` | 澄清轮短路：判定该追问，**未进入路由**（见下）   |

``clarify`` 为什么也是一条决策：I9 要求「**每次**问答可追溯路由决策」。
澄清判定发生在路由**之前**（阶段 1 条目 1.4），它短路掉了路由——**「因为要澄清所以
没路由」本身就是这一轮的路由决策**。不记的话，该轮在事件流里既没有决策行、
也看不出是「澄清短路」还是「事件写失败」（回归修复）。
``source_type`` 仍取两值词汇表里的 ``external``：澄清轮确实没有查库。

本模块是**纯数据**：不读配置、不碰数据库，便于单测与阶段 5 回放。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# 内部 source_type（统筹 §3.8）：与 messages.source_type 同词汇表
SourceType = Literal["internal_db", "external"]

# 决策来源（I9 可追溯口径）
RouteSource = Literal["regex", "llm", "fallback", "off", "clarify"]

# 路由模式（配置 ROUTING_MODE，命名冻结）
RoutingMode = Literal["regex", "llm", "off"]

# LLM 失败时的默认分路（配置 ROUTING_LLM_FALLBACK，命名冻结）
LlmFallback = Literal["direct", "search"]


@dataclass(frozen=True)
class RouteDecision:
    """一次意图路由的结论（不可变，可安全缓存/回放）。"""

    source_type: SourceType
    source: RouteSource
    reason: str
    matched_rule: str | None = None

    @property
    def is_internal(self) -> bool:
        """是否走库内（需要检索）。"""
        return self.source_type == "internal_db"

    def to_event_data(self) -> dict:
        """转成 ``session_events`` 载荷（I9 决策日志的落库形态）。

        只放可 JSON 无损往返的基本类型；``None`` 直接省略，保持载荷干净。
        """
        data: dict = {
            "source_type": self.source_type,
            "source": self.source,
            "reason": self.reason,
        }
        if self.matched_rule is not None:
            data["matched_rule"] = self.matched_rule
        return data

    @classmethod
    def from_event_data(cls, data: dict) -> "RouteDecision":
        """从 ``session_events`` 载荷还原（阶段 5 回放/对账用）。"""
        return cls(
            source_type=data["source_type"],
            source=data["source"],
            reason=data.get("reason", ""),
            matched_rule=data.get("matched_rule"),
        )


def clarify_decision(reason: str = "") -> RouteDecision:
    """澄清轮短路的决策留痕（I9：**每一轮**都要有可追溯的决策）。

    澄清判定在路由之前，它短路掉了路由；这条决策把「本轮因为要澄清、所以没路由」
    这件事记下来，让事件流里每一轮都有且只有一条 ``routing/decided``
    （回归修复 的修复）。
    """
    return RouteDecision(
        source_type="external",
        source="clarify",
        reason=reason or "澄清轮短路：判定需要追问，未进入路由",
    )


__all__ = [
    "LlmFallback",
    "RouteDecision",
    "RouteSource",
    "RoutingMode",
    "SourceType",
    "clarify_decision",
]
