"""解析：txt / md / pdf / 空文件 / 扫描版 / 不支持的格式。（老仓库 test_parsing.py 对拍）"""

import pytest

from app.core.errors import DocumentParseError
from app.pipeline.parsing import SCANNED_PDF_HINT, parse_document
from tests.pipeline.conftest import make_pdf


def test_txt_and_md():
    assert parse_document("a.txt", "第一行\n第二行".encode("utf-8")) == "第一行\n第二行"
    assert "标题" in parse_document("b.md", "# 标题\n正文".encode("utf-8"))


def test_gbk_fallback():
    assert "中文" in parse_document("c.txt", "中文内容".encode("gb18030"))


def test_pdf_with_text(pdf_with_text):
    text = parse_document("doc.pdf", pdf_with_text)
    assert "Zhilan" in text


def test_scanned_pdf_reports_readable_error(scanned_pdf):
    with pytest.raises(DocumentParseError) as excinfo:
        parse_document("scan.pdf", scanned_pdf)
    assert str(excinfo.value) == SCANNED_PDF_HINT


def test_empty_file_reports_readable_error():
    with pytest.raises(DocumentParseError) as excinfo:
        parse_document("empty.txt", b"")
    assert "为空" in str(excinfo.value)


def test_unsupported_suffix():
    # 上传格式扩展后 docx / xlsx / xls / pptx / csv 均已支持，换真正不认识的扩展名
    with pytest.raises(DocumentParseError) as excinfo:
        parse_document("a.exe", b"whatever")
    assert "暂不支持" in str(excinfo.value)


def test_broken_pdf_reports_readable_error():
    with pytest.raises(DocumentParseError):
        parse_document("broken.pdf", b"%PDF-1.4 not really a pdf")


def test_make_pdf_helper_is_valid():
    assert parse_document("x.pdf", make_pdf("hello")).startswith("hello")
