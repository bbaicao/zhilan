"""``回归修复`` 判据载体：导出 frontmatter 的 ``_yaml_scalar`` 判据**补 \\r**。

**缺陷**：``app/export/markdown.py:83`` 的转义判据只列了 ``\n``、**没有** ``\r``。笔记标题 / 分类
含**中间**裸 CR（API 侧建笔记不做 CR 过滤）时会**不加引号**原样写进 ``title:`` 行；YAML 1.1 把
孤立 CR 当**行分隔**（行终止符 = ``\n`` / ``\r\n`` / ``\r``）⇒ ``safe_load`` 侧劈行、
元数据解析错乱或整块失败。首 / 尾 CR 本来就被 ``text != text.strip()`` 兜住，**只有中间 CR 在窗外**。

**修法**：判据补 ``\r``（``json.dumps`` 把它转成 ``\r`` 两个字符落在双引号内 ⇒ frontmatter 仍单行）。
**只动判据**，``_frontmatter`` 与「含特殊字符 ⇒ ``json.dumps``」的形状未动。

**审计加固（136-1，2026-09-28 就地扩面）**：TAB / 其余 C0 / DEL / C1 / NEL / LS / PS /
U+FFFE-FFFF **同样**会让 ``safe_load`` 报错（TAB 与 NEL/LS/PS 是 ``ScannerError``，其余
``ReaderError``）⇒ 导出后再入库（产品自己的 ``pipeline/frontmatter.py`` 也用 ``safe_load``）
会让**整块元数据解析失败**。修法 = 判据换成一条覆盖全类的字符类 + 对
``json.dumps(ensure_ascii=False)`` 不转义的子集改走 ASCII 转义（详见 ``app/export/markdown.py``
的 ``_YAML_QUOTE_RE`` / ``_YAML_ASCII_ESCAPE_RE`` 注释）。

**本文件钉九条腿**：
1. ``test_yaml_scalar_quotes_interior_cr`` —— 中间 CR 被转义（**回退击杀的判决腿**）；
2. ``test_exported_file_has_no_bare_cr`` —— 真写盘：文件里**没有裸 CR**、title 仍占**一行**、
   ``yaml.safe_load`` 读回原值（同时回答「``write_text`` 会不会把 CR 翻译掉」这个坑）；
3. ``test_no_cr_output_is_byte_identical`` —— 🟢 **负控**（无 CR 输出**逐字节不变** +
   判据**没有**被过度扩大：无特殊字符仍不加引号）；
4. ``test_product_reader_round_trips_cr_category`` —— 往返腿：走**本仓自己的读取器**
   （``app/pipeline/frontmatter.parse_frontmatter``）解析导出文本 ⇒ 零警告 + 值原样读回；
5. ``test_export_markdown_keeps_cr_out_of_the_file`` —— 端到端（真 DB + 真写盘 + 公共入口
   ``export_markdown``）；
6. ``test_document_path_is_also_covered`` —— 文档那条路（``_document_text``）同一判据、同样覆盖；
7. ``test_control_chars_are_quoted_and_escaped`` —— 加固腿：TAB / 其余 C0 代表 / DEL / C1 / NEL /
   LS / PS / 非字符**逐类**往返（每类都断「raw 字符不得留在输出里」+「safe_load 原样读回」）；
8. ``test_control_char_export_round_trips_through_product_reader`` —— 真写盘 + 产品自己的
   ``parse_frontmatter`` 往返（TAB / NEL 两类，即 136-1 报告的原始复现链）；
9. ``test_value_shape_ambiguity_is_registered`` —— **现状钉**：值形态歧义（``123`` / ``true`` /
   ``2026-01-01`` …）**本条不修**，只登记（不得假装已修）。

✅ **已覆盖（实测枚举，不是声称）**：探针 ``probe136_sweep.py`` 对 **BMP 全扫 63,488 码点 +
星体面抽样 10,811 码点** 逐个断「加引号后 ``safe_load`` 能原样读回」——**改前实测 67 个失败**
（U+0000–U+0008 / U+0009 TAB / U+000B / U+000C / U+000E–U+001F / U+007F / U+0080–U+009F /
U+2028 / U+2029 / U+FFFE / U+FFFF），**改后 0 个失败**。

⚠️ **仍未覆盖（留名，⛔ 不得写「任何字符都能原样读回」）**：① 代理区 U+D800–U+DFFF（写盘前先
``UnicodeEncodeError``，进不了文件）；② 非 BMP 的非字符（PyYAML 读入期不拒，实测 0 失败）；
③ **值形态歧义**（第 ⑨ 腿钉现状，本条不修 —— 属「加引号策略」的另一根轴）。
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import text

from app.export.markdown import (
    _document_text,
    _note_text,
    _write_text,
    _yaml_scalar,
    export_markdown,
)
from app.models import Note
from app.pipeline.frontmatter import parse_frontmatter

CR = "\r"


def _stub(title="", category="", content="", tags=None, created_at=None, source_document_id=None):
    """``_note_text`` 只读这几个属性 ⇒ 用哑对象即可（不必每腿都落库）。"""
    return SimpleNamespace(
        title=title,
        category=category,
        content=content,
        tags=tags,
        created_at=created_at,
        source_document_id=source_document_id,
    )


def _frontmatter_block(raw: str) -> tuple[str, list[str]]:
    """取导出文本的 frontmatter 块（行尾统一成 ``\\n`` 之后再切，返回 (块文本, 全部行)）。"""
    lines = raw.replace("\r\n", "\n").split("\n")
    assert lines[0] == "---", raw[:80]
    end = lines.index("---", 1)
    return "\n".join(lines[1:end]), lines


# ── 1 · 判决腿：中间 CR 被转义 ────────────────────────────────────────────────


def test_yaml_scalar_quotes_interior_cr():
    """🔴 **回归修复 的判决腿**：中间带 CR 的值必须走 ``json.dumps``（引号 + ``\\r`` 转义）。

    回退击杀口径：把判据里的 ``\\r`` 摘掉 ⇒ 本腿**必须红**（实测回退后第一次断言即红：
    ``_yaml_scalar("abc\\rdef")`` 返回裸 CR 的原文、``"\\r" not in out`` 失败）。
    """
    value = "abc" + CR + "def"
    out = _yaml_scalar(value)
    assert CR not in out, "中间 CR 原样穿出 ⇒ YAML 侧会把这一行劈成两行"
    assert out == json.dumps(value, ensure_ascii=False)
    assert yaml.safe_load(out) == value

    # 三种位置都过一遍（首/尾本来就走 ``text != text.strip()``，这条腿把它们一起钉住）
    for sample in (CR + "开头", "结尾" + CR, "甲" + CR + "乙", CR, "a" + CR + "b:c", "x" + CR + "y\nz"):
        scalar = _yaml_scalar(sample)
        assert CR not in scalar, repr(sample)
        assert yaml.safe_load(scalar) == sample, repr(sample)


# ── 2 · 真写盘：文件里不许有裸 CR ─────────────────────────────────────────────


def test_exported_file_has_no_bare_cr(tmp_path):
    """真写盘（``_write_text``）：CR 必须是被**转义**，不是被放行、也不是被翻译。

    本腿同时回答任务卡里的坑 ②：``Path.write_text`` 走 ``open(newline=None)`` ——
    它只把 ``\\n`` 翻成 ``\\r\\n``（本机 Windows），**孤立 CR 原样放行**。
    所以断言必须先把合法行尾 ``\\r\\n`` 剥掉，再看还剩不剩 ``\\r``。
    """
    note = _stub(title="标题" + CR + "副标题", category="工作", content="正文第一行\n第二行\n")
    target = tmp_path / "note.md"
    _write_text(target, _note_text(note))

    raw = target.read_bytes().decode("utf-8")
    assert CR not in raw.replace("\r\n", ""), "文件里出现裸 CR ⇒ frontmatter 不是单行了"

    block, lines = _frontmatter_block(raw)
    assert sum(1 for line in lines if line.startswith("title:")) == 1
    assert sum(1 for line in lines if line.startswith("category:")) == 1
    data = yaml.safe_load(block)
    assert data["title"] == "标题" + CR + "副标题"
    assert data["category"] == "工作"


# ── 3 · 🟢 负控：无 CR 输出逐字节不变 ─────────────────────────────────────────


def test_no_cr_output_is_byte_identical():
    """🟢 **负控腿（回退击杀时本腿必须仍绿）**：判据补 ``\\r`` **不得**改动无 CR 的输出。

    两条一起钉：① 无 CR 笔记的导出文本**逐字节等于**手写期望；② 判据没有被过度扩大 ——
    普通中文值仍**不加引号**，而既有特殊字符（``:`` / 首尾空白）仍加引号。
    """
    note = _stub(title="工作 笔记", category="工作", content="正文第一行\n第二行\n")
    expected = "---\ntitle: 工作 笔记\ncategory: 工作\n---\n正文第一行\n第二行\n"
    assert _note_text(note) == expected

    assert _yaml_scalar("工作") == "工作"
    assert _yaml_scalar("工作 笔记") == "工作 笔记"
    assert _yaml_scalar("2026-01") == "2026-01"
    # 既有特殊字符行为逐字不动
    assert _yaml_scalar("a:b") == json.dumps("a:b", ensure_ascii=False)
    assert _yaml_scalar(" 前导空格") == json.dumps(" 前导空格", ensure_ascii=False)
    assert _yaml_scalar("") == json.dumps("", ensure_ascii=False)


# ── 4 · 往返腿：本仓自己的读取器 ─────────────────────────────────────────────


def test_product_reader_round_trips_cr_category():
    """往返：导出文本 → ``parse_frontmatter``（产品自己的读取器）⇒ **零警告** + 值原样读回。

    ``category`` 是 ``parse_frontmatter`` 认的键（``title`` 是未知键、被静默忽略 ⇒ 用 category 断往返）。
    """
    note = _stub(title="标题" + CR + "副标题", category="甲" + CR + "乙", content="正文")
    parsed = parse_frontmatter(_note_text(note))
    assert parsed.present is True
    assert parsed.warnings == [], parsed.warnings
    assert parsed.category == "甲" + CR + "乙"
    assert parsed.body == "正文"


# ── 5 · 端到端：真 DB + 真写盘 + 公共入口 ────────────────────────────────────


@pytest_asyncio.fixture
async def _clean_notes(db):
    """``notes`` 表不在 conftest 的清理序里（照 ``tests/pipeline/test_code_lines_t3_2_4.py:38-44`` 的写法）。"""
    await db.execute(text("DELETE FROM notes"))
    await db.commit()
    yield


@pytest.mark.asyncio
async def test_export_markdown_keeps_cr_out_of_the_file(db, tmp_path, _clean_notes):
    """端到端：含 CR 的笔记落库 → ``export_markdown`` → 磁盘上的文件单行 + 可解析读回原值。"""
    db.add(
        Note(
            title="标题" + CR + "副标题",
            content="# 正文\n",
            category="甲" + CR + "乙",
            tags=None,
            kb_id=1,
        )
    )
    await db.commit()

    result = await export_markdown(db, tmp_path, include_documents=False)
    assert result.notes_exported == 1, result
    assert result.errors == [], result.errors

    files = list(Path(result.output_dir).rglob("*.md"))
    assert len(files) == 1, files
    raw = files[0].read_bytes().decode("utf-8")
    assert CR not in raw.replace("\r\n", "")

    block, lines = _frontmatter_block(raw)
    assert sum(1 for line in lines if line.startswith("title:")) == 1
    data = yaml.safe_load(block)
    assert data["title"] == "标题" + CR + "副标题"
    assert data["category"] == "甲" + CR + "乙"

    parsed = parse_frontmatter(raw)
    assert parsed.warnings == [], parsed.warnings
    assert parsed.category == "甲" + CR + "乙"


# ── 6 · 文档那条路 ───────────────────────────────────────────────────────────


def test_document_path_is_also_covered():
    """``_document_text`` 与 ``_note_text`` 共用 ``_yaml_scalar`` ⇒ 文件名里的 CR 同样不得穿出。"""
    document = SimpleNamespace(filename="a" + CR + "b.py", category="x" + CR + "y", tags=None, created_at=None)
    text_out = _document_text(document, "内容")
    block, lines = _frontmatter_block(text_out)
    assert CR not in text_out.replace("\r\n", "")
    assert sum(1 for line in lines if line.startswith("title:")) == 1
    data = yaml.safe_load(block)
    assert data["title"] == "a" + CR + "b"
    assert data["source_file"] == "a" + CR + "b.py"
    assert data["category"] == "x" + CR + "y"


# ── 7 · 控制字符全类（审计加固 136-1） ───────────────────────────────────────

#: 每类挑代表：C0（TAB / NUL / VT / FF / ESC / BEL）、DEL、C1、NEL、LS、PS、非字符。
#: 改前实测（BMP 全扫 67 个失败码点）：TAB 与 NEL/LS/PS ⇒ ``ScannerError``；其余 ⇒ ``ReaderError``。
_CONTROL_SAMPLES = [
    ("TAB", "\t"),
    ("NUL", "\x00"),
    ("VT", "\x0b"),
    ("FF", "\x0c"),
    ("ESC", "\x1b"),
    ("BEL", "\x07"),
    ("DEL", "\x7f"),
    ("C1-86", "\x86"),
    ("NEL", "\x85"),
    ("LS", "\u2028"),
    ("PS", "\u2029"),
    ("FFFE", "\ufffe"),
    ("FFFF", "\uffff"),
]


def test_control_chars_are_quoted_and_escaped():
    """加固腿：上面每一类都必须**被转义**（加引号 + raw 字符不留在输出里），且 ``safe_load`` 原样读回。

    回退击杀口径：回到「只列 ``\n`` / ``\r``」的判据 ⇒ 这些样本**不加引号** ⇒ 第一次断言即红。
    """
    for name, ch in _CONTROL_SAMPLES:
        value = "a" + ch + "b"
        out = _yaml_scalar(value)
        assert out != value, f"{name} 未被转义（原样穿出）"
        assert ch not in out, f"{name} 的 raw 字符仍留在输出里：{out!r}"
        loaded = yaml.safe_load("k: " + out)
        assert loaded == {"k": value}, f"{name} 读回不一致：{loaded!r}"


def test_control_char_export_round_trips_through_product_reader(tmp_path):
    """真写盘 + **产品自己的读取器**往返：带 TAB / NEL 的分类导出后再解析，元数据必须原样回来。

    这正是 136-1 的复现链：``export_markdown`` 写出的 frontmatter 交给 ``parse_frontmatter``（入库链路）
    ⇒ 改前 ``safe_load`` 报错 ⇒ ``warnings`` 非空 + ``category`` 整个丢失。
    """
    note = _stub(title="标题\t副标题", category="甲\x85乙", content="正文")
    target = tmp_path / "ctrl.md"
    _write_text(target, _note_text(note))
    raw = target.read_bytes().decode("utf-8")

    body = raw.replace("\r\n", "")
    for name, ch in _CONTROL_SAMPLES:
        assert ch not in body, f"文件里出现 raw {name}"

    parsed = parse_frontmatter(raw)
    assert parsed.warnings == [], parsed.warnings
    assert parsed.category == "甲\x85乙"
    block, _ = _frontmatter_block(raw)
    assert yaml.safe_load(block)["title"] == "标题\t副标题"


# ── 8 · 值形态歧义（登记，不修） ─────────────────────────────────────────────


def test_value_shape_ambiguity_is_registered():
    """⚠️ **现状钉（如实登记，不得假装已修）**：plain scalar 的**类型歧义**不在本条范围。

    ``123`` / ``true`` / ``2026-01-01`` 这类值**不加引号** ⇒ ``safe_load`` 读成 int / bool / date，
    元数据取回时**类型不对**（下游判「应是文本」而丢弃）。要修得改「加引号策略」本身
    （凡形态像别的类型就加引号）⇒ **本条不扩面**，只把现状钉死，免得有人以为它已被覆盖。
    若将来决定修，本腿会先红 —— 那时把它改成往返断言即可。
    """
    for sample in ("123", "true", "1.5"):
        out = _yaml_scalar(sample)
        assert out == sample, f"{sample!r} 现在不加引号（本条不改它）"
        loaded = yaml.safe_load("k: " + out)["k"]
        assert not isinstance(loaded, str), (sample, loaded)

    out = _yaml_scalar("2026-01-01")
    assert out == "2026-01-01"
    assert type(yaml.safe_load("k: " + out)["k"]).__name__ == "date"
