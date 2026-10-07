"""OPT3：展示补全不能作为源位移，裁剪仍保留实际命中。"""

import pytest

from app.models import Chunk, Document
from app.pipeline.chunking import chunk_document
from app.retrieval.types import Material
from app.retrieval.windows import expand_windows


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fence", "table"])
async def test_display_completion_keeps_real_source_envelope(db, kind):
    source = (
        "```python\n" + "    pad = 1\n" * 20 + "    needle = 3\n" + "    tail = 2\n" * 20 + "```"
        if kind == "fence" else
        "| 名称 | 数量 |\n| --- | --- |\n"
        + "".join(f"| row{i} | {i} |\n" for i in range(40))
        + "| needle | 999 |\n" + "| tail | 100 |\n" * 20
    )
    pieces = chunk_document(source, size=120, overlap=0)
    piece = next(p for p in pieces if "needle" in p.text and p.start_offset > 0)
    doc = Document(filename="source.md", file_type="md", index_status="ready", status="ready", raw_content=source)
    db.add(doc)
    await db.flush()
    row = Chunk(document_id=doc.id, ordinal=0, content=piece.text,
                start_offset=piece.start_offset, end_offset=piece.end_offset)
    db.add(row)
    await db.commit()
    result = await expand_windows(db, [Material(
        chunk_id=row.id, document_id=doc.id, content=row.content, filename=doc.filename
    )], "needle", limit=40)
    assert len(result) == 1 and "needle" in result[0].content
    assert len(result[0].content) <= 40
    assert (result[0].span_start, result[0].span_end) == (piece.start_offset, piece.end_offset)
    assert "needle" in source[result[0].span_start:result[0].span_end]
