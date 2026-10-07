"""OPT3：真实写工具保留 BOM/字节序，并披露不能沿用旧编码的回退。"""

import codecs

import pytest

from tests.agent.test_write_encoding_t3_6 import (
    _fresh_tokens, _write_via_handler, isolate_backups,  # noqa: F401
)


@pytest.mark.parametrize("encoding,bom", [
    ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-16-be", codecs.BOM_UTF16_BE),
    ("utf-32-le", codecs.BOM_UTF32_LE), ("utf-32-be", codecs.BOM_UTF32_BE),
])
def test_write_preserves_bom_endianness(monkeypatch, tmp_path, encoding, bom):
    text = "新版中文\r\nsecond line"
    obs, ws = _write_via_handler(
        monkeypatch, tmp_path, rel="encoded.txt", content=text,
        existing=bom + "旧版中文".encode(encoding),
    )
    assert obs.ok is True and encoding in obs.text
    assert (ws / "encoded.txt").read_bytes() == bom + text.encode(encoding)


def test_rejected_binary_encoding_reports_utf8_fallback(monkeypatch, tmp_path):
    obs, ws = _write_via_handler(
        monkeypatch, tmp_path, rel="renamed.txt", content="替换正文",
        existing=b"GIF89apicture",
    )
    assert obs.ok is True and "utf-8" in obs.text and "回退" in obs.text
    assert (ws / "renamed.txt").read_bytes() == "替换正文".encode("utf-8")
