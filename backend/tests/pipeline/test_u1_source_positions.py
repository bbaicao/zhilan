"""U1.3：明确页边删除、原文范围及 PDF 页槽位；OCR 仅使用受控替身。"""

from io import BytesIO

import pytest
from pypdf import PdfReader
from sqlalchemy import select

from app.core.config import settings
from app.models import Chunk, Document
from app.pipeline import chunking, ingest, parsing
from app.pipeline.cleaning import clean
from tests.pipeline.conftest import make_pdf_multi


def test_same_header_text_in_body_is_kept_with_original_position(monkeypatch):
    monkeypatch.setattr(settings, "CLEAN_MIN_REPEAT_RATIO", 0.6)
    fact = "合法重复事实"
    pages = [
        f"{fact}\n第一页正文一\n第一页正文二\n第一页正文三\n第一页正文四",
        f"{fact}\n第二页正文一\n第二页正文二\n第二页正文三\n第二页正文四",
        "\n".join(["第三页正文一", "第三页正文二", "第三页正文三", "第三页正文四",
                   fact, "第三页正文五", "第三页正文六", "第三页正文七", "第三页正文八"]),
    ]
    raw, page_values = parsing._page_result(pages)
    result = clean(raw, pages=page_values)
    assert result.text.count(fact) == 1
    deleted = [item for item in result.removed_ranges if item["step"] == "header_footer"]
    assert [item["page"] for item in deleted] == [1, 2]
    assert all(raw[item["raw_start"]:item["raw_end"]].strip() == fact for item in deleted)
    for span in result.source_map:
        assert result.text[span["clean_start"]:span["clean_end"]] == raw[span["raw_start"]:span["raw_end"]]
    body_offset = raw.rfind(fact)
    assert any(span["raw_start"] <= body_offset < span["raw_end"] for span in result.source_map)
    from app.pipeline.cleaning import RULE_VERSION
    assert result.report()["rule_version"] == RULE_VERSION


def test_same_prefix_on_two_pages_uses_ranges_without_probe(monkeypatch):
    def forbidden_probe(*_args):
        raise AssertionError("page assignment must not search a text prefix")

    monkeypatch.setattr(chunking, "_assign_page", forbidden_probe)
    first = "甲" * 40 + "第一" + "乙" * 58
    second = "甲" * 40 + "第二" + "丙" * 58
    raw, pages = parsing._page_result([first, second])
    result = clean(raw, pages=pages)
    chunks = chunking.chunk_document(
        result.text, size=102, overlap=0, source_map=result.source_map, page_spans=result.page_spans,
    )
    assert [chunk.text for chunk in chunks] == [first, second]
    assert [chunk.page for chunk in chunks] == [1, 2]
    assert chunks[1].source_ranges[0]["raw_start"] == len(first) + 2


def test_cross_page_chunk_keeps_all_ranges_and_empty_page_slot():
    raw, pages = parsing._page_result(["第一页面正文" * 4, "", "第三页面正文" * 4])
    result = clean(raw, pages=pages)
    chunks = chunking.chunk_document(
        result.text, size=1000, overlap=0, source_map=result.source_map, page_spans=result.page_spans,
    )
    assert len(chunks) == 1 and chunks[0].page == 1
    assert {item["page"] for item in chunks[0].source_ranges if item["page"] is not None} == {1, 3}
    for item in chunks[0].source_ranges:
        assert raw[item["raw_start"]:item["raw_end"]] == result.text[item["canonical_start"]:item["canonical_end"]]


def test_normalization_and_control_removal_keep_cell_positions(monkeypatch):
    monkeypatch.setattr(settings, "CLEAN_TABLE_DOWNGRADE", False)
    raw = "\r\n  第一行  内容 \x00\r\n第二行\r\n"
    result = clean(raw)
    location = result.text.index("内容")
    containing = next(span for span in result.source_map if span["clean_start"] <= location < span["clean_end"])
    raw_location = containing["raw_start"] + location - containing["clean_start"]
    assert raw[raw_location:raw_location + 2] == "内容"
    assert any(raw[item["raw_start"]:item["raw_end"]] == "\x00" for item in result.removed_ranges)
    disabled = clean(raw, enable=False)
    assert disabled.text == raw
    assert disabled.source_map == [{"clean_start": 0, "clean_end": len(raw), "raw_start": 0, "raw_end": len(raw)}]


def test_unconfirmed_ocr_page_layout_returns_unknown():
    raw = "全文中只有同一个长片段，无法确认它属于扫描件的哪一页。"
    text, pages = parsing._mineru_page_result(raw, [raw], 3)
    result = clean(text, pages=pages)
    assert result.page_spans == []
    assert all(chunk.page is None for chunk in chunking.chunk_document(
        result.text, size=20, overlap=0, source_map=result.source_map, page_spans=result.page_spans,
    ))


def test_explicit_ocr_page_marker_has_direct_offsets():
    raw = "第一页\n\n<!-- PageBreak -->\n\n第二页"
    text, pages = parsing._mineru_page_result(raw, ["第一页", "第二页"], 2)
    assert [(span["page"], raw[span["raw_start"]:span["raw_end"]]) for span in pages.source_spans] == [
        (1, "第一页"), (2, "第二页"),
    ]
    assert text == raw


def test_pypdf_mixed_page_fill_preserves_real_page_number(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "MINERU_ENABLE", True)
    monkeypatch.setattr(settings, "OCR_SCAN_THRESHOLD", 0.0)
    monkeypatch.setattr(parsing.mineru, "available", lambda: (True, "controlled OCR"))

    def controlled_ocr(data):
        calls.append(len(PdfReader(BytesIO(data)).pages))
        return "SCAN PAGE TWO", ["SCAN PAGE TWO"]

    monkeypatch.setattr(parsing.mineru, "parse_pdf", controlled_ocr)
    raw, pages = parsing.parse_document_pages("mixed.pdf", make_pdf_multi(["NATIVE " * 30, "", "NATIVE PAGE THREE"]))
    assert calls == [1]
    assert [span["page"] for span in pages.source_spans] == [1, 2, 3]
    for span in pages.source_spans:
        assert raw[span["raw_start"]:span["raw_end"]] == pages[span["page"] - 1]
    result = clean(raw, pages=pages)
    ranges = chunking.chunk_document(
        result.text, size=1000, overlap=0, source_map=result.source_map, page_spans=result.page_spans,
    )[0].source_ranges
    assert any(item["page"] == 2 and "SCAN PAGE TWO" in raw[item["raw_start"]:item["raw_end"]] for item in ranges)


def test_generated_fence_context_is_outside_source_ranges():
    raw = "```py\n" + "\n".join(f"    value_{index} = 1000000000" for index in range(8)) + "\n```"
    result = clean(raw)
    chunks = chunking.chunk_document(
        result.text, size=40, overlap=0, source_map=result.source_map, page_spans=result.page_spans,
    )
    continuation = next(chunk for chunk in chunks[1:] if chunk.text.startswith("```py\n"))
    assert continuation.start_offset > len("```py\n")
    assert all(item["raw_start"] >= continuation.start_offset for item in continuation.source_ranges)
    assert all(raw[item["raw_start"]:item["raw_end"]] == result.text[item["canonical_start"]:item["canonical_end"]]
               for item in continuation.source_ranges)


@pytest.mark.asyncio
async def test_prepare_and_ingest_persist_the_same_chunk_sources(db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MINERU_ENABLE", False)
    monkeypatch.setattr(settings, "RAG_CHUNK_SIZE", 80)
    monkeypatch.setattr(settings, "RAG_CHUNK_OVERLAP", 5)
    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path))

    async def no_vectors(*_args, **_kwargs):
        return False, "受控来源检查"

    monkeypatch.setattr(ingest, "index_chunks", no_vectors)
    data = make_pdf_multi(["FIRST PAGE " * 20, "", "THIRD PAGE " * 20])
    prepared = ingest.prepare_document("sources.pdf", data)
    expected = prepared.clean_result.report()["chunk_provenance"]
    assert expected and any(3 in item["pages"] for item in expected)
    result = await ingest.ingest_document(db, "sources.pdf", data)
    doc = await db.get(Document, result.document_id)
    chunks = (await db.execute(
        select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.ordinal)
    )).scalars().all()
    assert doc.raw_content == prepared.raw
    assert doc.clean_report["chunk_provenance"] == expected
    assert [chunk.page for chunk in chunks] == [piece.page for piece in prepared.pieces]
    assert [item["ordinal"] for item in expected] == [chunk.ordinal for chunk in chunks]
    for item in expected:
        for span in item["raw_ranges"]:
            assert doc.raw_content[span["raw_start"]:span["raw_end"]] == prepared.text[
                span["canonical_start"]:span["canonical_end"]
            ]
