"""全局搜索操作符的解析用例（阶段 3 · 条目 3.4）：纯函数，离线跑。

覆盖老仓库 ``tests/test_search_operators.py`` 的**解析面**（行为逐字对齐）：

- 没写操作符 = 整串关键词（``legacy=True``，连空格与引号都原样保留）；
- 五种写法的操作符：``tag:``（逗号/顿号多值）、``path:`` / ``category:``（别名同义）、
  ``file:``、``date:``（年月日 / 闭区间 / 四种比较符）、全角 ``：``、引号包值含空格；
- 非法值（``date:昨天`` / ``date:2026-13-45``）→ 记 ``warnings`` 且**跳过该条件**，
  不当关键词去搜；不认识的 ``xx:``（如 ``https://…``）仍当关键词；
- ``is_blank`` / ``is_empty`` / ``describe()`` 与两类提示文案（``no_hit_hint`` /
  ``no_condition_hint``）。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.search.query import (
    DATE_SYNTAX,
    SEARCH_LIMIT,
    SYNTAX_HINT,
    no_condition_hint,
    no_hit_hint,
    parse_date_span,
    parse_search,
)


# --------------------------------------------------------------------- 关键词与操作符识别


def test_parse_plain_keyword_is_legacy():
    """没写操作符：整串当关键词（连空格与引号都原样保留），不拆词。"""
    query = parse_search("  知澜 知识库  ")
    assert query.legacy is True
    assert query.keywords == ["知澜 知识库"]
    assert query.conditions == []
    assert query.raw == "知澜 知识库"
    assert query.warnings == []


def test_parse_legacy_keeps_quotes_verbatim():
    """legacy 口径连引号都原样保留（老基线逐字一致，不做引号剥离）。"""
    query = parse_search('"知澜 知识库"')
    assert query.legacy is True
    assert query.keywords == ['"知澜 知识库"']


def test_parse_unknown_prefix_stays_keyword():
    """不认识的 xx: 前缀不是操作符（否则 http:// 会被当成操作符吃掉）。"""
    query = parse_search("https://example.com/a?b=1")
    assert query.legacy is True
    assert query.keywords == ["https://example.com/a?b=1"]
    assert query.warnings == []


def test_parse_blank_query_is_blank_and_empty():
    for text in ("", "   ", "\t\n"):
        query = parse_search(text)
        assert query.is_blank is True, text
        assert query.is_empty is True
        assert query.raw == "" and query.keywords == [] and query.conditions == []


# --------------------------------------------------------------------- tag


def test_parse_tag_single_and_condition_shape():
    query = parse_search("tag:复盘")
    assert query.legacy is False
    assert query.tags == ["复盘"]
    assert [item.kind for item in query.conditions] == ["tag"]
    assert query.conditions[0].label == "标签"
    assert query.conditions[0].text() == "标签：复盘"
    assert query.describe() == "标签：复盘"


def test_parse_tag_comma_and_dunhao_split():
    """标签值支持半角逗号、全角逗号与顿号分隔，多个标签取交集。"""
    for text in ("tag:复盘,面试", "tag:复盘，面试", "tag:复盘、面试"):
        query = parse_search(text)
        assert query.tags == ["复盘", "面试"], text
        assert [item.value for item in query.conditions] == ["复盘", "面试"], text
        assert query.warnings == []


def test_parse_tag_dedup_across_tokens():
    query = parse_search("tag:复盘,面试 tag:复盘")
    assert query.tags == ["复盘", "面试"]
    assert [item.value for item in query.conditions] == ["复盘", "面试"]  # 重复的不再进条件


def test_parse_tag_empty_value_warns():
    query = parse_search("tag:")
    assert query.tags == []
    assert query.is_empty is True
    assert query.warnings and "没有写值" in query.warnings[0]


# --------------------------------------------------------------------- path / category / file


def test_parse_path_and_category_are_same_kind():
    for text in ("path:工作日志", "category:工作日志", "PATH:工作日志", "Path:工作日志"):
        query = parse_search(text)
        assert query.categories == ["工作日志"], text
        assert query.conditions[0].kind == "category", text
        assert query.conditions[0].label == "分类", text
    mixed = parse_search("path:工作日志 category:工作日志")
    assert mixed.categories == ["工作日志"]  # 同一个值不重复记条件
    assert len(mixed.conditions) == 1


def test_parse_file_value():
    query = parse_search("file:周报")
    assert query.files == ["周报"]
    assert query.conditions[0].kind == "file"
    assert query.conditions[0].label == "文件名"


# --------------------------------------------------------------------- date


def test_parse_date_day_month_year():
    day = parse_search("date:2026-08-04")
    assert day.date_from == datetime(2026, 8, 4)
    assert day.date_to == datetime(2026, 8, 5)
    assert day.conditions[0].value == "2026-08-04"

    month = parse_search("date:2026-08")
    assert (month.date_from, month.date_to) == (datetime(2026, 8, 1), datetime(2026, 9, 1))
    assert month.conditions[0].value == "2026-08-01 ~ 2026-08-31"

    year = parse_search("date:2026")
    assert (year.date_from, year.date_to) == (datetime(2026, 1, 1), datetime(2027, 1, 1))
    assert year.conditions[0].value == "2026-01-01 ~ 2026-12-31"


def test_parse_date_december_month_rolls_over():
    """12 月是边界：``止`` 必须跨年到次年 1 月 1 日，不能写成 12 月 32 日。"""
    query = parse_search("date:2026-12")
    assert (query.date_from, query.date_to) == (datetime(2026, 12, 1), datetime(2027, 1, 1))


def test_parse_date_closed_range_is_right_open():
    """闭区间写法 → 右开 ``[起, 止)``：``2026-08-01..2026-08-31`` 的止换成 09-01。"""
    closed = parse_search("date:2026-08-01..2026-08-31")
    assert (closed.date_from, closed.date_to) == (datetime(2026, 8, 1), datetime(2026, 9, 1))

    until = parse_search("date:..2026-08-31")
    assert until.date_from is None
    assert until.date_to == datetime(2026, 9, 1)
    assert until.conditions[0].value == "更早 ~ 2026-08-31"

    since = parse_search("date:2026-08-01..")
    assert since.date_from == datetime(2026, 8, 1)
    assert since.date_to is None
    assert since.conditions[0].value == "2026-08-01 ~ 更晚"


def test_parse_date_comparisons():
    assert parse_search("date:>=2026-08-01").date_from == datetime(2026, 8, 1)
    assert parse_search("date:>2026-08-01").date_from == datetime(2026, 8, 2)
    assert parse_search("date:<=2026-08-31").date_to == datetime(2026, 9, 1)
    assert parse_search("date:<2026-08-31").date_to == datetime(2026, 8, 31)

    ge = parse_search("date:>=2026-08-01")
    assert ge.date_to is None and ge.conditions[0].value == "2026-08-01 ~ 更晚"
    lt = parse_search("date:<2026-08-31")
    assert lt.date_from is None and lt.conditions[0].value == "更早 ~ 2026-08-30"


def test_parse_date_conditions_intersect():
    query = parse_search("date:>=2026-08-01 date:<=2026-08-31")
    assert (query.date_from, query.date_to) == (datetime(2026, 8, 1), datetime(2026, 9, 1))
    assert len(query.conditions) == 2  # 界面两条都回显，生效的是交集


def test_parse_date_invalid_warns_and_is_empty():
    """``date:昨天``：记警告、跳过条件，且**不**把它当关键词（否则只会搜出个寂寞）。"""
    query = parse_search("date:昨天")
    assert query.date_from is None and query.date_to is None
    assert query.conditions == []
    assert query.keywords == []
    assert query.is_empty is True
    assert query.warnings and "不是可识别的日期" in query.warnings[0]
    assert DATE_SYNTAX in query.warnings[0]


def test_parse_date_slash_and_invalid_calendar():
    assert parse_search("date:2026/08/04").date_from == datetime(2026, 8, 4)
    for text in ("date:2026-13-45", "date:2026-13", "date:不是日期"):
        query = parse_search(text)
        assert query.is_empty is True and query.warnings, text


def test_parse_date_open_range_with_bad_side_warns():
    query = parse_search("date:2026-08-01..不是日期")
    assert query.warnings and query.is_empty is True


def test_parse_date_span_returns_none_for_blank():
    assert parse_date_span("   ") is None
    assert parse_date_span("2026-08-01") == (datetime(2026, 8, 1), datetime(2026, 8, 2))


# --------------------------------------------------------------------- 冒号 / 引号 / 混合


def test_parse_fullwidth_colon_and_quoted_value():
    fullwidth = parse_search("tag：复盘")
    assert fullwidth.tags == ["复盘"]

    quoted = parse_search('tag:"面试 复盘"')
    assert quoted.tags == ["面试 复盘"]
    assert quoted.warnings == []

    chinese_quotes = parse_search("tag:“面试 复盘”")
    assert chinese_quotes.tags == ["面试 复盘"]


def test_parse_quoted_bare_word_is_one_keyword():
    query = parse_search('tag:复盘 "中期 评审"')
    assert query.tags == ["复盘"]
    assert query.keywords == ["中期 评审"]


def test_parse_mixed_keywords_after_operator():
    """出现操作符后，剩下的裸词按空格拆成多个关键词（都要命中）。"""
    query = parse_search("tag:复盘 中期 评审")
    assert query.tags == ["复盘"]
    assert query.keywords == ["中期", "评审"]
    assert query.describe() == "标签：复盘；关键词：「中期」、「评审」"


def test_parse_operator_plus_word_without_space():
    """``path:工作日志中期`` 是一个词：操作符值里带空格必须靠引号分清。"""
    query = parse_search("path:工作日志 中期")
    assert query.categories == ["工作日志"] and query.keywords == ["中期"]


def test_syntax_hint_covers_every_operator():
    """界面常驻提示照这份登记渲染：少一个操作符就是提示与实现不一致。"""
    for token in ("tag:", "path:", "file:", "date:"):
        assert token in SYNTAX_HINT
    assert SEARCH_LIMIT == 20


# --------------------------------------------------------------------- 提示文案


def test_hint_texts_are_readable():
    # 分工：警告负责说明，提示只给下一步 —— 同一句不在界面上出现两遍
    invalid_date = parse_search("date:昨天")
    date_hint = no_condition_hint(invalid_date)
    assert DATE_SYNTAX not in date_hint and "不是可识别的日期" not in date_hint
    assert "直接写关键词" in date_hint

    # 标签写空那条警告没带写法：这时提示才补一句可用写法
    blank_tag = parse_search("tag:")
    tag_hint = no_condition_hint(blank_tag)
    assert SYNTAX_HINT in tag_hint and "没有写值" not in tag_hint


def test_no_hit_hint_names_conditions_and_next_step():
    query = parse_search("tag:复盘 中期")
    hint = no_hit_hint(query)
    assert hint.startswith("没有找到")
    assert "标签：复盘" in hint and "中期" in hint
    assert "换个角度找" in hint

    keyword_only = no_hit_hint(parse_search("不存在的词"))
    assert "换个关键词再试" in keyword_only


def test_no_hit_hint_explains_repeated_category():
    """分类只有一个取值：``path:`` 写多个互相排斥 —— 最容易踩的坑，要说明白。"""
    hint = no_hit_hint(parse_search("path:工作日志 path:周报"))
    assert "互相排斥" in hint


def test_no_condition_hint_without_warnings_lists_syntax():
    """没有任何警告却一条条件都没成立（理论上不出现）时也要给可用写法。"""
    query = parse_search("")
    assert query.warnings == []
    hint = no_condition_hint(query)
    assert SYNTAX_HINT in hint and DATE_SYNTAX in hint


# --------------------------------------------------------------------- 形状（SQL 条件不混层）


def test_note_and_document_clauses_are_separate_layers():
    """文档级条件里不混关键词（关键词走分块正文），标签也不在 SQL 里（v2 在 Python 侧收口）。"""
    from app.search.service import chunk_where, document_where, note_where

    query = parse_search("path:工作日志 file:周报 中期")
    doc_clauses = [str(clause) for clause in document_where(query)]
    assert len(doc_clauses) == 2  # 分类 + 文件名
    assert all("documents." in clause for clause in doc_clauses)

    chunk_clauses = [str(clause) for clause in chunk_where(query)]
    assert len(chunk_clauses) == 1 and "chunks.content" in chunk_clauses[0]

    note_clauses = [str(clause) for clause in note_where(query)]
    assert any("notes.category" in clause for clause in note_clauses)
    assert any("notes.title" in clause for clause in note_clauses)
    assert any("notes.content" in clause for clause in note_clauses)
    assert not any("JSON" in clause.upper() for clause in note_clauses)


def test_tag_condition_does_not_touch_sql_clauses():
    """带 tag 时 SQL 子句里**不出现**标签条件：判定完全在 Python 侧（方言无感）。"""
    from app.search.service import document_where, note_where, tag_filter_enabled

    query = parse_search("tag:复盘")
    assert tag_filter_enabled(query) is True
    assert [str(clause) for clause in document_where(query)] == []
    assert [str(clause) for clause in note_where(query)] == []
    assert tag_filter_enabled(parse_search("复盘")) is False


@pytest.mark.parametrize(
    "tags, want, expected",
    [
        (None, ["复盘"], False),
        ([], ["复盘"], False),
        ({"复盘": 1}, ["复盘"], True),
        ({"复盘": 1}, ["复盘", "面试"], False),
        (["复盘", "面试"], ["复盘", "面试"], True),
        ([" 复盘 "], ["复盘"], True),
    ],
)
def test_has_all_tags_is_intersection(tags, want, expected):
    """标签判定与检索层同口径：同一条资料要同时具备全部标签（多标签取交集）。

    走 ``app.search`` 包门面导入 —— 顺带钉住「公共入口确实导出这些符号」。
    """
    from app.search import has_all_tags

    assert has_all_tags(tags, want) is expected
