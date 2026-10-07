"""正则规则集（阶段 1 · 条目 1.2.1）——纯函数，无 IO。

设计口径（对应阶段 1 设计说明「正则勿过度拟合」）：

1. **规则只判「要不要去库里找」，不判「找得到吗」**。命中的意义是
   ``internal_db``（该检索），不是「有引用」。
2. **误判是廉价且可自愈的**：命中了但库里没东西 → 1.3 走 direct 直答
   （统筹 §3.8 第 2 行），用户看到的仍是正常回答；反之漏判才会把
   库内问题当库外答（更容易出错）。所以规则集**宁可稍宽，不可过窄**。
3. 但仍要挡住明显的错判：裸词 ``库里`` 不能吃掉「**数据库**里」
   （见 ``_NOT_DB`` 负向断言）。
4. 每条规则有**名字**，命中后写进决策日志的 ``matched_rule``（I9 可追溯，
   也让阶段 5 调参时能看出是哪条规则在起作用）。

规则分两档，命中任一即视为库内：

- **显式引用档**：话里直接提到知识库/文档/资料/笔记等**库内对象**——
  这是强信号，几乎不会误判；
- **检索动作档**：``查一下`` / ``搜一下`` 等**检索动词**——按条目 1.2 规格
  收录，但单独出现时信号较弱，故单独成档、单独命名，便于后续按数据调权。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 「数据库」里的「库里」不是知识库：负向断言挡住（坑清单式的最小防呆）
_NOT_DB = r"(?<!数据)"

# 中文里的「里/中/内」都可指范围；「有没有/是否」是典型的库内提问句式
_SCOPE = r"(里|中|内)"
_ASK_HAS = r"(有没有|有没|是否有|是否|有无|提到|说过|讲过|写过|记过)"


@dataclass(frozen=True)
class Rule:
    """一条命名规则：名字进决策日志，pattern 只做判定。"""

    name: str
    pattern: re.Pattern[str]
    description: str


def _rule(name: str, regex: str, description: str) -> Rule:
    return Rule(name=name, pattern=re.compile(regex, re.IGNORECASE), description=description)


# --- 档一：显式引用库内对象（强信号）------------------------------------
#
# 档内顺序 = **具体优先**：同一次提问可能命中多条（「我上传的资料里」同时满足
# ``my_library`` 与 ``doc_scope``），日志里记更具体的那条才有排障价值。
# 判定结果（internal_db）不受影响，变的只是 ``matched_rule`` 的可读性。

EXPLICIT_RULES: tuple[Rule, ...] = (
    _rule("kb_named", r"知识库|知识库里|知识库中|我的知识库|本地知识库", "直接提到「知识库」"),
    _rule("my_library", r"我(的|上传的|导入的|存的)(文档|文件|资料|笔记|材料)", "提到「我的文档/我上传的资料」"),
    _rule("kb_scope", rf"{_NOT_DB}库{_SCOPE}", "「库里/库中」指知识库（排除「数据库」）"),
    _rule("doc_scope", rf"(文档|文件|资料|笔记|材料|附件){_SCOPE}", "提到文档/资料/笔记等库内对象"),
    _rule("doc_ask", rf"(文档|文件|资料|笔记|知识库|材料|附件).{{0,6}}{_ASK_HAS}", "「文档里有没有…」式提问"),
    _rule("kb_english", r"\b(knowledge\s*base|kb|my\s+docs?|my\s+documents?)\b", "英文知识库指代"),
)

# --- 档二：检索动作（弱信号，按条目 1.2 规格收录）-----------------------

RETRIEVAL_RULES: tuple[Rule, ...] = (
    _rule("retrieval_verb", r"(查一下|查查|查询一下|搜一下|搜搜|搜索一下|找一下|检索一下|翻一下)", "检索动词"),
    _rule("retrieval_where", r"(哪里|哪儿|哪份|哪个文件|哪篇).{0,6}(提到|说过|写过|讲过)", "「哪里提到过…」式提问"),
    _rule("citation_ask", r"(出处|来源|引用|依据|第几页|原文)", "追问出处/引用"),
)

ALL_RULES: tuple[Rule, ...] = EXPLICIT_RULES + RETRIEVAL_RULES

RULE_NAMES: tuple[str, ...] = tuple(rule.name for rule in ALL_RULES)


def match_rule(text: str) -> Rule | None:
    """返回第一条命中的规则；没有命中返回 ``None``。

    顺序即优先级：显式引用档在检索动作档之前，所以「知识库里查一下」会记
    ``kb_named`` 而不是 ``retrieval_verb``（日志里体现更强的那条依据）。
    """
    for rule in ALL_RULES:
        if rule.pattern.search(text):
            return rule
    return None


def matched_rule_names(text: str) -> list[str]:
    """列出所有命中的规则名（调参/排障用，判定本身只看 ``match_rule``）。"""
    return [rule.name for rule in ALL_RULES if rule.pattern.search(text)]


__all__ = [
    "ALL_RULES",
    "EXPLICIT_RULES",
    "RETRIEVAL_RULES",
    "RULE_NAMES",
    "Rule",
    "match_rule",
    "matched_rule_names",
]
