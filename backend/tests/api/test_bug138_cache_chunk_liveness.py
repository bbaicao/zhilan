"""`回归修复` 的永久回归钉：**回收站里的文档，它的块不算「活着」**。

## 缺陷是什么（缺陷清单-2 · 六A，2026-09-28）

`app/cache/entry.py::fingerprints_live` 的 `chunks` 支路只查两件事——`chunks` 行在不在、
`content` 的短哈希变没变（修复前那条 SQL 是
`select(Chunk.id, Chunk.content).where(Chunk.id.in_(batch))`）——**不看 `documents.deleted_at`**。
而本仓删文档是**软删**：`app/api/document_routes.py::delete_document` 只置 `documents.deleted_at`，
**`chunks` 行一行都不删**（硬删只走 `purge_document` 的级联）。合起来的后果：

1. 开缓存提问、命中写回 ⇒ 缓存条目记下 `{chunk_id: content 短哈希}`；
2. 把被引用的那篇文档**删进回收站**（块行仍在、内容逐字未变）；
3. 同题再问 ⇒ 存活校验**照样为真** ⇒ 返回一条**引用回收站文档**的整轮答案（TTL 默认 24h 内持续）。

而未命中路径的检索两支路本就排除软删（`retrieval/vector.py` 回表过滤、
`retrieval/keyword.py` 语料装载都是 `Document.deleted_at.is_(None)`）⇒ **两条路口径不一致**：
同一道题，走缓存那一轮与走检索那一轮拿到的材料面不同。

## 修法（本文件钉住的形态）

`chunks` 支路改成 **inner join `documents` + `Document.deleted_at.is_(None)`** ⇒ 软删文档的块
**根本不进 `found`**（不是「进了但值不同」）⇒ 指纹比对失败 ⇒ 调用方当未命中（真检索 + 真模型都重跑）。
口径与笔记支路（`回归修复`：`note:` 键查 `notes.deleted_at`）和检索两支路同源。

## 腿构成（6 例 · 修前「2 红 / 4 绿」）

| # | 用例 | 修前 | 作用 |
|---|---|---|---|
| 1 | 软删文档后的块 ⇒ 判**不**活 | **红** | 缺陷本体（判别力来源） |
| 2 | 软删**没动** `chunks` 行（行还在、内容逐字相同） | 绿 | 前提钉：证明第 1 条红的唯一原因是 `deleted_at` 过滤 |
| 3 | 文档没删 ⇒ 仍判活（同条内含「块被硬删 ⇒ 判不活」对照） | 绿 | 负控：防「一律判不活」的假修法 |
| 4 | `_LIVENESS_BATCH=1`（每块一批）⇒ 软删那批照样拦下；只引用活文档的块仍判活 | **红** | 分批循环里**每条** SQL 都要带 join |
| 5 | 块内容被改 ⇒ 仍判不活 | 绿 | 回归：存活校验的另一半不许被带坏 |
| 6 | 笔记支路（`note:<id>`）三态不变 | 绿 | 回归：`回归修复` 的钉子不许被带坏 |

## 残余面（**未覆盖，如实登记**）

- 本支路**只滤 `deleted_at`、不滤 `documents.index_status`**：文档正在 reindex（`processing`）
  而块内容**尚未变**时仍会命中——那是「内容确实没变」的另一回事，本条目不扩面。
- **孤儿块**（`document_id` 指向不存在的 `documents` 行；正常路径不产生）由「算活」变「算不活」，
  方向保守（多一次未命中，不会错命中）。
- 本文件钉的是**函数级**判据；`store.lookup` → `fingerprints_live` → 删条目 + `stale` 计数的
  接线（`app/cache/store.py:205-210`）本笔未改动，由既有用例覆盖。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.cache import entry as cache_entry
from app.models import Chunk, Document, Note
from tests.retrieval.conftest import seed_document

pytestmark = pytest.mark.asyncio

#: 软删时刻（与 `tests/retrieval/conftest.py::seed_document(deleted=True)` 同款写法：UTC naive）。
_DELETED_AT = datetime(2026, 9, 28, 12, 0, 0)


async def _chunk_fingerprints(db) -> dict[str, str]:
    """照写回侧口径现算这一轮的全部块指纹（`chat_routes._cache_write_entry` 的纯数字键那半）。"""
    rows = (await db.execute(select(Chunk.id, Chunk.content))).all()
    return {str(int(row[0])): cache_entry.content_fingerprint(row[1]) for row in rows}


async def _chunk_ids_by_document(db) -> dict[int, list[str]]:
    rows = (await db.execute(select(Chunk.id, Chunk.document_id))).all()
    grouped: dict[int, list[str]] = {}
    for row in rows:
        grouped.setdefault(int(row[1]), []).append(str(int(row[0])))
    return grouped


# ==========================================================================
# 一、缺陷本体 + 前提钉
# ==========================================================================


async def test_soft_deleted_document_chunk_is_not_live(db):
    """**缺陷本体**：文档进回收站 ⇒ 它的块判「不活」（修复前这条必红）。

    ⚠️ 判别力建立在「删之前是活的」这个前置断言上：没有它，一条「什么键都判不活」的实现
    也能让本用例绿——那是假证据（并把整片缓存都踢没）。
    """
    document = await seed_document(
        db, ["甲文档的第一块正文。", "甲文档的第二块正文。"], filename="bug138-soft-del.md"
    )
    fingerprints = await _chunk_fingerprints(db)
    assert len(fingerprints) == 2, "前提校验：seed_document 应当建出两个块"
    assert await cache_entry.fingerprints_live(db, fingerprints) is True, (
        "前提校验：删之前这两个块必须是活的（否则下面那条断言没有判别力）"
    )

    document.deleted_at = _DELETED_AT
    await db.commit()

    assert await cache_entry.fingerprints_live(db, fingerprints) is False, (
        "文档已进回收站 ⇒ 它的块不许再判「活着」——否则缓存会交出一条引用回收站文档的整轮答案"
    )


async def test_soft_delete_keeps_chunk_rows_untouched(db):
    """**前提钉**：软删**真的**没动 `chunks` 行（行还在、`document_id` 与内容逐字相同）。

    这条两态都绿（修前修后），它存在的意义是把上面那条的「红」归因锁死：
    判不活的唯一原因是 `documents.deleted_at` 过滤，**不是**「块被删了」或「内容变了」。
    """
    document = await seed_document(
        db, ["软删不退块第一块。", "软删不退块第二块。"], filename="bug138-rows.md"
    )
    before = {
        int(row[0]): (row[1], int(row[2]))
        for row in (await db.execute(select(Chunk.id, Chunk.content, Chunk.document_id))).all()
    }
    assert before, "前提校验：seed_document 应当建出至少一个块"

    document.deleted_at = _DELETED_AT
    await db.commit()

    after = {
        int(row[0]): (row[1], int(row[2]))
        for row in (await db.execute(select(Chunk.id, Chunk.content, Chunk.document_id))).all()
    }
    assert after == before, (
        "软删只置 documents.deleted_at、不动 chunks 行——这条不成立的话，"
        "回归修复 的机制描述就要重写（本仓删文档走的是软删）"
    )
    document_row = (
        await db.execute(select(Document.id, Document.deleted_at).where(Document.id == document.id))
    ).one()
    assert document_row[1] is not None, "前提校验：软删确实落在了 documents 行上"


# ==========================================================================
# 二、负控（正例）：没删的照旧活着
# ==========================================================================


async def test_live_document_chunk_still_live(db):
    """**负控**：文档没删 ⇒ 仍判活（防「把软删写成一律不活」这种假修法）。

    同一条里顺带钉住「块被硬删 ⇒ 判不活」（存活校验的第一半）：同一份活指纹先判活，
    再塞一个不存在的 id ⇒ 立刻判不活。
    """
    await seed_document(db, ["活着的第一块。", "活着的第二块。"], filename="bug138-live.md")
    fingerprints = await _chunk_fingerprints(db)
    assert len(fingerprints) == 2
    assert await cache_entry.fingerprints_live(db, fingerprints) is True, (
        "没删的文档 ⇒ 块必须照旧判活（否则这条修复等于把缓存整片关掉）"
    )

    missing = dict(fingerprints)
    missing["999999999"] = cache_entry.content_fingerprint("根本不存在的块")
    assert await cache_entry.fingerprints_live(db, missing) is False, (
        "对照腿：块被硬删仍要判不活（存活校验不许被本条改动带松）"
    )


# ==========================================================================
# 三、分批循环：每一批都要带 join
# ==========================================================================


async def test_every_batch_filters_soft_deleted_document(db, monkeypatch):
    """**分批循环**：`_LIVENESS_BATCH=1`（3 个块 = 3 批）⇒ 软删那批照样被拦下。

    只给第一批带 join 的写法会让这条红：批大小压到 1 等于逐个块各发一条 SQL，
    「某一批漏了过滤」再也藏不住。
    """
    live_doc = await seed_document(db, ["活文档的块。"], filename="bug138-batch-live.md")
    dead_doc = await seed_document(
        db, ["将被软删文档的块甲。", "将被软删文档的块乙。"], filename="bug138-batch-dead.md"
    )
    fingerprints = await _chunk_fingerprints(db)
    assert len(fingerprints) == 3

    grouped = await _chunk_ids_by_document(db)
    live_ids = grouped[int(live_doc.id)]
    dead_ids = grouped[int(dead_doc.id)]
    assert len(live_ids) == 1 and len(dead_ids) == 2

    monkeypatch.setattr(cache_entry, "_LIVENESS_BATCH", 1)

    assert await cache_entry.fingerprints_live(db, fingerprints) is True, (
        "前提校验：压小批大小本身不许改变结论（此时还没软删）"
    )

    dead_doc.deleted_at = _DELETED_AT
    await db.commit()

    assert await cache_entry.fingerprints_live(db, fingerprints) is False, (
        "软删文档的块分布在非首批 ⇒ 每一批的 SQL 都必须带 documents 过滤"
    )

    live_only = {chunk_id: fingerprints[chunk_id] for chunk_id in live_ids}
    assert await cache_entry.fingerprints_live(db, live_only) is True, (
        "对照腿：只引用活文档的块 ⇒ 分批下仍判活（防「分批本身把一切判死」）"
    )


# ==========================================================================
# 四、回归：存活校验的另一半与笔记支路
# ==========================================================================


async def test_edited_chunk_content_still_not_live(db):
    """**回归**：块内容被改 ⇒ 仍判不活（`reindex` 那半的判据不许被本条带坏）。"""
    await seed_document(db, ["原始内容。"], filename="bug138-edited.md")
    fingerprints = await _chunk_fingerprints(db)
    assert len(fingerprints) == 1
    assert await cache_entry.fingerprints_live(db, fingerprints) is True

    chunk = (await db.execute(select(Chunk))).scalars().one()
    chunk.content = "被改过的内容。"
    await db.commit()

    assert await cache_entry.fingerprints_live(db, fingerprints) is False, (
        "内容变了必须判不活（本条的 join 不许把它从「内容比对」变成「只看行在不在」）"
    )


async def test_note_branch_unchanged(db):
    """**回归**：笔记支路（`note:<id>`，`回归修复`）三态不变。"""
    note = Note(
        title="存活校验探针",
        content="  笔记正文（前后带空白，与图通道口径一致）  ",
        category="探针",
        kb_id=1,
    )
    db.add(note)
    await db.commit()
    note_id = int(note.id)
    fingerprints = {
        f"note:{note_id}": cache_entry.content_fingerprint((note.content or "").strip())
    }
    assert await cache_entry.fingerprints_live(db, fingerprints) is True, "活笔记照旧命中"

    note.deleted_at = _DELETED_AT
    await db.commit()
    assert await cache_entry.fingerprints_live(db, fingerprints) is False, "软删笔记照旧不命中"
