"""澄清轮 clarify（阶段 1 · 条目 1.4）。

**一句话**：用户这一句还没法去检索（太短、只有指代、信息不足）时，不硬答也不拒答，
而是**追问一句**把缺的信息问出来。

## 位置：理解前置，早于路由与检索

老基线（源码只读快照）的图是 ``understand → (clarify | rewrite → retrieve → grade)``，
即澄清判定**在检索之前**。v2 沿用这个顺序（阶段 1 设计说明 条目 1.4 位置写的是
「``app/generation/clarify.py`` + **路由/理解前置**」）：

```
提问 → ① 澄清判定（本模块）→ 不清晰 → kind=clarify，追问一句，结束
                            → 清晰   → ② 路由（routing）→ ③ 直答/检索
```

## 三条硬口径（条目 1.4 规格）

1. **I1 kind=clarify**：澄清轮的契约 ``kind`` 恒为 ``clarify``，正文是追问原文；
2. **I7 不把澄清误判成拒答**：澄清是「我再问你一句」，拒答是「资料里没有」——
   两者的文案、kind、answer_source 三者全部不同（本模块与 ``answer.py`` 的
   ``REFUSE_TEXT`` 严格分开，有专门用例双向断言）；
3. **映射**：``answer_source=clarify``、``refs`` 恒空（§3.8 第 4 行，经 ``source_map``）。

## 降级：拿不到追问就不追问

``QUERY_CLARIFY_ENABLE`` 关闭、或没配对话模型、或模型没给出可用追问 → **一律放行**
（``clear=True``），让问答按正常路径继续。澄清是**增益**不是关卡：它不许把本来能答的
问题卡住（否则就是拿 I7 的语义去伤害可用性）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.core.config import settings
from app.generation import source_map
from app.generation.answer import ASSUME_PREFIX, GenerationResult

logger = logging.getLogger(__name__)

# --- 追问文案（对照老基线 clarify.py，非整文件搬运）---------------------

CLARIFY_SYSTEM = (
    "你是知澜。用户这一句还没法检索知识库，你要追问一句，把缺的信息问出来。"
    "根据他刚说的话来问，不要用套话，不要同时问一串人/文件/时间。"
    "只输出这一句中文追问，不要解释，不要引号，不要前缀。"
)

# 模型爱加的引号与前缀，输出前剥掉
_STRIP_CHARS = "\"'“”‘’"
_PREFIXES = ("追问：", "追问:", "问题：", "问题:")

# 追问调用的小预算（追问就该短）
CLARIFY_MAX_TOKENS = 120
CLARIFY_TEMPERATURE = 0.4

# 历史喂给追问模型的最大轮数（老基线取最近 6 条）
HISTORY_LIMIT = 6

# --- 模糊判定规则（1.4.1）-----------------------------------------------

# 客套/语气词/虚词：剥掉后剩下的才是「实质内容」
_FILLER = re.compile(
    r"(帮我|请你|请|麻烦|看看|看一下|看下|了解一下|说一下|一下|嗯|啊|呢|吧|"
    r"这个|那个|这些|那些|它|他|她|的|是|什么|怎么|怎样|如何)"
)
# 指代词：出现它且实质内容很少 → 指代不清
_DEMONSTRATIVE = re.compile(r"(这个|那个|这些|那些|它|他|她)们?")

# 少于这个字数直接判模糊
MIN_LENGTH = 4
# 剥掉虚词后少于这个字数判模糊
MIN_LEFTOVER = 2
# 有指代词时，实质内容少于这个字数判模糊
MIN_LEFTOVER_WITH_DEMONSTRATIVE = 4

# 判定依据（写进 verdict.reason，便于 1.4.3 排障与阶段 5 调参）
REASON_TOO_SHORT = "too_short"
REASON_NO_SUBSTANCE = "no_substance"
REASON_DEMONSTRATIVE = "demonstrative_only"
REASON_SPECIFIC = "specific_enough"
REASON_DISABLED = "disabled"
REASON_NO_MODEL = "no_model"
REASON_MAX_ROUNDS = "max_rounds_reached"
REASON_NO_QUESTION = "no_question"


def looks_vague(message: str) -> bool:
    """规则判定：这句话是不是「没法检索」。

    三条规则（与老基线同序、同阈值）：

    1. 去空白后长度 < 4 → 模糊（「嗯」「那个」）；
    2. 剥掉客套/虚词后长度 < 2 → 模糊（「那个怎么样」只剩「样」）；
    3. 出现指代词且实质内容 < 4 → 模糊（「它呢」「这个怎么弄」）。

    纯函数、无 IO——规则先行，模型只负责**写追问**，不负责判定（1.4.1）。
    """
    text = (message or "").strip()
    if len(text) < MIN_LENGTH:
        return True
    leftover = _FILLER.sub("", text)
    leftover = re.sub(r"\s+", "", leftover)
    if len(leftover) < MIN_LEFTOVER:
        return True
    if _DEMONSTRATIVE.search(text) and len(leftover) < MIN_LEFTOVER_WITH_DEMONSTRATIVE:
        return True
    return False


def vagueness_reason(message: str) -> str:
    """返回判定依据（``REASON_*`` 之一），供留痕与调参。"""
    text = (message or "").strip()
    if len(text) < MIN_LENGTH:
        return REASON_TOO_SHORT
    leftover = re.sub(r"\s+", "", _FILLER.sub("", text))
    if len(leftover) < MIN_LEFTOVER:
        return REASON_NO_SUBSTANCE
    if _DEMONSTRATIVE.search(text) and len(leftover) < MIN_LEFTOVER_WITH_DEMONSTRATIVE:
        return REASON_DEMONSTRATIVE
    return REASON_SPECIFIC


# --- 判定结果 -----------------------------------------------------------


@dataclass
class ClarifyVerdict:
    """一次澄清判定的结论。

    ``clear=True`` = 放行（按正常路径问答）；``clear=False`` = 该追问。
    ``assume=True`` = 已达追问上限，按现有信息作答并在正文前加假设前缀。
    """

    clear: bool
    assume: bool = False
    question: str = ""
    missing: list[str] = field(default_factory=list)
    reason: str = ""
    # 追问是否真的问到了模型（可观测：rule=规则放行 / llm=模型写了追问）
    source: str = "rule"

    @property
    def is_clarify(self) -> bool:
        return not self.clear

    def to_result(self) -> GenerationResult:
        """转成澄清轮的生成结果（契约三字段经 ``source_map`` 收口）。"""
        kind, answer_source, refs = source_map.map_clarify_round()
        return GenerationResult(
            answer=self.question,
            kind=kind,
            answer_source=answer_source,
            refs=refs,
        )


# --- 追问文案生成（1.4.2）-----------------------------------------------


def clarify_enabled() -> bool:
    """澄清开关是否打开（``QUERY_CLARIFY_ENABLE``，默认开）。"""
    return bool(getattr(settings, "QUERY_CLARIFY_ENABLE", True))


def max_rounds() -> int:
    """追问上限（``QUERY_CLARIFY_MAX_ROUNDS``，默认 2；负值按 0 处理）。"""
    return max(0, int(getattr(settings, "QUERY_CLARIFY_MAX_ROUNDS", 2)))


def _clean_question(text: str) -> str:
    """剥掉模型爱加的引号/前缀，只留第一行。

    顺序很关键：**先剥引号 → 再剥前缀 → 再剥一次引号**。老基线的写法是
    「剥引号 → 剥前缀」，于是模型回 ``“追问：你指的是哪份文档？”`` 时，前缀被剥掉了，
    但**结尾的引号留在原地**（那句 `strip` 在剥前缀之前就跑完了），用户会看到
    「你指的是哪份文档？”」。这里在剥完前缀后再补一次，把它收干净——只影响
    「模型多套了一层引号」这一种输入，正常输出逐字不变。
    """
    raw = (text or "").strip().strip(_STRIP_CHARS)
    for prefix in _PREFIXES:
        if raw.startswith(prefix):
            raw = raw[len(prefix) :].strip().strip(_STRIP_CHARS)
            break
    return raw.splitlines()[0].strip().strip(_STRIP_CHARS) if raw else ""


def _recent_history(history: list[dict] | None) -> str:
    """把最近几轮拼成可读上下文（只取 user/assistant 且有正文）。"""
    lines: list[str] = []
    for item in (history or [])[-HISTORY_LIMIT:]:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            who = "用户" if role == "user" else "知澜"
            lines.append(f"{who}：{content}")
    return "\n".join(lines)


async def draft_clarify_question(
    message: str, history: list[dict] | None = None, *, model=None
) -> str:
    """让模型写一句追问；拿不到就返回空串（调用方据此放行）。

    ``model`` 可注入（单测用 FauxProvider）；不传按配置装配 —— ⚠️ **默认装配走
    ``build_aux_model()``**（辅助链路，不带用户配置的思考参数；A14 的「澄清请求体不含思考键」）。
    **任何失败都返回空串**，绝不抛异常——澄清不许把问答卡死（I5 精神）。
    """
    bound = model
    if bound is None:
        from app.generation.answer import build_aux_model

        bound = build_aux_model()
    if bound is None:
        return ""

    recent = _recent_history(history)
    prompt = (
        f"最近对话：\n{recent}\n\n用户这一句：{message}" if recent else f"用户这一句：{message}"
    )

    from cubeloop.providers.base import TextContent, UserMessage

    try:
        from app.usage.instrumentation import usage_scope
        with usage_scope(role="clarify"):
            result = await bound.generate(
                [UserMessage(content=[TextContent(text=prompt)])],
                system_prompt=CLARIFY_SYSTEM,
                max_output_tokens=CLARIFY_MAX_TOKENS,
                temperature=CLARIFY_TEMPERATURE,
            )
    except Exception as exc:  # noqa: BLE001 —— 追问失败必须放行，不许炸穿
        logger.warning("追问生成失败（将放行不追问）：%s: %s", exc.__class__.__name__, exc)
        return ""

    if getattr(result, "stop_reason", "") == "error":
        logger.warning("追问返回错误（将放行不追问）：%s", getattr(result, "error_message", ""))
        return ""

    text = "".join(
        getattr(block, "text", "")
        for block in getattr(result, "content", [])
        if getattr(block, "type", "") == "text"
    )
    return _clean_question(text)


# --- 编排（1.4.1 + 1.4.2 合流）-----------------------------------------


async def judge_query(
    message: str,
    history: list[dict] | None = None,
    *,
    clarify_rounds: int = 0,
    model=None,
    enabled: bool | None = None,
    limit: int | None = None,
    force: bool = False,
) -> ClarifyVerdict:
    """判定这一句要不要追问，并（该追问时）产出追问原文。

    判定顺序（与老基线一致）：

    1. 开关关闭 → 放行（``REASON_DISABLED``）；
    2. 已达上限（``clarify_rounds >= max_rounds``）→ 放行且 ``assume=True``
       （按现有信息作答，正文前加 ``ASSUME_PREFIX``）；
    3. 规则判定不模糊 → 放行（``REASON_SPECIFIC``）；
    4. 该追问 → 让模型写一句；**写不出来就放行**（``REASON_NO_QUESTION``）。

    参数可注入（``model`` / ``enabled`` / ``limit``），单测因此不必改全局配置。
    """
    active_enabled = clarify_enabled() if enabled is None else bool(enabled)
    if not active_enabled:
        return ClarifyVerdict(clear=True, reason=REASON_DISABLED)

    active_limit = max_rounds() if limit is None else max(0, int(limit))
    if clarify_rounds >= active_limit:
        return ClarifyVerdict(clear=True, assume=True, reason=REASON_MAX_ROUNDS)

    # U3.5：未能可靠绑定历史指代时仍复用相同开关、上限与失败放行。
    reason = "unresolved_reference" if force else vagueness_reason(message)
    if not force and not looks_vague(message):
        return ClarifyVerdict(clear=True, reason=reason)

    question = await draft_clarify_question(message, history, model=model)
    if not question:
        return ClarifyVerdict(clear=True, reason=REASON_NO_QUESTION)

    return ClarifyVerdict(
        clear=False,
        question=question,
        missing=["具体对象"],
        reason=reason,
        source="llm",
    )


def assume_prefix(assume: bool) -> str:
    """追问达上限后的假设前缀（正文前置）。"""
    return f"{ASSUME_PREFIX}\n\n" if assume else ""


__all__ = [
    "CLARIFY_MAX_TOKENS",
    "CLARIFY_SYSTEM",
    "CLARIFY_TEMPERATURE",
    "HISTORY_LIMIT",
    "REASON_DEMONSTRATIVE",
    "REASON_DISABLED",
    "REASON_MAX_ROUNDS",
    "REASON_NO_MODEL",
    "REASON_NO_QUESTION",
    "REASON_NO_SUBSTANCE",
    "REASON_SPECIFIC",
    "REASON_TOO_SHORT",
    "ClarifyVerdict",
    "assume_prefix",
    "clarify_enabled",
    "draft_clarify_question",
    "judge_query",
    "looks_vague",
    "max_rounds",
    "vagueness_reason",
]
