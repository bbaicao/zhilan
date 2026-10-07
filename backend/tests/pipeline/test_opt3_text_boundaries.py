"""OPT3：同一共享文本入口的反例、编码兼容和结构保真。"""

import codecs

import pytest

from app.core.errors import DocumentParseError
from app.pipeline.clean_status import _read_disk_text
from app.pipeline.cleaning import clean, downgrade_tables
from app.pipeline.parsing import _decode, detect_encoding, parse_document_pages


@pytest.mark.parametrize("data", [
    b"\x89PNG\r\n\x1a\n\x00IHDR", b"PK\x03\x04archive",
    b"%PDF-1.4\nstream", b"GIF89apicture", b"ordinary\x00hidden",
    b"ordinary\x01hidden", codecs.BOM_UTF16_LE + b"x",
], ids=["png", "zip", "pdf", "gif", "nul", "control", "broken-bom"])
def test_obvious_nontext_is_rejected(data):
    for suffix in ("txt", "md", "py", "csv"):
        with pytest.raises(DocumentParseError):
            parse_document_pages(f"renamed.{suffix}", data)
    with pytest.raises(DocumentParseError):
        _decode(data)
    with pytest.raises(DocumentParseError):
        detect_encoding(data)


@pytest.mark.parametrize("encoding,text", [
    ("utf-8", "正常中文\t文字\r\n"),
    ("utf-8-sig", "正常中文\t文字\r\n"),
    ("utf-16", "正常中文\t文字\r\n"),
    ("utf-32", "正常中文\t文字\r\n"),
    ("gb18030", "正常中文\t文字\r\n"),
    ("latin-1", "caf\xe9\tCr\xe8me\r\n"),
], ids=["utf8", "utf8-bom", "utf16-bom", "utf32-bom", "gb18030", "latin1"])
def test_legal_text_roundtrip_and_disk_read(tmp_path, encoding, text):
    data = text.encode(encoding)
    for suffix in ("txt", "md", "py"):
        value, pages = parse_document_pages(f"normal.{suffix}", data)
        assert value == text and pages == []
    path = tmp_path / "normal.py"
    path.write_bytes(data)
    assert _read_disk_text(str(path), 1) == text
    csv_text, _ = parse_document_pages("normal.csv", f"name,value\n{text.strip()},2".encode(encoding))
    assert text.strip().replace("\t", " ") in csv_text or text.strip() in csv_text


def test_changed_disk_binary_is_unreadable(tmp_path):
    path = tmp_path / "external.py"
    path.write_bytes(b"GIF89achanged externally")
    assert _read_disk_text(str(path), 1) is None


@pytest.mark.parametrize("raw", [
    "这是普通正文  后面继续说明。",
    "第一句\t第二句\n第三句\t第四句",
    "- 条目一  说明\n- 条目二  说明",
    "只是一行  123",
    "这是一句普通说明。  数量\n下一句仍是正文。  123",
], ids=["prose-spaces", "prose-tabs", "list", "single-row", "sentences-with-number"])
def test_unproven_whitespace_is_not_a_table(raw):
    assert downgrade_tables(raw.splitlines())[0] == raw.splitlines()
    assert " | " not in clean(raw).text


def test_numeric_table_and_cells_keep_real_source():
    raw = "项目\t数量\n苹果\t12\n梨子\t34"
    result = clean(raw)
    assert result.text == "项目 | 数量\n苹果 | 12\n梨子 | 34"
    for span in result.source_map:
        value = result.text[span["clean_start"]:span["clean_end"]]
        original = raw[span["raw_start"]:span["raw_end"]]
        assert value == original or (value == " | " and original == "\t")
    for value in ("项目", "数量", "苹果", "12", "梨子", "34"):
        clean_start, raw_start = result.text.index(value), raw.index(value)
        assert any(
            span["clean_start"] <= clean_start < span["clean_end"]
            and span["raw_start"] + clean_start - span["clean_start"] == raw_start
            for span in result.source_map
        )


def test_explicit_markdown_and_code_stay_unchanged():
    for raw in ("| 项目 | 数量 |\n| --- | --- |\n| 苹果 | 12 |",
                "```py\nvalue = 1  # comment\n\treturn value\n```"):
        assert clean(raw).text == raw
