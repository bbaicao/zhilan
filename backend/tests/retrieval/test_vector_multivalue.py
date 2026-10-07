"""多值 kb 的向量路下推用例（``回归修复``）。

修前：``payload_filter`` 只在「恰好一个 ``kb_id``」时下推 ⇒ 多值**完全不带过滤**发全库查询，
范围收口发生在 top-K **之后**（回表 + ``matches_document``）⇒ 范围内的行只要不在全库 top-20 内
就被静默丢掉；``kb_ids=[1,2] + categories=[A]`` 还会只推 category、**kb 维度被静默丢弃**。

修后（本文件钉住的语义）：多值 ⇒ 按 kb 值**逐份单值查询**（不扩 ``app/vector`` 协议）
→ 按 ``chunk_id`` 合并（同一条保留最高分）→ **合并之后**才截断 → 回表收口照旧。

**不碰真容器、不触网**：假 store + ``FakeEmbedding`` 注入；``db`` / ``seed`` 夹具来自
``tests/conftest.py`` 与 ``tests/retrieval/conftest.py``（回表这一关要真行，所以仍连测试库）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Chunk
from app.retrieval.types import VECTOR_TOP_K, fanout_filters
from app.retrieval.vector import search_vectors
from app.vector.base import VectorHit
from app.vector.embed import FakeEmbedding


class _MapStore:
    """按 ``filters['kb_id']`` 返回预设命中的假向量库；照协议只返回前 ``limit`` 条。

    ``calls`` 记录每次收到的参数（验「按值分了几次、每次 filters 是什么」），
    ``served`` 记录实际交出去的 chunk id（验「回表收口确实剔掉了东西」）。
    """

    def __init__(self, hits_by_kb: dict[int | None, list[VectorHit]] | None = None) -> None:
        self.hits_by_kb = dict(hits_by_kb or {})
        self.calls: list[dict] = []
        self.served: list[int] = []

    def query(self, vector, limit: int = 5, filters=None):  # noqa: ANN001
        self.calls.append({"vector": list(vector), "limit": limit, "filters": filters})
        hits = list(self.hits_by_kb.get((filters or {}).get("kb_id"), []))[:limit]
        self.served += [int(hit.id) for hit in hits]
        return hits


class _FlakyStore:
    """第一次查询正常，之后抛错——验「多值展开中途失败」也走整条降级（不留半份结果）。"""

    def __init__(self, hits: list[VectorHit] | None = None) -> None:
        self.hits = hits or []
        self.calls = 0

    def query(self, vector, limit: int = 5, filters=None):  # noqa: ANN001
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("向量库目录被别的进程占用")
        return list(self.hits)


class _BoomStore:
    def query(self, vector, limit: int = 5, filters=None):  # noqa: ANN001
        raise RuntimeError("向量库目录被别的进程占用")


class _BoomEmbedding:
    async def embed_texts(self, texts):  # noqa: ANN001
        raise RuntimeError("未配置 embed 模型（EMBED_* 或 MODEL_ROLES）")


async def _chunk_ids(db, document_id: int) -> list[int]:
    """取某文档的分块 id（按 ordinal 升序）。"""
    rows = (
        await db.execute(
            select(Chunk.id).where(Chunk.document_id == document_id).order_by(Chunk.ordinal)
        )
    ).scalars()
    return [int(chunk_id) for chunk_id in rows.all()]


# --- 1. 多值 kb 真下推：N 个值 = N 次单值查询 ---------------------------------


@pytest.mark.asyncio
async def test_each_kb_value_gets_its_own_single_value_query(db, seed):
    """``kb_ids=[1,2]`` ⇒ **两次**查询，每次 filters 各含一个 kb 值（不是 None）。"""
    await seed(db, ["一号库的块。"], filename="kb1.md", kb_id=1)
    await seed(db, ["二号库的块。"], filename="kb2.md", kb_id=2)
    store = _MapStore()

    materials, warning = await search_vectors(
        db, "向量库", {"kb_ids": [1, 2]}, embedding=FakeEmbedding(dim=16), store=store
    )

    assert (materials, warning) == ([], None)
    assert len(store.calls) == 2
    assert [call["filters"] for call in store.calls] == [{"kb_id": 1}, {"kb_id": 2}]
    assert all(call["limit"] == VECTOR_TOP_K for call in store.calls)


@pytest.mark.asyncio
async def test_kb_dimension_is_not_dropped_when_a_category_also_filters(db, seed):
    """``kb_ids=[1,2] + categories=[A]``：kb **与** category 都要随每份单值查询一起下推。

    这条专打缺陷里那段「更隐蔽」：改前只推 ``{'category': 'A'}``、kb 维度被静默丢掉。
    """
    await seed(db, ["一号库的技术块。"], filename="kb1.md", kb_id=1, category="技术")
    await seed(db, ["二号库的技术块。"], filename="kb2.md", kb_id=2, category="技术")
    store = _MapStore()

    await search_vectors(
        db,
        "向量库",
        {"kb_ids": [1, 2], "categories": ["技术"]},
        embedding=FakeEmbedding(dim=16),
        store=store,
    )

    assert [call["filters"] for call in store.calls] == [
        {"kb_id": 1, "category": "技术"},
        {"kb_id": 2, "category": "技术"},
    ]


# --- 2. 合并与截断：合并去重后才截到 limit ------------------------------------


@pytest.mark.asyncio
async def test_hits_from_every_kb_are_merged_then_truncated(db, seed):
    """两条 kb 各返回若干命中（含同一条重复出现）⇒ 合并去重后**才**截断，同 id 取最高分。"""
    document_a = await seed(db, ["一库块一。", "一库块二。", "重复块。"], filename="a.md", kb_id=1)
    document_b = await seed(db, ["二库块一。", "二库块二。", "二库块三。"], filename="b.md", kb_id=2)
    ids_a = await _chunk_ids(db, document_a.id)
    ids_b = await _chunk_ids(db, document_b.id)

    # 构造性重叠：同一条 chunk 在两个 kb 的候选里都出现（真库不会，这里验的是合并口径）
    shared = ids_a[2]
    store = _MapStore(
        {
            1: [
                VectorHit(id=str(ids_a[0]), score=0.90),
                VectorHit(id=str(ids_a[1]), score=0.80),
                VectorHit(id=str(shared), score=0.50),
            ],
            2: [
                VectorHit(id=str(ids_b[0]), score=0.85),
                VectorHit(id=str(ids_b[1]), score=0.70),
                VectorHit(id=str(shared), score=0.60),
                VectorHit(id=str(ids_b[2]), score=0.40),
            ],
        }
    )

    materials, warning = await search_vectors(
        db, "向量库", {"kb_ids": [1, 2]}, limit=5, embedding=FakeEmbedding(dim=16), store=store
    )

    assert warning is None
    # 合并后 6 条不同 chunk（重复的那条只算一次）→ 截到 limit=5 ⇒ 最低分的「二库块三」被丢掉；
    # 若先截断再合并（=改前的语义）就凑不出这 5 条，若忘了去重则会多出一条。
    assert [item.chunk_id for item in materials] == [
        ids_a[0],
        ids_b[0],
        ids_a[1],
        ids_b[1],
        shared,
    ]
    scores = [item.vector_score for item in materials]
    assert scores == sorted(scores, reverse=True)
    # 同一条重复出现时保留**最高分**（改前若「先到先得」会留下 0.50）
    assert materials[-1].vector_score == pytest.approx(0.60)


# --- 3. 范围收口仍生效（合并没把 matches_document 架空） -----------------------


@pytest.mark.asyncio
async def test_hits_outside_the_kb_range_are_still_dropped(db, seed):
    """合并结果里混进范围外的命中 ⇒ 回表 ``matches_document`` 照旧剔除。"""
    document_in = await seed(db, ["范围内的块。"], filename="in.md", kb_id=1)
    document_out = await seed(db, ["范围外的块。"], filename="out.md", kb_id=9)
    ids_in = await _chunk_ids(db, document_in.id)
    ids_out = await _chunk_ids(db, document_out.id)

    store = _MapStore(
        {
            1: [
                VectorHit(id=str(ids_in[0]), score=0.60),
                VectorHit(id=str(ids_out[0]), score=0.95),  # 分更高，但不在 kb_ids=[1,2] 里
            ],
            2: [],
        }
    )

    materials, warning = await search_vectors(
        db, "向量库", {"kb_ids": [1, 2]}, embedding=FakeEmbedding(dim=16), store=store
    )

    assert warning is None
    assert store.served == [ids_in[0], ids_out[0]]  # 假库确实把范围外那条也交出来了
    assert [item.chunk_id for item in materials] == [ids_in[0]]  # 收口把它剔掉


# --- 4. 单值不回归 ------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_kb_value_keeps_one_query_and_the_store_order(db, seed):
    """``kb_ids=[7]`` ⇒ 只发**一次**查询、filters 恰为 ``{'kb_id': 7}``，且顺序原样透传。"""
    document = await seed(db, ["七库块一。", "七库块二。", "七库块三。"], filename="kb7.md", kb_id=7)
    ids = await _chunk_ids(db, document.id)
    # 故意不是分数降序：改前的单值路径是「原样透传向量库给的顺序」，这里把它钉住
    store = _MapStore(
        {
            7: [
                VectorHit(id=str(ids[2]), score=0.20),
                VectorHit(id=str(ids[0]), score=0.90),
                VectorHit(id=str(ids[1]), score=0.50),
            ]
        }
    )

    materials, warning = await search_vectors(
        db, "向量库", {"kb_ids": [7]}, embedding=FakeEmbedding(dim=16), store=store
    )

    assert warning is None
    assert len(store.calls) == 1
    assert store.calls[0]["filters"] == {"kb_id": 7}
    assert [item.chunk_id for item in materials] == [ids[2], ids[0], ids[1]]


# --- 5. 空 / 缺省：不发多余查询 ------------------------------------------------


@pytest.mark.asyncio
async def test_absent_or_empty_kb_sends_exactly_one_unfiltered_query(db, seed):
    """``kb_ids`` 缺省 / 空 ⇒ 仍然只发一次查询，且 filters 与改前一致（``None``）。"""
    await seed(db, ["一号库的块。"], filename="kb1.md", kb_id=1)

    for filters in (None, {}, {"kb_ids": []}, {"kb_ids": None}):
        store = _MapStore()
        materials, warning = await search_vectors(
            db, "向量库", filters, embedding=FakeEmbedding(dim=16), store=store
        )
        assert (materials, warning) == ([], None), filters
        assert len(store.calls) == 1, filters
        assert store.calls[0]["filters"] is None, filters


# --- 6. 降级口径不变 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_fanout_failure_degrades_as_a_whole(db, seed):
    """多值展开**中途**抛错 ⇒ 整条降级（空 + 中文提示），不返回「只查到前几个库」的半份结果。"""
    document = await seed(db, ["一号库的块。"], filename="kb1.md", kb_id=1)
    ids = await _chunk_ids(db, document.id)
    store = _FlakyStore([VectorHit(id=str(ids[0]), score=0.9)])

    materials, warning = await search_vectors(
        db, "向量库", {"kb_ids": [1, 2]}, embedding=FakeEmbedding(dim=16), store=store
    )

    assert store.calls == 2  # 第二次真的抛了（用例前提成立）
    assert materials == []
    assert warning and "仅使用关键词检索" in warning


@pytest.mark.asyncio
async def test_multi_value_with_dead_embedding_or_store_still_degrades(db, seed):
    """embed 抛错 / store 抛错 ⇒ 多值路径同样是空列表 + 中文提示，不抛穿（I5）。"""
    await seed(db, ["一号库的块。"], filename="kb1.md", kb_id=1)

    materials, warning = await search_vectors(
        db, "向量库", {"kb_ids": [1, 2]}, embedding=_BoomEmbedding(), store=_MapStore()
    )
    assert materials == []
    assert warning and "仅使用关键词检索" in warning

    materials, warning = await search_vectors(
        db, "向量库", {"kb_ids": [1, 2]}, embedding=FakeEmbedding(dim=16), store=_BoomStore()
    )
    assert materials == []
    assert warning and "仅使用关键词检索" in warning


# --- 展开纯函数（不连库） ------------------------------------------------------


def test_fanout_filters_only_splits_multi_value_kb():
    """``fanout_filters``：单值 / 缺省原样一份；多值按去重升序拆成单值且**不改入参**。"""
    assert fanout_filters(None) == [None]
    assert fanout_filters({"kb_ids": []}) == [{"kb_ids": []}]
    assert fanout_filters({"kb_id": 3}) == [{"kb_id": 3}]
    assert fanout_filters({"kb_ids": [7]}) == [{"kb_ids": [7]}]

    source = {"kb_ids": [2, 1], "categories": ["A"], "tags": ["重要"]}
    assert fanout_filters(source) == [
        {"kb_ids": [1], "categories": ["A"], "tags": ["重要"]},
        {"kb_ids": [2], "categories": ["A"], "tags": ["重要"]},
    ]
    assert source == {"kb_ids": [2, 1], "categories": ["A"], "tags": ["重要"]}
    assert fanout_filters({"kb_id": 2, "kb_ids": [3]}) == [
        {"kb_ids": [2]},
        {"kb_ids": [3]},
    ]
