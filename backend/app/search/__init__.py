"""全局搜索包（阶段 3 · 条目 3.4）：把老仓库 ``/api/search`` 按 v2 分层重写。

三层分工（**禁止反向 import**：本包绝不 import ``app.api``）：

- :mod:`app.search.query` —— 纯解析：搜索框字符串 → :class:`SearchQuery`
  （操作符 ``tag:`` / ``path:`` / ``category:`` / ``file:`` / ``date:``、关键词、警告、提示文案）。
  只依赖标准库，不碰 DB；
- :mod:`app.search.service` —— DB 查询：只依赖 ``app.db`` / ``app.models`` / ``app.retrieval``
  （标签二次收口复用 ``app.retrieval.types`` 的同一套口径）；
- :mod:`app.api.search_routes` —— HTTP 层：只做取参数 + 组装 pydantic 响应。

行为以老仓库为基线（``<legacy-repo>`` 只读参考）：没写操作符时与改造前**逐字一致**
（整串当关键词做子串匹配），写了操作符则条件之间取交集；无命中给可读中文提示。

两处**有意偏离**（v2 适配，详见 ``service.py`` 里的注释）：

1. 标签不再下推 SQL（老仓库用 ``JSON_CONTAINS``）——v2 是方言无感的，
   带 tag 条件时不加 SQL ``LIMIT``，取回后在 Python 里按「同时具备全部标签」收口；
2. 文档不查 ``index_status`` —— 老仓库也没查，保持一致。
"""

from __future__ import annotations

from app.search.query import (
    DATE_HINT,
    DATE_SYNTAX,
    KIND_LABELS,
    OPERATOR_KINDS,
    SEARCH_LIMIT,
    SYNTAX_HINT,
    Condition,
    SearchQuery,
    no_condition_hint,
    no_hit_hint,
    parse_date_span,
    parse_search,
)
from app.search.service import (
    SearchResult,
    first_chunk_statement,
    has_all_tags,
    search,
    tag_filter_enabled,
)

__all__ = [
    "DATE_HINT",
    "DATE_SYNTAX",
    "KIND_LABELS",
    "OPERATOR_KINDS",
    "SEARCH_LIMIT",
    "SYNTAX_HINT",
    "Condition",
    "SearchQuery",
    "SearchResult",
    "first_chunk_statement",
    "has_all_tags",
    "no_condition_hint",
    "no_hit_hint",
    "parse_date_span",
    "parse_search",
    "search",
    "tag_filter_enabled",
]
