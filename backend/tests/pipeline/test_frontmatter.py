"""Markdown frontmatter 解析（老仓库 test_frontmatter.py 离线用例对拍）。

需容器的入库落列 / 上传响应 / 清洗预览 / 语料导入用例随 2.4 ingest 迁入。
"""

from datetime import datetime

from app.pipeline.frontmatter import date_from_name, parse_frontmatter


def test_parse_full_block():
    """三个键齐全：值正确、正文剥离、无警告。"""
    text = "---\ncategory: 工作日志\ntags: [实习, 剪辑]\ncreated_at: 2026-07-01\n---\n正文第一行\n\n正文第二行"
    result = parse_frontmatter(text)
    assert result.present is True
    assert result.category == "工作日志"
    assert result.tags == ["实习", "剪辑"]
    assert result.created_at == datetime(2026, 7, 1)
    assert result.body == "正文第一行\n\n正文第二行"
    assert result.warnings == []


def test_parse_no_frontmatter_returns_body_unchanged():
    """没有 frontmatter：正文原样返回、present=False、零警告。"""
    text = "# 标题\n\n正文内容"
    result = parse_frontmatter(text)
    assert result.present is False
    assert result.body == text
    assert result.warnings == []


def test_hr_without_closing_is_not_frontmatter():
    """首行 ``---`` 找不到闭合时是水平分割线：不当 frontmatter，不剥正文，无警告。"""
    text = "---\n这就是正文，不是元数据"
    result = parse_frontmatter(text)
    assert result.present is False
    assert result.body == text
    assert result.warnings == []


def test_empty_block_is_legal():
    """空 frontmatter（Obsidian 允许）不算失败：无字段、无警告。"""
    result = parse_frontmatter("---\n---\n只有一行正文")
    assert result.present is True
    assert result.category == ""
    assert result.tags == []
    assert result.created_at is None
    assert result.warnings == []


def test_illegal_yaml_warns_with_line_number_and_keeps_body():
    """非法 YAML：带行号的警告，正文照常返回（解析失败不静默）。"""
    text = "---\ncategory: 工作日志\ntags: [实习, 剪辑\n---\n正文仍在"
    result = parse_frontmatter(text)
    assert result.present is True
    assert len(result.warnings) == 1
    assert "第 3 行" in result.warnings[0], result.warnings  # 文件行号（YAML 头上面还有 ---）
    assert "正文不受影响" in result.warnings[0]
    assert result.body == "正文仍在"


def test_non_dict_yaml_warns():
    """YAML 解析出列表而非键值对：警告、元数据为空。"""
    result = parse_frontmatter("---\n- a\n- b\n---\n正文")
    assert result.present is True
    assert result.warnings and "键值对" in result.warnings[0]
    assert result.category == "" and result.tags == [] and result.created_at is None


def test_tags_forms():
    """tags 收列表或逗号串；去空去重保序；支持中文逗号与顿号。"""
    listed = parse_frontmatter("---\ntags: [实习, 剪辑, 实习, \"\"]\n---\nX")
    assert listed.tags == ["实习", "剪辑"]
    comma = parse_frontmatter("---\ntags: a, b 、c，b\n---\nX")
    assert comma.tags == ["a", "b", "c"]


def test_tags_wrong_type_warns():
    result = parse_frontmatter("---\ntags: 123\n---\nX")
    assert result.tags == []
    assert result.warnings and "tags" in result.warnings[0]


def test_created_at_forms():
    """safe_load 自动解析的日期、字符串 ISO、带时间三种形态。"""
    by_yaml = parse_frontmatter("---\ncreated_at: 2026-07-01\n---\nX")
    assert by_yaml.created_at == datetime(2026, 7, 1)
    by_text = parse_frontmatter('---\ncreated_at: "2026-08-09 10:30:00"\n---\nX')
    assert by_text.created_at == datetime(2026, 8, 9, 10, 30)
    assert by_text.warnings == []


def test_created_at_invalid_warns():
    result = parse_frontmatter("---\ncreated_at: not-a-date\n---\nX")
    assert result.created_at is None
    assert result.warnings and "created_at" in result.warnings[0]


def test_unknown_keys_silently_ignored():
    """先不做任意键全量落库：未知键不算失败、不产生警告。"""
    result = parse_frontmatter("---\nalias: 别名\ncustom_field: 1\ncategory: 周报\n---\nX")
    assert result.category == "周报"
    assert result.warnings == []


def test_category_wrong_type_warns():
    result = parse_frontmatter("---\ncategory: [工作日志]\n---\nX")
    assert result.category == ""
    assert result.warnings and "category" in result.warnings[0]


def test_category_long_value_truncated():
    """超列宽（255）截断，不炸入库。"""
    result = parse_frontmatter("---\ncategory: 长" * 1 + "字" * 300 + "\n---\nX")
    assert len(result.category) == 255


def test_date_from_name():
    assert date_from_name("2026-07-01-入职第一天.md") == datetime(2026, 7, 1)
    assert date_from_name("2026-07-05 第1周周报.md") == datetime(2026, 7, 5)
    assert date_from_name("没有日期的文件.md") is None
    assert date_from_name("2026-13-45-假日期.md") is None  # 假日期不算数
