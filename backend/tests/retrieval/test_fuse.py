"""RRF 融合用例（阶段 3 · 条目 3.1）：分值公式、同源去重、稳定排序、截断。"""

from __future__ import annotations

import pytest

from app.retrieval.fuse import fuse
from app.retrieval.types import RRF_K, Material


def _material(chunk_id: int, **kwargs) -> Material:
    data = {"chunk_id": chunk_id, "document_id": chunk_id, "content": f"块{chunk_id}"}
    data.update(kwargs)
    return Material(**data)


def test_rrf_score_and_order():
    """``score = Σ 1/(RRF_K + rank)``：两路都命中的排在单路命中之前。"""
    vector_hits = [_material(3), _material(1)]
    keyword_hits = [_material(1), _material(2)]

    fused = fuse(vector_hits, keyword_hits)
    assert [item.chunk_id for item in fused] == [1, 3, 2]

    by_id = {item.chunk_id: item for item in fused}
    assert by_id[1].score == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    assert by_id[3].score == pytest.approx(1 / (RRF_K + 1))
    assert by_id[2].score == pytest.approx(1 / (RRF_K + 2))


def test_same_chunk_is_merged_once_and_keeps_both_signals():
    """同源去重：同一个 ``chunk_id`` 只出一条，互补字段从另一路补齐。"""
    vector_hits = [_material(5, content="", filename="", vector_score=0.91)]
    keyword_hits = [_material(5, content="向量库使用 Qdrant。", filename="手册.md", keyword_score=1.2)]

    fused = fuse(vector_hits, keyword_hits)
    assert len(fused) == 1
    only = fused[0]
    assert only.vector_score == pytest.approx(0.91)
    assert only.keyword_score == pytest.approx(1.2)
    assert only.content == "向量库使用 Qdrant。"
    assert only.filename == "手册.md"


def test_single_channel_hits_are_not_penalised():
    """单路命中按同一公式计分（缺失的一路不记 0 分惩罚）——RRF 相对加权和的理由。"""
    fused = fuse([_material(7)], [], limit=12)
    assert [item.chunk_id for item in fused] == [7]
    assert fused[0].score == pytest.approx(1 / (RRF_K + 1))


def test_ties_break_by_chunk_id_and_are_repeatable():
    """同分并列按 ``chunk_id`` 升序定序；同一批输入两次调用逐条相同。"""
    vector_hits = [_material(9)]  # 两路各一条、都是 rank 1 ⇒ 分数相同，构成并列
    keyword_hits = [_material(2)]

    first = [item.chunk_id for item in fuse(vector_hits, keyword_hits)]
    second = [item.chunk_id for item in fuse(vector_hits, keyword_hits)]
    assert first == second == [2, 9]


def test_limit_truncates_to_fuse_limit():
    vector_hits = [_material(index) for index in range(1, 21)]
    fused = fuse(vector_hits, [], limit=12)
    assert len(fused) == 12
    assert [item.chunk_id for item in fused] == list(range(1, 13))


def test_empty_inputs_return_empty():
    assert fuse([], []) == []
    assert fuse([], [], limit=0) == []
