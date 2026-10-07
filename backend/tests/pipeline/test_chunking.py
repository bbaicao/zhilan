"""分块：约 800 字一块、100 字重叠。（老仓库 test_chunking.py 对拍）

`chunk_text` 是**冻结基线**（与老仓库 `app/rag/chunking.py` 逐字一致）。除常规切分外，
这里额外钉住两条**边界语义**——它们曾被误改过（回归修复）：
- `size=0`：老仓库写作 `size = size or 默认值`，故 **0 回退默认块长、不抛错**（不得改成 `is None` 判断）；
- `size<0`：负数非假值，故照常走到校验并抛 `ValueError`。
"""

import pytest

from app.pipeline.chunking import chunk_text, normalize


def test_short_text_single_chunk():
    assert chunk_text("短文本", size=800, overlap=100) == ["短文本"]


def test_chunk_size_and_overlap():
    text = "".join(f"第{i}句。" for i in range(400))
    chunks = chunk_text(text, size=800, overlap=100)
    assert len(chunks) > 1
    assert all(len(chunk) <= 800 for chunk in chunks)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt[:50] in prev, "相邻块之间应当有重叠"


def test_overlap_clamped_below_size():
    text = "甲" * 100
    chunks = chunk_text(text, size=10, overlap=99)
    assert len(chunks) > 1


def test_empty_text():
    assert chunk_text("   \n  ") == []


def test_normalize_collapses_spaces():
    assert normalize("a   b\n\n\nc") == "a b\n\nc"


# ---------------------------------------------------------------------------
# 冻结语义：size 边界（回归修复 回归）
# ---------------------------------------------------------------------------


def test_size_zero_falls_back_to_default_not_raises():
    """`size=0` 必须回退默认块长（老仓库 `size or 默认值` 语义），**不得**抛错。

    回归：曾把 `size or settings.RAG_CHUNK_SIZE` 改成 `size is None` 判断，导致 `size=0`
    由「回退默认」变成「抛 ValueError」，改变了冻结基线的可观察行为。
    """
    text = "".join(f"第{i}句。" for i in range(400))
    assert chunk_text(text, size=0) == chunk_text(text, size=None)
    # 且确实是「按默认块长切」而不是「整篇一块」
    assert len(chunk_text(text, size=0)) > 1


def test_negative_size_still_raises():
    """负数非假值，照常走到校验并抛错（与老仓库一致）。"""
    with pytest.raises(ValueError):
        chunk_text("任意文本", size=-5)

