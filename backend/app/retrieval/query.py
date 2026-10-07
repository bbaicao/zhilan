"""U3.5：只在指代/明确选择时绑定原始用户历史，回答仍使用原问。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

QUERY_REWRITE_VERSION = "u3-query-v1"
HISTORY_LIMIT = 12
_NUMBER = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_SELECTION = re.compile(r"第([一二三四五六七八九十]|[1-9]\d?)(?:个|种|项|套|款|份)")
_PREVIOUS = re.compile(r"上(?:一个|一种|一项|一份|一款|次那个|个)")
_PRONOUN = re.compile(r"(?<!其)(?:它(?!们)|这个|那个|这份|那份|这种|那种|该文档|该方案|该产品)")
_OPTIONS = re.compile(r"^\s*(?:[-*]\s*)?(?:([1-9]\d?)[.、）)]|([一二三四五六七八九十])[、）)]|[（(]([1-9]\d?|[一二三四五六七八九十])[）)])\s*(.+)$")
_MULTIPLE = re.compile(r"比较|对比|分别|两(?:个|种|项|份)|哪(?:个|种)|和|与|或者|还是|、|\bvs\b", re.I)
_GUESS = re.compile(r"可能|大概|也许|应该|预计|假设|推测|猜测")
_QUANTITY = re.compile(r"\d+(?:\.\d+)?\s*(?:元|万元|年|月|日|%|天|小时)")
_QUOTED = re.compile(r"《[^》\n]+》|[“\"「][^”\"\n」]+[”\"」]")
_NAMED = re.compile(r"《[^》\n]+》|[“\"「][^”\"\n」]+[”\"」]|[A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*")
_OBJECT_KIND = re.compile(r"文件|文档|报告|合同|方案|设备|产品|型号|项目|计划|政策|条例|手册|记录|协议|流程|系统|手机|耳机")
_CLAIM = re.compile(r"是|支持|包含|需要|预算|成本|金额|价格|期限|免费|[=＝]")
_CONSTRAINT = re.compile(r"预算|不超过|最多|至少|不要|不含|不使用|只看|仅看|支持|价格|金额|\d+年|\d+月")


@dataclass(frozen=True)
class QueryRewrite:
    original_question: str
    retrieval_query: str
    trigger: str = "none"
    status: str = "unchanged"
    elapsed_ms: float = 0.0
    calls: int = 0
    version: str = QUERY_REWRITE_VERSION

    @property
    def needs_clarify(self) -> bool:
        return self.status == "unresolved"

    @property
    def fallback(self) -> bool:
        return self.status == "failed"

    def payload(self) -> dict[str, Any]:
        """只记录机制与成本；消息正文已有唯一存储入口。"""
        return {
            "trigger": self.trigger,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "calls": self.calls,
            "strategy_version": self.version,
        }


def trigger_for(question: str) -> str:
    # 同句已经指明标题/型号的指代不依赖历史；引号内的文本也不是相对指代。
    stripped = _QUOTED.sub(lambda match: " " * len(match.group()), question)
    if _SELECTION.search(stripped):
        return "selection"
    if _PREVIOUS.search(stripped):
        return "previous"
    reference = _PRONOUN.search(stripped)
    if not reference or _NAMED.search(question[:reference.start()]):
        return "none"
    return "pronoun"


def _number(value: str) -> int:
    return _NUMBER[value] if value in _NUMBER else int(value)


def _options(content: str) -> tuple[dict[int, str], str]:
    choices: dict[int, str] = {}
    prelude: list[str] = []
    for line in content.splitlines():
        match = _OPTIONS.match(line)
        if match:
            index = _number(next(value for value in match.groups()[:3] if value))
            if index in choices:
                return {}, ""
            choices[index] = match.group(4).strip()
        elif not choices:
            prelude.append(line)
    return choices, "\n".join(prelude).strip()


def _assistant_label(item: str) -> str:
    # 助手仅供选择标签；冒号/句号后的价格、期限、推测等均不作为已知事实。
    label = re.split(r"[:：。；;,，\n]", item, maxsplit=1)[0].strip("*` \t")
    if not label or len(label) > 48 or _GUESS.search(label) or _QUANTITY.search(label) or _CLAIM.search(label):
        return ""
    return label if trigger_for(label) == "none" else ""


def _user_context(history: list[dict]) -> str:
    context: list[str] = []
    for position in range(len(history) - 1, -1, -1):
        item = history[position]
        if item.get("role") != "user":
            continue
        text = str(item.get("content") or "").strip()
        if not text:
            continue
        context.append(text)
        trigger = trigger_for(text)
        if trigger == "selection":
            selected = _bind(text, history[:position], trigger)
            return "\n".join([selected, *reversed(context[:-1])]) if selected else ""
        if trigger == "none":
            choices, _ = _options(text)
            has_object = bool(_NAMED.search(text) or _OBJECT_KIND.search(text))
            if not choices and not has_object and _CONSTRAINT.search(text):
                continue  # 用户单独补的预算／时间／否定条件仍跟随此前明确对象。
            if (
                len(text) < 4 or choices or _MULTIPLE.search(text)
                or not has_object
            ):
                return ""
            return "\n".join(reversed(context))
    return ""


def _bind(question: str, history: list[dict], trigger: str) -> str:
    if trigger != "selection":
        context = _user_context(history)
        return f"用户此前问题：{context}\n本次追问：{question}" if context else ""

    selection = _SELECTION.search(_QUOTED.sub("", question))
    index = _number(selection.group(1))
    for position in range(len(history) - 1, -1, -1):
        item = history[position]
        role = item.get("role")
        if role not in ("user", "assistant"):
            continue
        choices, prelude = _options(str(item.get("content") or ""))
        if not choices:
            if role == "user" and trigger_for(str(item.get("content") or "")) == "none":
                return ""  # 新主题已开始，不能回捞更早一组助手选项。
            continue
        chosen = choices.get(index, "")
        if not chosen:
            return ""
        if role == "assistant":
            chosen = _assistant_label(chosen)
            # 原始用户提出的条件逐字保留；助手列出的其余内容不进入查询。
            prior_users = [
                str(row.get("content") or "").strip()
                for row in history[:position]
                if row.get("role") == "user" and row.get("content")
            ]
            prelude = "\n".join(prior_users)
        if not chosen:
            return ""
        context = f"{prelude}\n用户选择：{chosen}".strip()
        return f"{context}\n本次追问：{question}"
    return ""


def rewrite_query(
    question: str,
    history: list[dict] | None = None,
    *,
    filters: dict | None = None,
) -> QueryRewrite:
    """无模型调用。失败保留原问，不能可靠绑定时交给既有澄清入口。

    ``history`` 必须是消息表的原始历史，不能传压缩摘要/记忆。filters 只读，
    搜索仍使用宿主的同一范围。ponytail: 明确指代/编号的保守文法有覆盖上限；
    自由语义不作猜测，统一走现有澄清，不增加一个自动改写模型。
    """
    started = time.perf_counter()
    trigger = "none"
    status = "unchanged"
    query = question
    try:
        trigger = trigger_for(question)
        if trigger != "none":
            query = _bind(question, list(history or [])[-HISTORY_LIMIT:], trigger)
            status = "applied" if query else "unresolved"
            query = query or question
    except Exception:  # 改写是优化项：坏历史/规则故障不能阻断回答，也不记录正文。
        status, query = "failed", question
    return QueryRewrite(
        original_question=question,
        retrieval_query=query,
        trigger=trigger,
        status=status,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        version=QUERY_REWRITE_VERSION,
    )


__all__ = ["QUERY_REWRITE_VERSION", "QueryRewrite", "rewrite_query", "trigger_for"]
