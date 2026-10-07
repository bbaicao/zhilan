"""全局搜索操作符解析（阶段 3 · 条目 3.4）：纯函数，不碰数据库。

从老仓库 ``app/rag/search_query.py`` 移植**解析部分**（行为逐字对齐），并按 v2 分层拆开：
条件 → SQL 子句的构造要跟着 v2 的 ORM 模型走（且标签改用 Python 二次收口），
所以这些都落在 :mod:`app.search.service`，本模块只留「字符串 → 条件 + 文案」这一半，
只依赖标准库。

语法（老仓库冻结，界面照同一份登记渲染提示）：

- ``tag:复盘``      标签（笔记/文档的 tags 数组包含；写多个要**都**满足）
- ``path:工作日志`` 分类 —— 与 ``category:`` 同义。做**包含**匹配，写一半也能找到；
- ``file:周报``     文档文件名或笔记标题包含
- ``date:2026-08``  入库日期（``created_at``）；支持 ``2026`` / ``2026-08`` /
                    ``2026-08-01``、``2026-08-01..2026-08-31``（闭区间，写法不加空格）、
                    ``>=2026-08-01`` / ``>2026-08-01`` / ``<=2026-08-31`` / ``<2026-09-01``
- 其余裸词仍是关键词；**出现操作符之后**，剩下的裸词按空格拆开、每个都要命中（AND）。

约定（逐条对齐老仓库）：

- 条件之间取交集 —— 「不做嵌套布尔 DSL」下唯一的组合方式；
- 全角 ``：`` 与 ``:`` 等价；值可以用 ``"…"`` 包住，里面可以有空格；
- 不认识的 ``xx:`` 前缀（例如 ``https://…``）**不当操作符**，仍是关键词；
- **没写操作符时与改造前逐字一致**：整串当关键词做子串匹配（``q.strip()``），
  既有搜索结果与排序一点不变（``legacy=True``）；
- 操作符写了非法值（如 ``date:昨天``）给**可读警告**并跳过该条件，不静默丢弃、
  也不把 ``date:昨天`` 当关键词去搜（那只会搜出个寂寞）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# 结果条数上限：沿用改造前写死的 20，不新增分页口径（老仓库逐字沿用）
SEARCH_LIMIT = 20

# 操作符登记：用户写法 → 内部类型（``path`` 是 ``category`` 的别名）
OPERATOR_KINDS: dict[str, str] = {
    "tag": "tag",
    "path": "category",
    "category": "category",
    "file": "file",
    "date": "date",
}
KIND_LABELS: dict[str, str] = {"tag": "标签", "category": "分类", "file": "文件名", "date": "日期"}
# 界面常驻提示照这里渲染（一处真相）
SYNTAX_HINT = "tag:标签 · path:分类 · file:文件名 · date:2026-08-01..2026-08-31"
DATE_SYNTAX = "2026 / 2026-08 / 2026-08-01 / 2026-08-01..2026-08-31 / >=2026-08-01"
DATE_HINT = f"date: 只认 {DATE_SYNTAX} 这几种写法"

_OPERATOR_RE = re.compile(r"^(tag|path|category|file|date)\s*[:：]\s*(.*)$", re.IGNORECASE)
_TAG_SPLIT_RE = re.compile(r"[,，、]")
_COMPARE_RE = re.compile(r"^(>=|<=|>|<)\s*(.+)$")
_YEAR_RE = re.compile(r"^\d{4}$")
_MONTH_RE = re.compile(r"^\d{4}-\d{1,2}$")
_DAY_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
_OPEN_QUOTES = '"\u201c'
_CLOSE_QUOTES = '"\u201d'


@dataclass
class Condition:
    """解析出的一条限定条件（给界面回显，也用于文案）。"""

    kind: str
    value: str

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def text(self) -> str:
        return f"{self.label}：{self.value}"


@dataclass
class SearchQuery:
    """一次全局搜索的解析结果：关键词 + 结构化条件 + 可读警告。"""

    raw: str = ""
    keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    date_from: datetime | None = None
    date_to: datetime | None = None  # 右开区间：``< date_to``
    conditions: list[Condition] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # True = 没写操作符：关键词是整串原文（改造前的口径）
    legacy: bool = False

    @property
    def is_blank(self) -> bool:
        """空查询：与改造前一致地返回空结果。"""
        return not self.raw

    @property
    def is_empty(self) -> bool:
        """没有任何可用的搜索条件（例如只写了 ``date:昨天``）。"""
        return not self.keywords and not self.conditions

    def describe(self) -> str:
        parts = [item.text() for item in self.conditions]
        if self.keywords:
            parts.append("关键词：" + "、".join(f"「{word}」" for word in self.keywords))
        return "；".join(parts)


def no_hit_hint(query: SearchQuery) -> str:
    """没有命中时的可读提示：说清按什么条件找的，并指一条下一步。"""
    scope = query.describe() or "当前条件"
    text = f"没有找到同时满足{scope}的内容。"
    if len(query.categories) > 1:
        # 分类只有一个取值，写多个是互相排斥的 —— 这是最容易踩的坑，说明白
        text += "分类只有一个取值，`path:` 写多个会互相排斥，建议只留一个。"
    elif query.conditions:
        text += "可以少写一个条件，或用 file: / tag: 换个角度找。"
    else:
        text += "可以换个关键词再试。"
    return text


def no_condition_hint(query: SearchQuery) -> str:
    """有条件写法但一条都没成立（如 ``date:昨天``）时的提示。

    分工：**警告负责说明哪里写错了**（界面会单独把警告显示一遍），这里只给**下一步**。
    否则同一句话在界面上出现两遍（警告行一遍、提示段再复述一遍），又长又吵。
    """
    if not query.warnings:
        return f"没有解析出可用的搜索条件。可用的写法：{SYNTAX_HINT}（date: 也支持 {DATE_SYNTAX}）。"
    if DATE_SYNTAX in " ".join(query.warnings):
        # 日期警告里已经带完整写法，这里不再抄一遍
        return "按上面的提示改一改写法，或者直接写关键词。"
    return f"可用的写法：{SYNTAX_HINT}；也可以直接写关键词。"


def parse_search(raw: str) -> SearchQuery:
    """把搜索框里的字符串解析成 :class:`SearchQuery`。"""
    text = (raw or "").strip()
    query = SearchQuery(raw=text)
    if not text:
        return query

    tokens = _tokenize(text)
    recognized = False
    bare: list[str] = []
    for token in tokens:
        match = _OPERATOR_RE.match(token)
        if match is None:
            bare.append(token)
            continue
        recognized = True
        _apply_operator(query, match.group(1).lower(), match.group(2).strip())

    if not recognized:
        # 没有操作符：与改造前逐字一致（整串当关键词，连引号都原样保留）
        query.keywords = [text]
        query.legacy = True
        return query

    query.keywords = bare
    return query


def _tokenize(text: str) -> list[str]:
    """按空白切词；``"…"`` 包住的部分算一个词（值里可以有空格）。"""
    tokens: list[str] = []
    buf: list[str] = []
    in_quote = False
    for char in text:
        if in_quote:
            if char in _CLOSE_QUOTES:
                in_quote = False
            else:
                buf.append(char)
            continue
        if char in _OPEN_QUOTES:
            in_quote = True
            continue
        if char.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        buf.append(char)
    if buf:
        tokens.append("".join(buf))
    return tokens


def _apply_operator(query: SearchQuery, name: str, value: str) -> None:
    """把一条 ``name:value`` 写进查询对象；非法值只记警告。"""
    kind = OPERATOR_KINDS[name]
    if not value:
        query.warnings.append(f"{name}: 后面没有写值，这个条件已跳过")
        return

    if kind == "tag":
        tags = _split_tags(value)
        if not tags:
            query.warnings.append(f"{name}: 后面没有写值，这个条件已跳过")
            return
        for tag in tags:
            if tag not in query.tags:
                query.tags.append(tag)
                query.conditions.append(Condition("tag", tag))
        return

    if kind == "category":
        if value not in query.categories:
            query.categories.append(value)
            query.conditions.append(Condition("category", value))
        return

    if kind == "file":
        if value not in query.files:
            query.files.append(value)
            query.conditions.append(Condition("file", value))
        return

    span = parse_date_span(value)
    if span is None:
        query.warnings.append(f"{name}:{value} 不是可识别的日期，这个条件已跳过（{DATE_HINT}）")
        return
    start, end = span
    if start is not None:
        query.date_from = start if query.date_from is None else max(query.date_from, start)
    if end is not None:
        query.date_to = end if query.date_to is None else min(query.date_to, end)
    query.conditions.append(Condition("date", _date_label(start, end)))


def _split_tags(value: str) -> list[str]:
    """标签值支持逗号分隔（与 frontmatter 的 tags 写法一致），多个标签取交集。"""
    tags: list[str] = []
    for item in _TAG_SPLIT_RE.split(value):
        text = item.strip()
        if text and text not in tags:
            tags.append(text)
    return tags


def _date_label(start: datetime | None, end: datetime | None) -> str:
    """日期条件的展示文本：右开区间换算回用户看得懂的「含末日」。"""
    left = start.strftime("%Y-%m-%d") if start else "更早"
    right = (end - timedelta(days=1)).strftime("%Y-%m-%d") if end else "更晚"
    if start and end and start.date() == end.date() - timedelta(days=1):
        return left
    return f"{left} ~ {right}"


def parse_date_span(value: str) -> tuple[datetime | None, datetime | None] | None:
    """解析日期条件，返回 ``[起, 止)``；认不出来返回 ``None``。

    ``止`` 是右开区间（``< 止``），所以 ``2026-08-31`` 这种「含末日」的写法会换成 ``09-01``，
    避免在 DATETIME 秒级精度上纠结 23:59:59。
    """
    text = value.strip()
    if not text:
        return None

    if ".." in text:
        left, _, right = text.partition("..")
        left, right = left.strip(), right.strip()
        left_span = _unit_span(left) if left else None
        right_span = _unit_span(right) if right else None
        if (left and left_span is None) or (right and right_span is None):
            return None
        start = left_span[0] if left_span else None
        end = right_span[1] if right_span else None
        if start is None and end is None:
            return None
        return start, end

    compare = _COMPARE_RE.match(text)
    if compare is not None:
        operator, body = compare.group(1), compare.group(2).strip()
        span = _unit_span(body)
        if span is None:
            return None
        start, end = span
        if operator == ">=":
            return start, None
        if operator == ">":
            return end, None  # 严格晚于该单位 = 从它的下一格开始
        if operator == "<=":
            return None, end
        return None, start

    return _unit_span(text)


def _unit_span(text: str) -> tuple[datetime, datetime] | None:
    """``2026`` / ``2026-08`` / ``2026-08-01`` 三种粒度 → ``[起, 止)``。"""
    value = text.strip().replace("/", "-")
    try:
        if _YEAR_RE.match(value):
            year = int(value)
            return datetime(year, 1, 1), datetime(year + 1, 1, 1)
        if _MONTH_RE.match(value):
            year, month = (int(part) for part in value.split("-"))
            start = datetime(year, month, 1)
            end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
            return start, end
        if _DAY_RE.match(value):
            year, month, day = (int(part) for part in value.split("-"))
            start = datetime(year, month, day)
            return start, start + timedelta(days=1)
    except ValueError:  # 2026-13-45 这类假日期不算数
        return None
    return None
