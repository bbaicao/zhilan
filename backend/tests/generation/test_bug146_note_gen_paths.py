"""`回归修复`（测试面 P2）：`note_gen` 三条**零判据**路径的纯函数级载体。

缺陷形态（判据面缺口，非产品缺陷）：**正向侧已被钉死、负向侧与两支路零判据**。

- **既有正向判据**（不是本条要补的面）：`tests/api/test_contract_runtime_4_6.py:42-48` ——
  SSE `done` 帧的 completeness 四键 + `checker == "regex"` + `complete is True and missing == []`
  （JSON 出参、走 faux 模型的端点路径）；结构面另由 `tests/contract/test_sse_protocol.py:149-150`
  与 `tests/contract/test_v2_shapes_4_6.py:25` 静态钉住；端点响应侧只有**键存在级**判别力
  （`tests/api/test_documents_endpoints.py:363-365` 只断言 `checker == "regex"`、
  `"complete" in completeness`、`isinstance(missing, list)`；⚠️ 台账/执行卡把这段记作 `:369-371`，
  实测在 `:363-365`，是同一段 generate-note 响应断言）。
- **全仓零载体的三处**（本载体要补的面）：`note_gen` 的 `raise_if_model_error` /
  `parse_note_output` / `check_completeness` 三个函数名在 `backend/tests` 下**只出现在本文件**
  （改前 grep 实测） ⇒

- 模型按 `SYSTEM_PROMPT`（`app/generation/note_gen.py:28-37`）**合法回 Markdown**（提示词明文允许
  「若无法输出 JSON，则输出 Markdown：第一行 `# 标题`」）时，标题/正文是否真被正确切开；
- 模型把 `Incorrect API key…` 这类**错误串当正文**返回时（`raise_if_model_error`，:78-94）是否拦得住；
- `check_completeness`（:164-200）是否退化成恒 `True`（`missing` 恒 `[]`）

—— **这三件现在都不会红**。本载体补三组判据，全部**纯函数级**：直接调 `note_gen` 的模块级函数，
**不出网、不调模型、不碰 DB**（无夹具依赖，`tests/generation/` 目录内无 conftest 约束）。

三组腿构成：

- **① `parse_note_output`（:97-140）的 Markdown 合法形状**（`test_a1`…`test_a7`）：
  第一行 `# 标题` + 正文 ⇒ 标题/正文各自正确（正文**不含**标题行）；无 `#` 时回落首行；
  单行输出**不产出空正文**；空输出回落 `fallback_title` / `未命名笔记`；
  JSON 代码围栏优先且 `tags` 归一（空标签剔除）；JSON 没给正文时**退回原文**；
  正文里出现 `{}` 混排**不误判**成 JSON。
- **② `raise_if_model_error`（:78-94）**（`test_b1`…`test_b6`）：**六个错误标记**逐个人命中即抛 /
  空（或纯空白）输出即抛 / 短正文（`< 400`）命中即抛 / **长正文（`>= 400`）里出现同一标记不拦**
  （长度门槛的负控）/ `399` 抛而 `400` 不抛（边界逐字钉住 `len(stripped) < 400`）。
- **③ `check_completeness`（:164-200）负向侧**（`test_c1`…`test_c5`）：源正文有日期/数字而笔记缺
  ⇒ `missing` **非空**；`completeness` 逐键形状符合 `02` §7.4（四键
  `complete` / `checker` / `checked` / `missing`，**键序**也钉住）；
  每个 missing 项 `item` / `kind` / `context` **三键**、`kind ∈ {date, number}`、
  `item` 必须能在源正文里找到（**不编造 missing 项**）；已覆盖的事实**不进** missing；
  空源无核对项；核对项**有上限**（40 个互异日期 ⇒ `checked` 被截断，不是 40）。

口径（如实登记）：**本条为覆盖面补齐** —— **零产品代码改动**、被测行为一字未改 ⇒
**新载体在改前代码上同样绿**（不是「修掉了一条缺陷」，也没有回退击杀可言）。
"""

from __future__ import annotations

import pytest

from app.core.errors import RagError
from app.generation.note_gen import (
    _MAX_CHECK_ITEMS,
    check_completeness,
    parse_note_output,
    raise_if_model_error,
)

# 六个错误标记（与 app/generation/note_gen.py:84-91 逐字同源）
ERROR_MARKERS = (
    "invalid api key",
    "incorrect api key",
    "rate limit",
    "rate_limit",
    "quota exceeded",
    "authentication",
)


# ===========================================================================
# ① Markdown 合法形状解析（parse_note_output）
# ===========================================================================


def test_a1_markdown_first_line_heading_is_split_into_title_and_body():
    """第一行 `# 标题` + 正文 ⇒ 标题 = 标题行文本、正文 = 余下全部（不含标题行）。"""
    raw = "# 项目说明\n\n正文第一行：2026-03-05 完成首轮验收。\n正文第二行：样本量 1200。"
    note = parse_note_output(raw, fallback_title="文件名兜底")

    assert note.title == "项目说明"
    assert note.content == "正文第一行：2026-03-05 完成首轮验收。\n正文第二行：样本量 1200。"
    assert "# 项目说明" not in note.content, "标题行不得混进正文"
    assert note.category == ""
    assert note.tags is None


def test_a2_markdown_without_heading_falls_back_to_the_first_line():
    """整段没有任何 `# 标题` 行 ⇒ 首行当标题、其余当正文（不是回落到 fallback_title）。"""
    note = parse_note_output("第一行当标题\n第二行是正文", fallback_title="文件名兜底")

    assert note.title == "第一行当标题"
    assert note.content == "第二行是正文"


def test_a3_single_line_output_never_produces_an_empty_body():
    """只有一行的输出 ⇒ 那一行仍是标题，但正文**不能为空**（退回原文，避免空笔记）。"""
    note = parse_note_output("只有一行正文")

    assert note.title == "只有一行正文"
    assert note.content == "只有一行正文"


def test_a4_empty_output_falls_back_to_filename_then_default_title():
    """空 / 纯空白输出 ⇒ 标题回落 `fallback_title`；连它也没有时是「未命名笔记」。"""
    assert parse_note_output("", fallback_title="文件名兜底").title == "文件名兜底"
    assert parse_note_output("   ").title == "未命名笔记"


def test_a5_json_fence_is_preferred_and_tags_are_normalized():
    """JSON 代码围栏优先于 Markdown 回落；`tags` 去空白、剔除空串。"""
    raw = (
        '```json\n{"title": "JSON 标题", "content": "JSON 正文", '
        '"category": "研究", "tags": ["甲", " 乙 ", ""]}\n```'
    )
    note = parse_note_output(
        raw, fallback_title="兜底", default_category="默认分类", default_tags=["丙"]
    )

    assert note.title == "JSON 标题"
    assert note.content == "JSON 正文"
    assert note.category == "研究", "JSON 给了 category ⇒ 覆盖默认分类"
    assert note.tags == ["甲", "乙"], "空标签剔除、两侧空白去掉"


def test_a6_json_without_content_falls_back_to_the_raw_text():
    """JSON 合法但没给 `content` ⇒ 正文退回**原文**（不是空笔记）。"""
    raw = '{"title": "只有标题"}'
    note = parse_note_output(raw)

    assert note.title == "只有标题"
    assert note.content == raw


def test_a7_markdown_body_with_braces_is_not_mistaken_for_json():
    """正文里出现 `{}` 混排但整段不是 JSON ⇒ 仍走 Markdown 分支（不被误判成 JSON）。"""
    note = parse_note_output("# 标题\n正文里有 {大括号} 但不是 JSON", fallback_title="兜底")

    assert note.title == "标题"
    assert note.content == "正文里有 {大括号} 但不是 JSON"


# ===========================================================================
# ② raise_if_model_error：错误标记 + 长度门槛
# ===========================================================================


@pytest.mark.parametrize("marker", ERROR_MARKERS)
def test_b1_short_output_carrying_any_error_marker_raises(marker):
    """六个错误标记**逐个**命中即抛（短文本 ⇒ 不会走「长正文豁免」）。"""
    with pytest.raises(RagError):
        raise_if_model_error(f"模型返回：{marker}（请检查配置）")


def test_b2_marker_matching_is_case_insensitive():
    """标记匹配前 `.lower()`：全大写形态同样拦得住。"""
    with pytest.raises(RagError):
        raise_if_model_error("ERROR: RATE LIMIT EXCEEDED")


def test_b3_empty_or_blank_output_raises():
    """空串 / 纯空白 ⇒ 抛（`if not stripped` 分支，先于标记检查）。"""
    for raw in ("", "   ", "\n\t "):
        with pytest.raises(RagError):
            raise_if_model_error(raw)


def test_b4_short_normal_body_passes():
    """正常短正文（无任何标记）⇒ 不抛。"""
    raise_if_model_error("这是一份正常的笔记正文，含日期 2026-03-05 与数字 42。")


def test_b5_long_body_carrying_a_marker_is_not_blocked():
    """长度门槛的**负控**：`>= 400` 的正常正文里出现同一标记 ⇒ 不拦。

    口径来源 = 实现注释「整段就是错误串时才拦（正文里偶尔出现 error 单词不算）」。
    若有人把长度合取项删掉（只按标记判），本腿转红。
    """
    body = ("这是一份正常笔记正文，条理清晰。" * 30) + " 文中提到 rate limit 这一术语。"
    assert len(body) >= 400, "构造前提：必须越过 400 字符门槛"

    raise_if_model_error(body)  # 不抛


def test_b6_length_threshold_boundary_is_399_raises_400_passes():
    """边界逐字钉住 `len(stripped) < 400`：399 抛、400 不抛。"""
    marker = "incorrect api key"
    short = "x" * (399 - len(marker)) + marker
    exact = "x" * (400 - len(marker)) + marker
    assert len(short) == 399 and len(exact) == 400

    with pytest.raises(RagError):
        raise_if_model_error(short)
    raise_if_model_error(exact)  # 恰好 400 ⇒ 不抛


# ===========================================================================
# ③ check_completeness：负向侧 + missing 形状契约（02 §7.4）
# ===========================================================================


def test_c1_missing_is_non_empty_and_each_item_matches_the_contract():
    """源正文有日期/数字而笔记一条都没覆盖 ⇒ `missing` 非空且逐键形状符合 02 §7.4。"""
    source = "本报告于 2026-03-05 完成，样本量 1200 份，覆盖率 98.5%。"
    note = "本报告已完成，进展顺利。"

    result = check_completeness(note, source)

    # 02 §7.4（:331-338）：completeness 四键，键序也是契约的一部分
    assert list(result.keys()) == ["complete", "checker", "checked", "missing"]
    assert result["complete"] is False
    assert result["checker"] == "regex"
    assert result["checked"] > 0
    assert result["missing"], "源正文有日期与数字、笔记一条都没覆盖 ⇒ missing 必须非空"

    tokens = {item["item"] for item in result["missing"]}
    assert {"2026-03-05", "1200", "98.5%"} <= tokens
    assert result["checked"] == len(result["missing"]), "一条都没覆盖 ⇒ 核对项全部进 missing"

    for item in result["missing"]:
        assert set(item.keys()) == {"item", "kind", "context"}, "02 §7.4：missing 项三键"
        assert isinstance(item["item"], str) and item["item"]
        assert item["kind"] in {"date", "number"}
        assert isinstance(item["context"], str) and item["context"]
        assert item["item"] in source, "不编造 missing 项"


def test_c2_fully_covered_facts_leave_no_missing():
    """正控：源正文的两类事实都写进笔记 ⇒ `complete=True`、`missing == []`。"""
    source = "交付日期 2026-03-05，数量 1200。"
    note = "交付日期是 2026-03-05，数量 1200 件。"

    result = check_completeness(note, source)

    assert result["complete"] is True
    assert result["missing"] == []
    assert result["checked"] > 0, "确实核对了事实（不是「零项 ⇒ 恒 True」的退化）"


def test_c3_only_the_uncovered_facts_are_listed():
    """剔除法：日期已覆盖、数字 1200 未覆盖 ⇒ missing **恰好**只列 1200。"""
    source = "2026-03-05 完成，样本量 1200 份。"
    note = "项目于 2026-03-05 完成。"

    result = check_completeness(note, source)

    assert [item["item"] for item in result["missing"]] == ["1200"]
    assert result["complete"] is False


def test_c4_checking_is_capped_not_unbounded():
    """核对项**有上限**：源正文给 40 个互异日期 ⇒ `checked` 被截断（远小于 40）。"""
    dates = " ".join(f"2026-{month:02d}-{day:02d}" for month in range(1, 9) for day in range(1, 6))
    assert len(set(dates.split())) == 40, "构造前提：40 个互异日期"

    result = check_completeness("与本段无关的笔记正文。", dates)

    assert result["checked"] == _MAX_CHECK_ITEMS
    assert 0 < result["checked"] < 40, "截断必须真发生（不是把整段全核对）"
    assert len(result["missing"]) == result["checked"], "笔记不含任何日期 ⇒ 被核对项全部 missing"
    assert {item["kind"] for item in result["missing"]} == {"date"}
    assert len({item["item"] for item in result["missing"]}) == result["checked"], "核对项互异"


def test_c5_empty_source_has_nothing_to_check():
    """空源正文 ⇒ 没有可核对项：`checked == 0`、`missing == []`、`complete=True`（不编造）。"""
    result = check_completeness("随便一段笔记正文", "")

    assert result == {"complete": True, "checker": "regex", "checked": 0, "missing": []}
