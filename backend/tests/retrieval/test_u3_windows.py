"""U3：窗口保留必要证据、来源坐标和统一正文预算。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Chunk, Note
from app.retrieval import keyword, windows
from app.retrieval.types import Material, render_materials
from tests.retrieval.conftest import seed_document


async def document_hits(db, bodies, *, offsets=None, **kwargs):
    document = await seed_document(db, bodies, start_ordinal=0, **kwargs)
    chunks = (await db.execute(
        select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ordinal)
    )).scalars().all()
    if offsets is not None:
        for row, start in zip(chunks, offsets):
            row.start_offset = start
            row.end_offset = start + len(row.content)
        await db.commit()
    return [
        Material(
            chunk_id=row.id, document_id=document.id, content=row.content,
            filename=document.filename, kb_id=document.kb_id,
            block_ordinal=row.ordinal, page=row.page, score=1.0,
        )
        for row in chunks
    ]


def test_overlong_single_body_keeps_middle_focus_and_true_coordinates():
    body = "前置背景" * 1600 + "needle_evidence：保修期限为三年。" + "结尾背景" * 1600
    snippet, start, end = windows.focus_window(body, "needle_evidence", 4000)

    assert "needle_evidence：保修期限为三年。" in snippet
    assert snippet == body[start:end] and 0 < start < end < len(body)
    assert len(snippet) <= 4000


@pytest.mark.asyncio
@pytest.mark.parametrize("structure", ["code", "table"])
async def test_neighbor_window_recovers_structural_evidence_without_repeating_overlap(db, structure):
    if structure == "code":
        bodies = [
            "定价示例：\n```python\n    def quote(price):\n",
            "    def quote(price):\n        return price * 1.2\n",
            "        return price * 1.2\n```\n结果为含税价。",
        ]
        expected = "定价示例：\n```python\n    def quote(price):\n        return price * 1.2\n```\n结果为含税价。"
        anchor = "return price"
        overlap = "        return price * 1.2"
    else:
        bodies = [
            "| 型号 | 期限 |\n| --- | --- |\n| AX-01 | 一年 |\n",
            "| AX-01 | 一年 |\n| AX-02 | 三年 |\n",
            "| AX-02 | 三年 |\n| AX-03 | 五年 |\n表注：期限从交付日起。",
        ]
        expected = "| 型号 | 期限 |\n| --- | --- |\n| AX-01 | 一年 |\n| AX-02 | 三年 |\n| AX-03 | 五年 |\n表注：期限从交付日起。"
        anchor = "AX-02"
        overlap = "| AX-02 | 三年 |"
    offsets = [expected.index(body) for body in bodies]
    hits = await document_hits(db, bodies, offsets=offsets)

    result = await windows.expand_windows(db, [hits[1]], anchor)

    assert len(result) == 1 and result[0].content == expected
    assert result[0].content.count(overlap) == 1
    assert result[0].covered_ordinals == (0, 1, 2)
    assert (result[0].span_start, result[0].span_end) == (0, len(expected))
    assert expected in render_materials(result)


@pytest.mark.asyncio
async def test_neighboring_hits_merge_without_losing_two_distinct_answers(db):
    bodies = ["合同背景。", "AX-02 的保修期是三年。", "AX-02 的交付期是四周。", "补充说明。"]
    hits = await document_hits(db, bodies)

    result = await windows.expand_windows(db, [hits[1], hits[2]], "AX-02")

    assert len(result) == 1 and result[0].chunk_id == hits[1].chunk_id
    assert "保修期是三年" in result[0].content and "交付期是四周" in result[0].content
    assert result[0].covered_ordinals == (0, 1, 2, 3)


@pytest.mark.asyncio
async def test_long_note_middle_window_uses_original_span_and_note_identity(db):
    prefix = "\n".join(f"背景说明第{index}行，仅供附录阅读。" for index in range(260))
    suffix = "\n".join(f"后续说明第{index}行，不含保修证据。" for index in range(260))
    content = prefix + "\nneedlearranty 的保修期为三年。\n" + suffix
    note = Note(title="长笔记", content=content, kb_id=4)
    db.add(note)
    await db.commit()
    scope = {"kb_ids": [4], "source_types": ["note"]}
    hits = await keyword.search_keywords(db, "needlearranty", scope, limit=1)
    assert len(hits) == 1 and hits[0].span_start > 0

    result = await windows.expand_windows(db, hits, "needlearranty", scope)

    assert len(result) == 1 and "needlearranty 的保修期为三年。" in result[0].content
    assert result[0].source_ref == ("note", note.id)
    assert result[0].chunk_key == f"kb:4:note:{note.id}:0"
    assert result[0].content == content[result[0].span_start:result[0].span_end]
    assert len(result[0].content) <= 4000 and result[0].span_start > 0


@pytest.mark.asyncio
async def test_window_keeps_shared_scope_current_version_and_format(db):
    bodies = ["  共享范围的说明。\n\n", "```python\n    alpha = 1\n\n    beta = 2\n```\n", "\n\n  后续说明。"]
    hits = await document_hits(
        db, bodies, kb_id=4, tags=["Allowed"], filename="共享.md",
    )
    # 已知命中与邻域使用同一current构建，不读旧版本同序号的正文。
    old = Chunk(
        document_id=hits[0].document_id, build_id="old", ordinal=0,
        content="SECRET_OLD_VERSION",
    )
    db.add(old)
    await db.commit()
    scope = {"kb_ids": [4, 5], "source_types": ["document"], "tags": ["Allowed"]}
    snapshot = {key: list(value) for key, value in scope.items()}

    result = await windows.expand_windows(db, [hits[1]], "alpha", scope)

    assert scope == snapshot and result[0].kb_id == 4
    assert "SECRET_OLD_VERSION" not in result[0].content
    assert "    alpha = 1\n\n    beta = 2" in result[0].content
    assert result[0].content.strip("\r\n") in render_materials(result)
    assert result[0].covered_ordinals == (0, 1, 2)


@pytest.mark.asyncio
async def test_all_candidate_focuses_fit_item_and_total_budgets_without_losing_hits():
    items = [
        Material(
            chunk_id=-index - 1, document_id=0, url=f"https://example.invalid/{index}",
            content=("前置背景" * 1700) + f"needle_evidence_{index}" + ("结尾背景" * 1700),
        )
        for index in range(16)
    ]

    result = await windows.expand_windows(object(), items, "needle_evidence")

    assert len(result) == 16
    assert all(f"needle_evidence_{index}" in item.content for index, item in enumerate(result))
    assert all(len(item.content) <= 4000 for item in result)
    assert sum(len(item.content) for item in result) <= 48_000


@pytest.mark.asyncio
async def test_known_nonoverlapping_identical_blocks_keep_both_real_occurrences(db):
    body = "重复签字条款：每份合同均须单独签字。\n"
    hits = await document_hits(db, [body, body], offsets=[0, len(body)])

    result = await windows.expand_windows(db, [hits[0]], "签字条款")

    assert len(result) == 1 and result[0].content.count("重复签字条款") == 2
    assert result[0].span_start == 0 and result[0].span_end == 2 * len(body)
    assert result[0].content == body + body
