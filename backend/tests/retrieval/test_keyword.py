"""关键词路用例（阶段 3 · 条目 3.1）：2 字词召回、真短语分层、诱饵拦截、1 字兜底、范围与门禁。

语料沿用选型记录（证据 ``S3-中文检索选型.md`` §二）那 9 条文本，便于「算法语义」与
「选型实测数字」逐条对照；另加两条：一条含**真短语**「数据检索」，一条是**对抗样本**
（含 数据 / 据检 / 检索 三个 2-gram，但**不含**该子串）。
"""

from __future__ import annotations

import pytest

from app.retrieval.keyword import FALLBACK_SCORE, search_keywords
from tests.retrieval.conftest import ADVERSARIAL_DOC, PHRASE_DOC, SELECTION_CORPUS


@pytest.mark.asyncio
async def test_two_char_queries_recall_expected_docs(db, seed_many):
    """中文 **2 字词**必须可用（选型记录的核心结论：trigram 对 2 字词 0 命中）。"""
    documents = await seed_many(db, SELECTION_CORPUS)
    order = {doc.id: index for index, doc in enumerate(documents, start=1)}
    expectations = {
        "向量": {1, 4, 5, 8},
        "检索": {1, 3, 5, 6},
        "分块": {2, 6},
    }
    for query, expected in expectations.items():
        hits = await search_keywords(db, query)
        got = {order[hit.document_id] for hit in hits}
        assert got == expected, f"{query!r} 召回 {sorted(got)}，期望 {sorted(expected)}"


@pytest.mark.asyncio
async def test_phrase_hit_outranks_scattered_overlap(db, seed_many):
    """真短语命中**整体优先**于「2-gram 散落」的对抗样本。

    对抗样本按老基线的重叠语义仍属**弱命中**（共享 数据/据检/检索 三个词），本实现不把它
    直接剔除，而是用「真短语分层」压到真命中之后——见 ``keyword.py`` 模块 docstring 的口径说明。
    """
    documents = await seed_many(db, [*SELECTION_CORPUS, PHRASE_DOC, ADVERSARIAL_DOC])
    true_doc = documents[-2].id
    adversarial_doc = documents[-1].id
    # 样本自证：真命中含子串、对抗样本不含
    assert "数据检索" in PHRASE_DOC and "数据检索" not in ADVERSARIAL_DOC

    hits = await search_keywords(db, "数据检索")
    ids = [hit.document_id for hit in hits]
    assert true_doc in ids and adversarial_doc in ids
    assert ids[0] == true_doc, "含真短语的文档必须排第一"
    by_doc = {hit.document_id: hit for hit in hits}
    assert by_doc[true_doc].keyword_score > by_doc[adversarial_doc].keyword_score


@pytest.mark.asyncio
async def test_zero_overlap_decoy_recalls_nothing(db, seed_many):
    """零重叠诱饵查询**一条都不召回**（I7 的前提：不能硬凑候选）。"""
    await seed_many(db, SELECTION_CORPUS)
    for query in ("量子纠缠退相干", "红楼梦的作者是谁"):
        assert await search_keywords(db, query) == [], query


@pytest.mark.asyncio
async def test_question_style_query_recalls_relevant_doc(db, seed_many):
    """问题式查询（不是关键词）也要有召回——覆盖率分母不再被 OOV 伪双字灌水。"""
    documents = await seed_many(db, SELECTION_CORPUS)
    vector_doc = documents[3].id  # 「向量库使用 Qdrant 内嵌模式…」
    hits = await search_keywords(db, "向量库用什么存储？")
    assert vector_doc in [hit.document_id for hit in hits]


@pytest.mark.asyncio
async def test_single_char_query_falls_back_to_like(db, seed_many):
    """1 字查询走 ``LIKE`` 兜底（D-b）：命中即低分，空集也不报错。"""
    await seed_many(db, SELECTION_CORPUS)
    hits = await search_keywords(db, "库")
    assert hits, "1 字查询应有命中（LIKE 兜底）"
    assert {hit.keyword_score for hit in hits} == {FALLBACK_SCORE}
    assert all("库" in hit.content for hit in hits)
    assert await search_keywords(db, "殁") == []


@pytest.mark.asyncio
async def test_not_ready_and_soft_deleted_documents_are_excluded(db, seed):
    """门禁：``index_status != ready`` 与软删（``deleted_at`` 非空）都不得进结果。"""
    await seed(db, ["向量库使用 Qdrant 内嵌模式。"], filename="ready.md")
    await seed(db, ["向量库正在建索引。"], filename="pending.md", index_status="pending")
    await seed(db, ["向量库已进回收站。"], filename="trash.md", deleted=True)

    hits = await search_keywords(db, "向量库")
    assert [hit.filename for hit in hits] == ["ready.md"]


@pytest.mark.asyncio
async def test_filters_narrow_results(db, seed):
    """范围过滤：``kb_ids`` 与 ``categories`` 下推 SQL；``tags``（JSON 列）后置收口。"""
    await seed(db, ["向量库使用 Qdrant 内嵌模式。"], filename="kb1.md", kb_id=1, category="技术")
    await seed(
        db,
        ["向量库在另一个知识库里。"],
        filename="kb2.md",
        kb_id=2,
        category="产品",
        tags=["重要", "草案"],
    )

    assert [h.filename for h in await search_keywords(db, "向量库", {"kb_ids": [2]})] == ["kb2.md"]
    assert [h.filename for h in await search_keywords(db, "向量库", {"kb_id": 1})] == ["kb1.md"]
    assert [h.filename for h in await search_keywords(db, "向量库", {"categories": ["产品"]})] == [
        "kb2.md"
    ]
    # tags：同一条资料要**同时**具备全部标签
    assert [h.filename for h in await search_keywords(db, "向量库", {"tags": ["重要"]})] == [
        "kb2.md"
    ]
    assert await search_keywords(db, "向量库", {"tags": ["重要", "不存在"]}) == []
    # 本阶段语料只有文档分块：范围只要 note ⇒ 空（不硬凑）
    assert await search_keywords(db, "向量库", {"source_types": ["note"]}) == []


@pytest.mark.asyncio
async def test_repeated_queries_return_identical_order(db, seed_many):
    """同一批输入两次调用结果**逐条相同**（RRF 与引用编号一致性的地基）。"""
    await seed_many(db, SELECTION_CORPUS)
    first = [hit.chunk_id for hit in await search_keywords(db, "检索与向量")]
    second = [hit.chunk_id for hit in await search_keywords(db, "检索与向量")]
    assert first == second
