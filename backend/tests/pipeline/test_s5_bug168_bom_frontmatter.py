r"""`回归修复` 判据载体 —— 带 UTF-8 BOM 的 `.md` 不得让 frontmatter 静默失效。

**BOM 与 frontmatter 的边界**：
`_decode`（`backend/app/pipeline/parsing.py:113-119`）的尝试顺序是
`utf-8 → utf-8-sig → gb18030 → latin-1`，**`utf-8` 先命中** ⇒ BOM 以 `\ufeff` 留在文本里；
`prepare_document`（`backend/app/pipeline/ingest.py:96`）的**文本腿**
（`parsing.py:689` `text = _decode(data)`）改前**不剥** BOM ⇒
`parse_frontmatter`（`frontmatter.py:46-53`）首行变成 `\ufeff---` ⇒
`_OPEN_RE`（`frontmatter.py:25` `^---\s*$`）失配 ⇒ **静默回退**
（`:52-53` `return Frontmatter(body=text)` ⇒ `present=False` / `warnings=[]`）⇒
category / tags / created_at 全空且**零提示**；同时 `date_from_name`
（`frontmatter.py:186-195`）的 `stem.strip()` 清洗集**不含 `\ufeff`** ⇒ 文件名日期也取不到。

**修法（本载体钉住的最小改动）**：
① `parsing.py:689` 文本腿**局部剥至多一个**前导 BOM；② `frontmatter.py:189` 清洗集加 `\ufeff`。
⛔ **不改 `_decode` 本体**：那会同时改 `_parse_code`（`parsing.py:582` 调用、`:167-171` 自身已剥一个
⇒ 变双剥）与 `backend/app/pipeline/clean_status.py:119-120` 的「至多剥一个」口径 ⇒ 撞
`backend/tests/api/test_bug108_bom_content_stale.py` 四腿（含**两个 BOM** 边界）⇒ 扩大战场。
⛔ 不放宽 `_OPEN_RE`（把 BOM 当分隔线是错的）；⛔ 不在 decode 层清洗 / re-encode。

**腿构成（每条腿的期望值都在本文件内独立重算，不借产品函数自证）**：
① 主腿：BOM `.md` 经 `prepare_document` 的 `category`/`tags`/`created_at`/**正文**与无 BOM **逐字相同**；
② `date_from_name("\ufeff2026-07-01.md") == datetime(2026, 7, 1)`，且既有空白清洗**不得被收窄**；
③ 剥 BOM 后 `frontmatter.body` **不带**前导 `\ufeff`（BOM 不进正文 / 不进分块）；
④ 边界腿：文本腿**只剥一个**（两个 BOM ⇒ 仍留一个）· **只剥前导**（正文中间的 `\ufeff` 不动）——
   钉住与 `_parse_code` / `clean_status` 一致的「至多一个 · 仅前导」口径；
⑤ 反向保护腿（本载体自带）：代码腿 `.py`（`_parse_code`）的 BOM 口径不受本笔影响。

**🔴 未覆盖（如实登记，⛔ 不写成「已覆盖」）**：**两个 BOM** 的 frontmatter **可用性**
（腿④只钉「只剥一个」这一事实；该形态本阶段**不修**，剥一个后仍解析不了）·
非 UTF-8 编码（gb18030 / latin-1）**带** BOM 腿。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.pipeline.frontmatter import date_from_name, parse_frontmatter
from app.pipeline.ingest import prepare_document
from app.pipeline.parsing import DocumentParseError, parse_document_pages

#: UTF-8 BOM 的字符 / 字节形态（**判据侧独立副本** —— 产品侧只有字面量 `"\ufeff"`）。
BOM = "\ufeff"
BOM_BYTES = b"\xef\xbb\xbf"

#: 样本文件名**不带日期前缀** ⇒ `created_at` 只可能来自 frontmatter
#: （失配时退 `date_from_name("笔记.md")` = None）⇒ 缺陷与修复可被同一条腿区分。
_FILENAME = "笔记.md"
#: 三键齐全的 frontmatter + 两行正文（正文含空行，用来逐字对拍 `body` 拼接）。
_YAML = "---\ncategory: jia\ntags: [a, b]\ncreated_at: 2026-07-01\n---\n正文第一行\n\n正文第二行"
_EXPECTED_BODY = "正文第一行\n\n正文第二行"


def test_bom_md_meta_matches_plain_md():
    """腿①：BOM 版与无 BOM 版的 category / tags / created_at / 正文 **逐字相同**。"""
    plain = prepare_document(_FILENAME, _YAML.encode("utf-8"))
    with_bom = prepare_document(_FILENAME, BOM_BYTES + _YAML.encode("utf-8"))

    # 独立锚：无 BOM 对照件本身必须解析成功（防「两边都空」的假绿）
    assert plain.category == "jia"
    assert plain.tags == ["a", "b"]
    assert plain.created_at == datetime(2026, 7, 1)
    assert plain.meta_warnings == []

    assert with_bom.category == plain.category
    assert with_bom.tags == plain.tags
    assert with_bom.created_at == plain.created_at
    assert with_bom.meta_warnings == plain.meta_warnings == []
    # 正文也不得差字符：BOM 既不进正文，也不因失配而把元数据行留在正文里
    assert with_bom.text == plain.text
    assert with_bom.raw == plain.raw
    assert BOM not in with_bom.raw


def test_date_from_name_tolerates_leading_bom():
    """腿②：文件名回退日期对前导 BOM 免疫；既有空白清洗语义不得被收窄。"""
    assert date_from_name(BOM + "2026-07-01.md") == datetime(2026, 7, 1)
    # 反向锚：修法不得把 `.strip()` 换成更窄的字符集（空格 / 制表符仍须清掉）
    assert date_from_name(" 2026-07-01.md") == datetime(2026, 7, 1)
    assert date_from_name("\t2026-07-01.md") == datetime(2026, 7, 1)
    # 反向锚：既有负面语义不动
    assert date_from_name("没有日期的文件.md") is None
    assert date_from_name("2026-13-45-假日期.md") is None


def test_text_leg_strips_bom_so_body_and_frontmatter_are_bom_free():
    """腿③：文本腿出口无前导 BOM ⇒ `parse_frontmatter` 恢复解析、`body` 不带 BOM。"""
    text, pages = parse_document_pages(_FILENAME, BOM_BYTES + _YAML.encode("utf-8"))
    assert not text.startswith(BOM), "文本腿必须剥掉前导 BOM"
    assert pages == [], "md 无页边界信息（与既有口径一致）"

    fm = parse_frontmatter(text)
    assert fm.present is True
    assert not fm.body.startswith(BOM)
    assert fm.body == _EXPECTED_BODY
    assert fm.warnings == []


def test_text_leg_strips_at_most_one_and_only_leading_bom():
    r"""腿④（边界）：**至多一个** · **仅前导** —— 与 `_parse_code` / `clean_status` 同口径。

    ⚠️ 两个 BOM 的形态本阶段**不修**：剥一个后首行仍是 `\ufeff---` ⇒ frontmatter
    **仍不解析**。本腿钉的是「至多一个」这个口径（⛔ 防 `lstrip("\ufeff")` 式全剥），
    不是「两 BOM 也能用」。
    """
    double = parse_document_pages(_FILENAME, BOM_BYTES * 2 + _YAML.encode("utf-8"))[0]
    assert double.startswith(BOM), "只剥一个：第二个 BOM 必须留下"
    assert not double.startswith(BOM * 2)
    fm = parse_frontmatter(double)
    assert fm.present is False, "两 BOM 形态本阶段不修（挂账）"
    assert fm.category == "" and fm.warnings == []

    # 只剥前导：正文中间的 `\ufeff` 不得被误伤
    middle = ("正文" + BOM + "尾部").encode("utf-8")
    assert BOM in parse_document_pages(_FILENAME, middle)[0]


def test_code_leg_bom_handling_unchanged():
    """腿⑤（反向保护 · 本载体自带）：`.py` 走 `_parse_code` 自己剥一个 —— 本笔不得动它。"""
    prepared = prepare_document("a.py", BOM_BYTES + b"x = 1\n")
    assert prepared.source_encoding == "utf-8"
    assert not prepared.raw.startswith(BOM)
    assert prepared.raw.startswith("x = 1")


def test_bom_only_md_is_reported_as_empty():
    """腿⑥：只剩 BOM 的 `.md` ⇒ 剥完即空 ⇒ 按既有 `EMPTY_HINT` 报空（不静默产出空正文）。"""
    with pytest.raises(DocumentParseError):
        parse_document_pages(_FILENAME, BOM_BYTES)
