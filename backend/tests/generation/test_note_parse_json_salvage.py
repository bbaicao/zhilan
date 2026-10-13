"""task-2 · 畸形 JSON 输出的字段抢救与标题兜底（用户现场「保存失败，请重试」根因）。

现场：模型输出不是合法 JSON（单行、末尾截断）⇒ 旧实现把整段原文当标题 ⇒ 后端
`note_routes.py:64` 的 `title: str = Field(min_length=1, max_length=255)` 判 422
⇒ 保存整单失败。

本文件钉三件事：
① 整段 JSON 非法时先按正则抢救 ``"title"`` / ``"content"`` 两个字段（值体按 JSON
   字符串体解码，字面 ``\\n`` / ``\\\\`` 还原成真实正文）；
② 仍抢救不到时，疑似 JSON / 超长的首行**不许**当标题（回落 fallback_title）；
③ 最终 title 一律裁剪到 ≤255 字符，且 ``GeneratedNote`` 形状与 completeness 语义不变。
"""

from __future__ import annotations

import json

import pytest

from app.generation.note_gen import parse_note_output

_SAMPLE_TITLE = "像素海项目纪要"
_SAMPLE_BODY = (
    "# 像素海项目纪要\n\n"
    "> 参考目录 \\像素海\\ 的结构，正文说明若干\n\n"
    "- 首轮验收 2026-03-05：覆盖目录与索引两部分，记录人：资料组\n"
    "- 样本 1200 份，完成率 98.5%\n"
    "- 待办：补充 \\像素海\\ 二级目录的说明文档与截图\n"
    "- 备注：本轮不涉及模型重跑与全量回归，仅本地核对\n"
    "- 归档：2026-03-06 复核通过，编号 ZH-2026-031\n"
    "- 目录规模：一级目录 12 个、二级目录 87 个、文件合计 1043 个\n"
    # 最后一行不带尾随换行：parse_note_output 对 content 会 .strip()
    "- 后续：按 2026-03-10 排期整理索引快照，并复核像素海原始目录"
)


def _truncated_model_output(title: str, body: str) -> str:
    """合法 JSON 砍掉收尾 `"}` ⇒ 真实形态的单行截断畸形输出（字面 \\n 与 \\\\ 都在）。"""
    return json.dumps({"title": title, "content": body}, ensure_ascii=False)[:-2]


# ① 真实形态畸形样本：整段 JSON 非法 ⇒ 按字段抢救出标题与正文


def test_d1_truncated_single_line_json_is_salvaged_into_title_and_body():
    """单行、超长、末尾截断的 JSON：标题 = 捞到的短标题，正文 = 解码后的真实 Markdown。"""
    raw = _truncated_model_output(_SAMPLE_TITLE, _SAMPLE_BODY)
    assert "\n" not in raw and len(raw) > 255, "构造前提：单行且首行远超 255 字"
    assert "\\n" in raw and "\\\\像素海\\\\" in raw, "构造前提：原文里是字面 \\n 与转义反斜杠"
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)  # 构造前提：整段确实不是合法 JSON

    note = parse_note_output(raw, fallback_title="别的兜底名")

    assert note.title == _SAMPLE_TITLE, "捞到的 title 字段胜过整段原文"
    assert len(note.title) <= 255
    assert note.content == _SAMPLE_BODY, "字面 \\n / \\\\ 必须还原成真实换行与单个反斜杠"
    assert note.content != raw, "正文不得再是整段原始 JSON"
    assert note.completeness == {
        "complete": True,
        "checker": "regex",
        "checked": 0,
        "missing": [],
    }, "GeneratedNote / completeness 语义不变（本函数只产草稿）"


def test_d2_invalid_escapes_are_loosely_unescaped_and_never_raise():
    """值体里的非法转义（``\\像素``）不许把异常抛给调用方：丢反斜杠保留原字符。"""
    raw = '{"title": "标题", "content": "路径 \\像素海\\ 与字面 \\n 结尾"'

    note = parse_note_output(raw, fallback_title="兜底")

    assert note.title == "标题"
    assert note.content == "路径 像素海 与字面 \n 结尾"


def test_d6_content_only_salvage_uses_fallback_title():
    """只捞到 ``content``（title 字段缺失）⇒ 正文用捞到的、标题回落 fallback_title。"""
    raw = '{"content": "# 正文\\n\\n- 要点 2026-03-05"'

    note = parse_note_output(raw, fallback_title="源文件")

    assert note.title == "源文件"
    assert note.content == "# 正文\n\n- 要点 2026-03-05"


def test_d7_title_only_salvage_keeps_raw_text_as_content():
    """只捞到 ``title``（content 值不是字符串）⇒ 正文退回原文，不产空笔记。"""
    raw = '{"title": "只有标题", "content": 42,}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)

    note = parse_note_output(raw, fallback_title="兜底")

    assert note.title == "只有标题"
    assert note.content == raw


# ② 抢救不到时：疑似 JSON / 超长的首行不许当标题


def test_d3_long_first_line_without_fields_falls_back_to_filename_stem():
    """首行远超 255 且抠不到字段 ⇒ 标题 = fallback_title；正文保持既有兜底语义（原文）。"""
    raw = "这是一段模型没按 JSON 输出的说明文字。" * 12
    assert "\n" not in raw and len(raw) > 255, "构造前提：单行、远超 255 字"

    note = parse_note_output(raw, fallback_title="原始文档名")

    assert note.title == "原始文档名"
    assert note.content == raw, "正文兜底语义不变：join(lines[1:]).strip() or text"
    assert len(note.title) <= 255


def test_d4_json_like_first_line_without_salvageable_fields_falls_back():
    """首行疑似 JSON（以 `{` 开头 / 含 `"title"`）但字段捞不到 ⇒ 同样不取作标题。"""
    for raw in ('{"title": 42, "content": 43,}', "{'title': '单引号不是 JSON'} ..."):
        note = parse_note_output(raw, fallback_title="兜底文件名")
        assert note.title == "兜底文件名", f"疑似 JSON 的首行不许当标题：{raw!r}"
        assert note.content == raw


def test_d5_final_title_is_clipped_to_255_characters():
    """③ 兜底链的任一路径（Markdown 标题 / fallback_title 过长）都要裁到 ≤255 字符。"""
    long_heading = "字" * 300
    note = parse_note_output(f"# {long_heading}\n正文", fallback_title="兜底")

    assert note.title == "字" * 255 and len(note.title) == 255
    assert note.content == "正文"

    long_stem = "文件名" * 100  # 300 字符
    note2 = parse_note_output("x" * 300, fallback_title=long_stem)

    assert len(note2.title) == 255 and note2.title == long_stem[:255]
    assert note2.content == "x" * 300, "裁剪只作用于 title，正文不动"
