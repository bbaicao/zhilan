"""回归检查 · 纯文本直读 + 编码探测（工具升级-3 · 阶段 2 · 第 2 笔）的判据载体。

交付面（包内 `D-回归检查` / `D-回归检查` / `D-回归检查`）：
- `CODE_SUFFIXES`（29 项），且是 `SUPPORTED_SUFFIXES` 的**真子集**；
- 这 29 个后缀走纯文本分支：不进 `_PARSERS`、不送 MinerU、整份扫空字节；
- `detect_encoding()` 与 `_decode` 共用严格检查，保留合法文本编码；
- 解码后含 `\x00` ⇒ `DocumentParseError(BINARY_HINT)`，合法宽字符 BOM 不误拒；
- BOM 只剥一个（否则阶段 2 的 `ast.parse` 会失败、静默退化）。

⚠️ **本笔不建 `0006`、不写数据库列**：编码名只到 `Prepared`（`回归检查` 那一笔才落库，见 `D-回归检查`）。
⚠️ **`_decode` 本体逐字不动**（改它会连带打掉既有 txt/md 的编码行为）—— 本文件有专条钉住。
"""

import inspect

import pytest

from app.core.errors import DocumentParseError
from app.pipeline.chunking import CODE_WINDOW_LINES, chunk_code
from app.pipeline.parsing import (
    CODE_SUFFIXES,
    SUPPORTED_SUFFIXES,
    _decode,
    detect_encoding,
    parse_document,
    parse_document_pages,
)


def test_code_suffixes_is_a_proper_subset():
    """29 项 + 真子集（不是「等于」——等于就意味着把 15 个文档后缀也吞进代码分支了）。"""
    assert len(CODE_SUFFIXES) == 29
    assert CODE_SUFFIXES < SUPPORTED_SUFFIXES
    assert len(SUPPORTED_SUFFIXES - CODE_SUFFIXES) == 15


def test_code_suffixes_excludes_all_original_document_suffixes():
    """原来的 15 个文档后缀一个都不许进代码集合（进了就改了既有格式的解析路径）。"""
    doc = {
        ".txt", ".md", ".markdown", ".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".csv",
        ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp",
    }
    assert CODE_SUFFIXES & doc == frozenset()
    assert doc <= SUPPORTED_SUFFIXES


def test_detect_encoding_order_matches_decode():
    """OPT3 共享入口行为一致；严格解码不能偷偷改成替换乱码。"""
    for data in (b"abc", "中文".encode("gb18030"), "café".encode("latin-1"),
                 "中文".encode("utf-16"), "中文".encode("utf-32")):
        assert _decode(data) == data.decode(detect_encoding(data))
    # 「不引入 errors=」要断**可执行体**而不是整个源码文本 —— 两个函数的 docstring 里
    # 都**明写**着「不引入 errors=\"replace\"」这条口径，扫全文必然假红。
    # 做法：按 **AST** 取函数体里所有 Call 的 keywords，逐个数有没有 errors=。
    import ast as _ast

    for func in (detect_encoding, _decode):
        tree = _ast.parse(inspect.getsource(func))
        calls = [n for n in _ast.walk(tree) if isinstance(n, _ast.Call)]
        for call in calls:
            kws = {kw.arg for kw in call.keywords}
            assert "errors" not in kws, (func.__name__, _ast.dump(call)[:120])


def test_detect_encoding_returns_the_hit_name():
    assert detect_encoding("abc".encode("utf-8")) == "utf-8"
    assert detect_encoding("\u4e2d\u6587\u6ce8\u91ca".encode("gb18030")) == "gb18030"
    assert detect_encoding("café".encode("latin-1")) == "latin-1"
    with pytest.raises(DocumentParseError):
        detect_encoding(b"\xff\xfe\xfd")


def test_code_file_keeps_indentation():
    """A13a 的「缩进还在」腿：代码直读**不得**过 `clean()`（清洗会剥行首缩进）。"""
    src = "def f():\n    if x:\n        return 1\n"
    text, pages = parse_document_pages("a.py", src.encode("utf-8"))
    assert text == src, "缩进/换行必须原样保留"
    assert pages == []


def test_code_branch_is_used_for_all_29_suffixes():
    """29 个后缀逐项走同一条分支（不是只给 `.py` 开了口子）。"""
    body = "x = 1\n"
    for suffix in sorted(CODE_SUFFIXES):
        text, pages = parse_document_pages(f"a{suffix}", body.encode("utf-8"))
        assert text == body, suffix
        assert pages == [], suffix


def test_null_byte_is_rejected_for_code_suffixes():
    """`D-回归检查` 的二进制闸：**整份**扫空字节（不是只看前 N KB）。"""
    payload = b"x = 1\n" + b"a" * 3000 + b"\x00" + b"y = 2\n"
    with pytest.raises(DocumentParseError) as excinfo:
        parse_document_pages("a.py", payload)
    assert "空字节" in str(excinfo.value)


def test_document_suffixes_do_not_get_the_null_gate():
    """OPT3 明确收紧文本准入；真实 NUL 拒绝，编码 NUL 由 BOM 正控覆盖。"""
    with pytest.raises(DocumentParseError):
        parse_document("a.txt", b"a\x00b")


def test_bom_is_stripped_exactly_once():
    """BOM 必须被剥掉：留着会让阶段 2 的 `ast.parse` 解析失败、静默退化成行窗切块。"""
    text, _ = parse_document_pages("a.py", "\ufeffx = 1\n".encode("utf-8"))
    assert not text.startswith("\ufeff")
    assert text == "x = 1\n"
    # utf-8-sig 编码的字节，BOM 由解码器吃掉
    text2, _ = parse_document_pages("b.py", "y = 2\n".encode("utf-8-sig"))
    assert text2 == "y = 2\n"
    # 只许剥一个：正文里真的以 \ufeff 开头时，第二个必须留下（不得用 lstrip 全剥）
    text3, _ = parse_document_pages("c.py", "\ufeff\ufeffz = 3\n".encode("utf-8"))
    assert text3 == "\ufeffz = 3\n"


def test_code_suffix_does_not_enter_parsers_or_mineru():
    """这 29 个后缀**不进** `_PARSERS`（注册表里必须零命中）。

    进了注册表 = 走了 Office/图片那条路，与「纯文本直读」是两回事。
    """
    from app.pipeline.parsing import _PARSERS

    assert set(_PARSERS) & CODE_SUFFIXES == set()


def test_unbounded_preamble_is_windowed():
    """🔴 **独立复核 B-1（BLOCKER）回归钉**：前言 / 空档块**也**必须按行窗切开。

    首版只对**符号块**做二次行窗，前言/空档/尾巴是无界 append ⇒ 实测产出过 **200 行 / 1983 字**
    的单块（`RAG_CHUNK_SIZE` = 800 的 2.5 倍），且直接进向量化。
    `D-回归检查` 第 6 条的「**单块**超过 80 行时只在该块内部再按行窗切开」覆盖**每一个块**。
    """
    nl = "\n"
    preamble = "".join("x" + str(i) + " = " + str(i) + nl for i in range(200))
    src = preamble + "def a():" + nl + "    pass" + nl + nl + "def b():" + nl + "    pass" + nl
    chunks = chunk_code(src)
    widths = [c.line_end - c.line_start + 1 for c in chunks]
    assert max(widths) <= CODE_WINDOW_LINES, widths
    # 前言必须被切成多块（不是一整块 200 行）
    preamble_blocks = [c for c in chunks if c.symbol_path is None]
    assert len(preamble_blocks) >= 3, [c.line_start for c in preamble_blocks]
    # 符号块仍带符号名
    assert "a" in {c.symbol_path for c in chunks}


def test_long_gap_between_symbols_is_windowed():
    """空档同理：符号之间 199 行空档不得产出无界块（B-1 的第二处无界 append）。"""
    nl = "\n"
    src = "def a():" + nl + "    pass" + nl + (nl * 200) + "def b():" + nl + "    pass" + nl
    chunks = chunk_code(src)
    assert max(c.line_end - c.line_start + 1 for c in chunks) <= CODE_WINDOW_LINES


def test_empty_code_file_reports_readable_error():
    with pytest.raises(DocumentParseError) as excinfo:
        parse_document_pages("a.py", b"   \n\n  ")
    assert "为空" in str(excinfo.value)
