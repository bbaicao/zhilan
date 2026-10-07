"""S5 · 回归修复 判据载体：``purge_document`` 向量删除失败必须计入派生失败计数。

缺陷（交接件 §2.1 · P3）：``backend/app/pipeline/ingest.py:924-945`` 的 ``purge_document``
在 Qdrant 删除抛错时**只 ``log.warning``**（``:939-945``）。同文件既有正确形态
``_delete_stale_points``（``:548-565``）在同一处还补了
``derived_counters.bump("stale_vector_points")``（``:565``）⇒ 少了这一下，残留点在运维面
**不可观测**（``failure_counts()`` 恒 0），而 HTTP 层照回 200「已彻底删除」。

判据（交接件 §2.1）：① ``purge_document`` 不抛；② MySQL 行仍被清；
③ ``failure_counts()["stale_vector_points"] >= 1``（当前码 ③ == 0 ⇒ RED）。

反向保护（必须仍绿）：``tests/pipeline/test_vector_index.py:204-224``（向量删不掉不挡 MySQL
硬删）、``tests/pipeline/test_bug107_orphan_vector_points.py``（既有点删除的键名/计数）、
十键冻结面（``tests/api/test_bug083_counter_keys_ten.py`` /
``tests/api/test_bug083_s10_breaker.py``）。

⛔ 本载体**不**声称「残留点已修」：``orphans_removed`` 恒 0、reindex 无修复路径；
   ``stale_vector_points`` 的语义 = **失败计数**（不是「点已删」）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core import derived_counters
from app.models import Chunk, Document
from app.pipeline import ingest

KEY = "stale_vector_points"


class _FakeStore:
    """最小假向量库：只实现 ``purge_document`` 用到的 ``delete``。"""

    def __init__(self, *, boom: bool) -> None:
        self.boom = boom
        self.delete_calls: list[list[str]] = []

    def delete(self, ids):  # noqa: ANN001 —— 形状对齐 QdrantVectorStore.delete
        self.delete_calls.append([str(item) for item in ids])
        if self.boom:
            raise RuntimeError("Qdrant 不可用")


@pytest.fixture(autouse=True)
def _clean_derived_counters():
    """计数是**进程全局**的 ⇒ 前后各清一次，免跨用例泄漏（同 test_bug107 的做法）。"""
    derived_counters.reset_failure_counts()
    yield
    derived_counters.reset_failure_counts()


async def _make_doc_with_chunks(db, n: int = 3):
    """与 ``test_vector_index.py:47-64`` 同形：Document + n 个 Chunk（真 MySQL 测试库）。"""
    doc = Document(
        filename="162载体.md",
        file_type="md",
        status="ready",
        index_status="ready",
        kb_id=1,
        category="测试分类",
    )
    db.add(doc)
    await db.flush()
    chunks = [
        Chunk(document_id=doc.id, content=f"第 {i} 块正文，用于 162 载体。", ordinal=i)
        for i in range(n)
    ]
    db.add_all(chunks)
    await db.flush()
    return doc, chunks


async def _chunk_ids(db, doc_id: int) -> list[int]:
    stmt = select(Chunk.id).where(Chunk.document_id == doc_id)
    return list((await db.execute(stmt)).scalars().all())


async def _document_ids(db, doc_id: int) -> list[int]:
    stmt = select(Document.id).where(Document.id == doc_id)
    return list((await db.execute(stmt)).scalars().all())


# --- 腿①：删点失败 ⇒ 不抛 + MySQL 清空 + 计数 ≥ 1（判据 ①②③） -----------------


@pytest.mark.asyncio
async def test_a1_delete_failure_counted_and_mysql_purged(db, monkeypatch):
    """注入删除失败：``purge_document`` 不抛、行仍清、``stale_vector_points`` 计数 +1。"""
    store = _FakeStore(boom=True)
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: store)
    doc, chunks = await _make_doc_with_chunks(db, 3)
    doc_id, chunk_ids = doc.id, [c.id for c in chunks]

    await ingest.purge_document(db, doc_id)  # ① 不抛

    assert store.delete_calls == [[str(cid) for cid in chunk_ids]], "必须先按 chunk id 尝试删点"
    counted = derived_counters.failure_counts().get(KEY, 0)
    assert counted >= 1, "③ 失败必须计数（当前码恒 0 ⇒ RED）"
    assert counted == 1, "一次失败一次 bump（与 ingest.py:565 同义）"
    assert await _chunk_ids(db, doc_id) == [], "② Chunk 行必须仍被清"
    assert await _document_ids(db, doc_id) == [], "② Document 行必须仍被清"


# --- 腿②：删点成功不得计数（负向对照） ----------------------------------------


@pytest.mark.asyncio
async def test_a2_success_path_does_not_count(db, monkeypatch):
    """删点成功**不得**计数（否则「失败计数」被稀释成「每次都记」）。"""
    store = _FakeStore(boom=False)
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: store)
    doc, chunks = await _make_doc_with_chunks(db, 2)

    await ingest.purge_document(db, doc.id)

    assert store.delete_calls == [[str(c.id) for c in chunks]]
    assert derived_counters.failure_counts().get(KEY, 0) == 0
    assert await _document_ids(db, doc.id) == []


# --- 腿③：无 chunk ⇒ 短路（不摸向量库、不计数） --------------------------------


@pytest.mark.asyncio
async def test_a3_no_chunks_skips_store_and_counter(db, monkeypatch):
    """无 chunk ⇒ 不摸向量库、不计数（既有 ``if chunk_ids:`` 短路语义不得改义）。"""
    store = _FakeStore(boom=True)
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: store)
    doc, chunks = await _make_doc_with_chunks(db, 0)
    assert chunks == []

    await ingest.purge_document(db, doc.id)

    assert store.delete_calls == []
    assert derived_counters.failure_counts().get(KEY, 0) == 0
    assert await _document_ids(db, doc.id) == []


# --- 腿④：键必须是冻结十键之一（⛔ 不得为修 162 新开计数键） --------------------


def test_a4_key_is_one_of_the_frozen_ten():
    """``stale_vector_points`` 必须在冻结十键内，且键集仍是 10 个、无重复。"""
    assert KEY in derived_counters.COUNTER_KEYS
    assert len(derived_counters.COUNTER_KEYS) == 10
    assert len(set(derived_counters.COUNTER_KEYS)) == 10
