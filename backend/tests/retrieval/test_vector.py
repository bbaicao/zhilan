"""向量路用例（阶段 3 · 条目 3.1）：回表、filters 下推、软删/未就绪剔除、失败降级。

**不碰真容器**：假 store + ``FakeEmbedding`` 注入（``search_vectors(..., embedding=, store=)``），
DB 用真会话（``db`` 夹具）——要验的正是「向量命中之后回表取正文与元数据」。
"""

from __future__ import annotations

import pytest

from app.retrieval.vector import search_vectors
from app.vector.base import VectorHit
from app.vector.embed import FakeEmbedding


class _FakeStore:
    """按预设命中返回的假向量库；记录每次收到的参数（用于断言 filters 下推）。"""

    def __init__(self, hits: list[VectorHit] | None = None) -> None:
        self.hits = hits or []
        self.calls: list[dict] = []

    def query(self, vector, limit: int = 5, filters=None):  # noqa: ANN001
        self.calls.append({"vector": list(vector), "limit": limit, "filters": filters})
        return list(self.hits)


class _BoomEmbedding:
    async def embed_texts(self, texts):  # noqa: ANN001
        raise RuntimeError("未配置 embed 模型（EMBED_* 或 MODEL_ROLES）")


class _EmptyEmbedding:
    async def embed_texts(self, texts):  # noqa: ANN001
        return []


class _BoomStore:
    def query(self, vector, limit: int = 5, filters=None):  # noqa: ANN001
        raise RuntimeError("向量库目录被别的进程占用")


@pytest.mark.asyncio
async def test_hits_are_materialised_from_db(db, seed):
    document = await seed(
        db,
        ["第一块：向量库使用 Qdrant。", "第二块：向量化由上游接口完成。"],
        filename="手册.md",
        kb_id=3,
        category="技术",
    )
    from app.models import Chunk
    from sqlalchemy import select

    rows = (await db.execute(select(Chunk).order_by(Chunk.ordinal))).scalars().all()
    second = rows[1].id
    store = _FakeStore([VectorHit(id=str(second), score=0.87)])

    materials, warning = await search_vectors(db, "向量库怎么存储", embedding=FakeEmbedding(dim=16), store=store)

    assert warning is None
    assert len(materials) == 1
    only = materials[0]
    assert only.chunk_id == second
    assert only.document_id == document.id
    assert only.content == "第二块：向量化由上游接口完成。"
    assert only.filename == "手册.md"
    assert only.category == "技术"
    assert only.kb_id == 3
    assert only.block_ordinal == 2
    assert only.vector_score == pytest.approx(0.87)
    assert only.keyword_score is None


@pytest.mark.asyncio
async def test_single_value_filters_are_pushed_to_the_protocol(db, seed):
    """恰好一个 kb / 一个 category 时下推 payload 过滤；tags 不在 payload 里，不下推。"""
    await seed(db, ["向量库使用 Qdrant。"], filename="a.md", kb_id=2, category="产品", tags=["重要"])
    store = _FakeStore([])

    await search_vectors(
        db,
        "向量库",
        {"kb_ids": [2], "categories": ["产品"], "tags": ["重要"]},
        embedding=FakeEmbedding(dim=16),
        store=store,
    )

    assert store.calls[-1]["filters"] == {"kb_id": 2, "category": "产品"}
    assert store.calls[-1]["limit"] > 0


@pytest.mark.asyncio
async def test_multi_value_kb_filters_are_fanned_out_and_post_filtered(db, seed):
    """多值 kb 协议表达不了 ⇒ **按值拆成单值各查一次**（回归修复），回表后仍用同一套口径收窄。

    （改前这里断言的是「不下推 = 只发一次 filters=None」——那正是缺陷本身：范围内的行只要不在
    全库 top-K 里就被静默丢掉。逐条展开/合并的边界见 ``test_vector_multivalue.py``。）
    """
    await seed(db, ["向量库属于一号库。"], filename="kb1.md", kb_id=1)
    await seed(db, ["向量库属于二号库。"], filename="kb2.md", kb_id=2)
    from app.models import Chunk
    from sqlalchemy import select

    ids = (await db.execute(select(Chunk.id).order_by(Chunk.id))).scalars().all()
    store = _FakeStore([VectorHit(id=str(ids[0]), score=0.9), VectorHit(id=str(ids[1]), score=0.8)])

    materials, warning = await search_vectors(
        db, "向量库", {"kb_ids": [2, 3]}, embedding=FakeEmbedding(dim=16), store=store
    )

    assert warning is None
    assert [call["filters"] for call in store.calls] == [{"kb_id": 2}, {"kb_id": 3}]
    assert [item.filename for item in materials] == ["kb2.md"]

    # 多值里两个都在库里时，两条都保留（多值语义是「或」，不是「且」）
    materials, _warning = await search_vectors(
        db, "向量库", {"kb_ids": [1, 2]}, embedding=FakeEmbedding(dim=16), store=store
    )
    assert [item.filename for item in materials] == ["kb1.md", "kb2.md"]


@pytest.mark.asyncio
async def test_soft_deleted_and_not_ready_hits_are_dropped(db, seed):
    """软删（向量不删）与非 ready 的命中必须在**回表**这一关被剔除。"""
    await seed(db, ["向量库可用。"], filename="ready.md")
    await seed(db, ["向量库已进回收站。"], filename="trash.md", deleted=True)
    await seed(db, ["向量库还没建好索引。"], filename="pending.md", index_status="pending")
    from app.models import Chunk
    from sqlalchemy import select

    ids = (await db.execute(select(Chunk.id).order_by(Chunk.id))).scalars().all()
    store = _FakeStore([VectorHit(id=str(chunk_id), score=0.9) for chunk_id in ids])

    materials, warning = await search_vectors(
        db, "向量库", embedding=FakeEmbedding(dim=16), store=store
    )

    assert warning is None
    assert [item.filename for item in materials] == ["ready.md"]


@pytest.mark.asyncio
async def test_non_numeric_point_ids_are_skipped(db, seed):
    """point id 非纯数字（不是本仓 chunk id）→ 跳过，不炸。"""
    await seed(db, ["向量库使用 Qdrant。"], filename="a.md")
    store = _FakeStore([VectorHit(id="abc-123", score=0.5)])

    materials, warning = await search_vectors(
        db, "向量库", embedding=FakeEmbedding(dim=16), store=store
    )
    assert (materials, warning) == ([], None)


@pytest.mark.asyncio
async def test_unavailable_embedding_degrades_to_keyword_only(db, seed):
    """未配置 / 调用失败的 embedding → 空列表 + 中文提示（**不抛穿**，I5）。"""
    await seed(db, ["向量库使用 Qdrant。"], filename="a.md")
    materials, warning = await search_vectors(db, "向量库", embedding=_BoomEmbedding(), store=_FakeStore())
    assert materials == []
    assert warning and "仅使用关键词检索" in warning


@pytest.mark.asyncio
async def test_empty_vectors_and_store_failure_also_degrade(db, seed):
    await seed(db, ["向量库使用 Qdrant。"], filename="a.md")
    materials, warning = await search_vectors(db, "向量库", embedding=_EmptyEmbedding(), store=_FakeStore())
    assert materials == []
    assert warning and "仅使用关键词检索" in warning

    materials, warning = await search_vectors(
        db, "向量库", embedding=FakeEmbedding(dim=16), store=_BoomStore()
    )
    assert materials == []
    assert warning and "仅使用关键词检索" in warning


@pytest.mark.asyncio
async def test_blank_question_and_note_only_scope_return_empty(db, seed):
    await seed(db, ["向量库使用 Qdrant。"], filename="a.md")
    assert await search_vectors(db, "   ", embedding=_BoomEmbedding(), store=_FakeStore()) == ([], None)
    assert await search_vectors(
        db, "向量库", {"source_types": ["note"]}, embedding=_BoomEmbedding(), store=_FakeStore()
    ) == ([], None)
