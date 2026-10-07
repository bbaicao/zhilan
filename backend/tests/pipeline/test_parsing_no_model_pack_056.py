"""`回归修复` 回归钉：MinerU 开着但**模型包 / 解析器缺失**时，因果必须指向模型包。

## 缺陷（2026-09-22 · 工具调用前置修复计划 §P1-2 登记）

`app/pipeline/parsing.py::_parse_pdf_document` 原先对「MinerU 不可用」与「这份 PDF 真的没有文本层」
**不区分**，两条路都落到 :data:`SCANNED_PDF_HINT`（「请上传文字版或 txt/md」）⇒ 用户导入模型包
却没生效（6.5/6.6 之后桌面默认 ``MINERU_ENABLE=True``）时，会被指引去换文件 —— **因果指错**。

## 修法与判据（对照实验）

- 开、不可用 ⇒ :data:`NO_MODEL_PACK_HINT`（含真因 ``reason``）；
- 开、可用但 MinerU 失败 ⇒ **原文案逐字不变**（:data:`SCANNED_PDF_HINT`）；
- 关（默认档）⇒ **原文案逐字不变**。
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.errors import DocumentParseError
from app.pipeline import mineru, parsing


def _force_empty_text(monkeypatch):
    """把 PDF 快提那一步压成「一个字都没提出来」（只替换两个私有纯函数，不碰真 pypdf）。"""
    monkeypatch.setattr(parsing, "_pdf_raw_pages", lambda _data: [""])
    monkeypatch.setattr(parsing, "_pdf_text", lambda _pages: "")


def test_no_model_pack_points_at_the_model_pack(monkeypatch):
    """① 开 + 不可用 ⇒ 中文、指向模型包、带上真因；**不是**扫描件提示。"""
    _force_empty_text(monkeypatch)
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(
        mineru, "available", lambda: (False, "权重缺失 model.gguf / mmproj.gguf（目录 D:/Zhilan/models）")
    )

    with pytest.raises(DocumentParseError) as excinfo:
        parsing._parse_pdf_document(b"%PDF-1.4 stub")

    message = str(excinfo.value)
    assert message != parsing.SCANNED_PDF_HINT, "不能再用扫描件提示把用户引去换文件"
    assert message == parsing.NO_MODEL_PACK_HINT.format(
        reason="权重缺失 model.gguf / mmproj.gguf（目录 D:/Zhilan/models）"
    )
    assert "模型包" in message and "导入模型包" in message


def test_with_model_pack_keeps_the_scanned_hint_verbatim(monkeypatch):
    """② 开 + 可用（MinerU 只是解析失败）⇒ 回落 pypdf 后仍无文本 ⇒ ==原文案逐字==。"""
    _force_empty_text(monkeypatch)
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(mineru, "available", lambda: (True, "MinerU 可用"))
    monkeypatch.setattr(
        mineru, "parse_pdf", lambda _data: (_ for _ in ()).throw(DocumentParseError("MinerU 解析失败：stub"))
    )

    with pytest.raises(DocumentParseError) as excinfo:
        parsing._parse_pdf_document(b"%PDF-1.4 stub")

    assert str(excinfo.value) == parsing.SCANNED_PDF_HINT


def test_mineru_off_keeps_the_scanned_hint_verbatim(monkeypatch):
    """③ 默认档（关）⇒ 行为与升级前逐字一致，且**不问** ``available()``。"""
    _force_empty_text(monkeypatch)
    monkeypatch.setattr(settings, "MINERU_ENABLE", False)
    monkeypatch.setattr(
        mineru, "available", lambda: (_ for _ in ()).throw(AssertionError("默认档不该问 available()"))
    )

    with pytest.raises(DocumentParseError) as excinfo:
        parsing._parse_pdf_document(b"%PDF-1.4 stub")

    assert str(excinfo.value) == parsing.SCANNED_PDF_HINT
