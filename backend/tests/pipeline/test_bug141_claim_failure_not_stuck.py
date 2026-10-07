"""回归修复 判据载体：认领分支的窄缝不得把文档行**永久卡在 pending/processing**。

## 重复文档认领的失败收口

``_ingest_into`` 的重复判定分支有两处 ``raise DuplicateDocument``（回收站行 / 正常行），
修前它们**不经过 ``mark_failed``**：

- **认领路径**（``created=False``，``_find_failed_by_path`` 认领的那行）：``mark_pending`` 已提交
  ⇒ 直接 raise ⇒ 该行**永远停在 pending/processing**；而 409 文案指向的是**另一条**行
  （用户完全看不出这条卡住的行是刚才那次上传留下的），本仓无清扫器 ⇒ 列表里永久转圈；
- **新建路径**（``created=True``）：``session.delete(doc) + commit()`` 是「丢弃刚建的 pending 行」，
  这一步**若失败**（DB 抖动 / 连接断开 / 提交冲突），刚建的行就永久卡在 pending。

## 修后正确行为（本文件的断言全按此写）

两处分支在 raise **之前**保证「**没有任何行**停在 pending/processing」（置 failed 或把刚建的
行清掉，两种收口都算过），并且**仍抛 ``DuplicateDocument``**（409 契约语义不变）。

## ⚠️ 打桩纪律（B 路 REAL-3 的教训）

**不许** ``monkeypatch.setattr(AsyncSession, "commit", …)``：那是**类级**打桩，会连
SQLAlchemy 连接池的 pre-ping（aiomysql 的 checkin/ping）一起打断 ⇒ 抛出来的根本不是
你要注入的那次 commit（实测表现为 ``MissingGreenlet``），判据会变成假红。

本文件改用**两个「动作驱动」的桩**（都不碰 ``commit``）：

- 腿②：桩 ``AsyncSession.delete`` —— 只对「本次刚建的 pending 行」（即 ``created=True``
  的丢弃动作）抛错。产品因此走「回滚 + 置 failed」的兜底；
- 腿③：桩 ``ingest.mark_failed`` —— 只对「``created=False`` 的认领收口」抛错，
  验「收尾失败**不许换掉** 409」。

## 🔴 残余窗口（如实登记，**不许**写成断言）

当 ``mark_failed`` **自己**都失败时（连接级故障），那一行**仍可能停在 pending** ——
产品注释里也如实写了「该窗口未消除」（本笔不引入独立 session / 重连重试）。
故腿③**只钉「主异常不被换掉」这一条硬事实**，**不**断言「认领行一定被置 failed」。

## 与既有 ``tests/pipeline/test_ingest.py`` 的关系

夹具照该文件既有范式（`:24-63`）；腿④复刻 `:167` 的 ``test_duplicate_content_raises`` 口径，
守「修复没有把丢弃新行的逻辑删掉」。本文件自带全部夹具，可单独跑。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import DocumentParseError, DuplicateDocument
from app.models import Document
from app.pipeline import ingest
from app.vector.embed import FakeEmbedding

_TRASHED_AT = datetime(2026, 9, 27, 12, 0, 0)
_CONTENT = "# 卡住的行\n\n认领分支 raise 之前必须把行收口，不能停在 pending。\n" * 3


# --------------------------------------------------------------------------
# 夹具（照 tests/pipeline/test_ingest.py:24-63 的既有范式）
# --------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _clean_documents(db):
    """每个用例前清 documents/chunks（保证本文件单独跑时也不受别的用例残留影响）。"""
    await db.execute(text("DELETE FROM chunks"))
    await db.execute(text("DELETE FROM documents"))
    await db.commit()
    yield


@pytest.fixture(autouse=True)
def _isolate_vector(monkeypatch):
    """隔离真 embedding / 真 Qdrant（本文件只判状态机，不连向量库、不出网）。"""

    class _NoEmbed(FakeEmbedding):
        async def embed_texts(self, texts):
            raise RuntimeError("测试隔离：不调用真实 embedding")

    class _NoStore:
        def ensure_collection(self, dim):
            raise RuntimeError("测试隔离：不连 Qdrant")

        def upsert(self, points):
            raise RuntimeError("测试隔离：不连 Qdrant")

        def delete(self, ids):
            raise RuntimeError("测试隔离：不连 Qdrant")

    monkeypatch.setattr(ingest, "get_embedding_client", lambda *, fake=False: _NoEmbed(dim=4))
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: _NoStore())


@pytest.fixture
def _upload_dir(monkeypatch, tmp_path):
    """把上传留档目录重定向到 tmp_path（property 不能被实例属性覆盖，改类属性）。"""
    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path))
    return tmp_path


# --------------------------------------------------------------------------
# 打桩与观测辅助
# --------------------------------------------------------------------------


def _fail_parse_once(monkeypatch) -> dict:
    """解析桩：**第一次抛、之后透传**（制造「解析失败 ⇒ hash 仍 NULL」的认领行）。"""
    state = {"calls": 0}
    orig = ingest.parse_document_pages

    def _boom_once(filename, data):
        state["calls"] += 1
        if state["calls"] == 1:
            raise DocumentParseError("该文件未提取到文字，请上传文字版或 txt/md。")
        return orig(filename, data)

    monkeypatch.setattr(ingest, "parse_document_pages", _boom_once)
    return state


def _fail_discard_delete(monkeypatch) -> dict:
    """桩 ``AsyncSession.delete``：**只对「本次刚建的 pending 行」**（``created=True`` 的
    丢弃动作）抛错 ⇒ 产品走「回滚 + 置 failed」兜底。

    ⚠️ 不桩 ``commit``（见模块 docstring「打桩纪律」）：那会打断连接池 pre-ping。
    """
    state = {"discard_deletes": 0, "raised": 0}
    orig_delete = AsyncSession.delete

    async def _delete(self, instance):
        if getattr(instance, "index_status", None) == "pending":
            state["discard_deletes"] += 1
            state["raised"] += 1
            raise RuntimeError("测试注入：丢弃刚建 pending 行失败")
        return await orig_delete(self, instance)

    monkeypatch.setattr(AsyncSession, "delete", _delete)
    return state


def _fail_mark_failed_for_claim(monkeypatch) -> dict:
    """桩 ``ingest.mark_failed``：只对**认领行**抛错（验「收尾失败不许换掉 409」）。

    ⚠️ **桩里绝不能读 ORM 属性**（如 ``doc.index_status``）：``_close_out_duplicate_row`` 的
    兜底分支会先 ``session.rollback()``，而 rollback 让 session 内**所有持久对象过期**
    ⇒ 读属性会触发隐式刷新（同步 IO）⇒ 在 AsyncSession 里抛 ``MissingGreenlet``，
    把「收尾失败」变成一条**与产品无关的假红**（本文件第一版就踩了这个坑）。

    改用**调用序**判定（``calls == 1``）：本次流程里 ``mark_failed`` 的第一次调用就是
    认领行的收口；兜底那次是第二次（本桩按设计放它过去，让兜底真的生效）。
    """
    state = {"calls": 0, "raised": 0}
    orig = ingest.mark_failed

    async def _mark_failed(session, doc, error):
        state["calls"] += 1
        if state["calls"] == 1:
            state["raised"] += 1
            raise RuntimeError("测试注入：认领行收口失败")
        return await orig(session, doc, error)

    monkeypatch.setattr(ingest, "mark_failed", _mark_failed)
    return state


async def _rows(db) -> list[Document]:
    await db.rollback()
    return list((await db.execute(select(Document).order_by(Document.id))).scalars().all())


def _dump(rows) -> list[tuple]:
    """行状态快照：``(id, index_status, status, 是否在回收站)``。"""
    return [(r.id, r.index_status, r.status, r.deleted_at is not None) for r in rows]


def _stuck(rows) -> list[tuple]:
    """停在 pending/processing 的行（**本文件的核心断言对象**）。"""
    return [t for t in _dump(rows) if t[1] in ("pending", "processing")]


async def _setup_claim_trap(db, tmp_path, monkeypatch) -> tuple[int, int]:
    """构造 141 的触发态（照缺陷清单五步）：

    ① X 以 n1 导入在**解析阶段**失败（hash 仍 NULL，只能按 file_path 认领）；
    ② 同内容以 n2 上传成功（hash 落在 n2 上）；
    ③ n2 被软删（进回收站）；
    ⇒ 再以 n1 导入 ⇒ 认领 n1 行置 pending ⇒ hash 命中 n2（已删）⇒ 抛 409。

    返回 ``(认领行 id, 回收站行 id)``。
    """
    bad = tmp_path / "n1.md"
    bad.write_text(_CONTENT, encoding="utf-8")
    _fail_parse_once(monkeypatch)

    with pytest.raises(DocumentParseError):
        await ingest.ingest_document(db, bad.name, bad.read_bytes(), stored_path=str(bad))

    rows = await _rows(db)
    assert len(rows) == 1 and rows[0].index_status == "failed", f"前置①不成立：{_dump(rows)}"
    claim_id = rows[0].id
    assert rows[0].content_hash is None, "前置①：解析失败的行 hash 必须为 NULL"

    ok = await ingest.ingest_document(db, "n2.md", _CONTENT.encode("utf-8"))
    good = await db.get(Document, ok.document_id)
    good.deleted_at = _TRASHED_AT
    await db.commit()
    return claim_id, ok.document_id


# --------------------------------------------------------------------------
# 腿① 主腿（认领路径）：409 之后**不许**有行停在 pending/processing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claimed_row_not_left_pending_when_trash_row_blocks(db, tmp_path, monkeypatch):
    """**主腿**：认领行命中「回收站里的同内容行」⇒ 抛 409 之后，认领行必须已收口。

    修前：``mark_pending`` 已提交，raise 不经过 ``mark_failed`` ⇒ 该行永久停在 pending，
    而 409 文案指向的是**另一条**行 ⇒ 列表里一条永不变化的 processing 行。
    """
    bad = tmp_path / "n1.md"
    bad.write_text(_CONTENT, encoding="utf-8")
    claim_id, trashed_id = await _setup_claim_trap(db, tmp_path, monkeypatch)

    with pytest.raises(DuplicateDocument):
        await ingest.ingest_document(db, bad.name, bad.read_bytes(), stored_path=str(bad))

    rows = await _rows(db)
    stuck = _stuck(rows)
    assert not stuck, (
        f"认领分支 raise 之前必须把行收口（置 failed 或回滚），不得停在 pending/processing："
        f"{stuck}；全部行 = {_dump(rows)}"
    )
    # 认领行仍在（「失败留行」语义不回退），且已收口
    claim_row = await db.get(Document, claim_id)
    assert claim_row is not None, "认领行不该被删掉（它是用户可见的失败行）"
    assert claim_row.index_status not in ("pending", "processing"), (
        f"认领行仍停在 {claim_row.index_status}（回归修复 未修）"
    )
    # 回收站行原样不动（本条目只动认领行的收口）
    trashed_row = await db.get(Document, trashed_id)
    assert trashed_row.deleted_at is not None
    # 可见面：列表里不得出现「永久转圈」的行
    visible = await ingest.list_documents(db)
    assert not [d for d in visible if d.index_status in ("pending", "processing")], (
        f"GET /documents 里出现永不变化的 processing 行："
        f"{[(d.id, d.index_status) for d in visible]}"
    )


@pytest.mark.asyncio
async def test_claimed_row_not_left_pending_when_live_ready_row_blocks(db, tmp_path, monkeypatch):
    """同一窄缝的**第二种触发面**：同内容命中一条**在库的 ready 行**（不软删）⇒ 同样不许卡 pending。

    这条把「已删行」这一维拿掉，证明缺陷不是「回收站专属」——两处认领分支都漏了收口。
    """
    bad = tmp_path / "n1.md"
    bad.write_text(_CONTENT, encoding="utf-8")
    _fail_parse_once(monkeypatch)

    with pytest.raises(DocumentParseError):
        await ingest.ingest_document(db, bad.name, bad.read_bytes(), stored_path=str(bad))
    rows = await _rows(db)
    assert len(rows) == 1 and rows[0].index_status == "failed", f"前置不成立：{_dump(rows)}"
    claim_id = rows[0].id

    ok = await ingest.ingest_document(db, "n2.md", _CONTENT.encode("utf-8"))  # 在库、ready

    with pytest.raises(DuplicateDocument):
        await ingest.ingest_document(db, bad.name, bad.read_bytes(), stored_path=str(bad))

    rows = await _rows(db)
    stuck = _stuck(rows)
    assert not stuck, (
        f"命中在库 ready 行时，认领行同样不得停在 pending/processing：{stuck}；"
        f"全部行 = {_dump(rows)}"
    )
    claim_row = await db.get(Document, claim_id)
    assert claim_row.index_status not in ("pending", "processing")
    blocker = await db.get(Document, ok.document_id)
    assert blocker.index_status == "ready", "既有的在库行不该被本路径改动"


# --------------------------------------------------------------------------
# 腿② 主腿（新建路径 · 桩 delete）：丢弃新行的动作失败 ⇒ 不许留卡住的行
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discard_delete_failure_leaves_no_stuck_row(db, tmp_path, monkeypatch):
    """**打桩腿**：让「丢弃刚建 pending 行」这个**动作本身**抛错（桩 ``AsyncSession.delete``）。

    修前：``session.delete(doc) + commit`` 抛错直接穿出（**不是 409**），且刚建的行留在库里
    ⇒ 永久 pending。

    修后：产品走兜底 ⇒ **主异常仍是 ``DuplicateDocument``**，且没有任何行停在
    pending/processing。此时那一行**不是「已丢弃」，而是「可见的失败行 + 可重试」**——
    这是**有意降级**（丢不掉就如实置 failed，绝不静默留一行转圈）。
    """
    # 前置：同内容已在回收站（新建路径会命中它）
    first = await ingest.ingest_document(db, "blocker.md", _CONTENT.encode("utf-8"))
    blocker = await db.get(Document, first.document_id)
    blocker.deleted_at = _TRASHED_AT
    await db.commit()

    stub = _fail_discard_delete(monkeypatch)
    with pytest.raises(DuplicateDocument):
        await ingest.ingest_document(db, "again.md", _CONTENT.encode("utf-8"))

    assert stub["raised"] >= 1, (
        f"桩没被触发（discard_deletes={stub['discard_deletes']}）⇒ 判据无效："
        "本次流程应含一次「丢弃刚建 pending 行」的 delete"
    )
    rows = await _rows(db)
    stuck = _stuck(rows)
    assert not stuck, (
        f"丢弃动作失败后不得留下停在 pending/processing 的行：{stuck}；全部行 = {_dump(rows)}"
    )
    # 有意降级：那一行落成「可见的失败行」（不是被静默丢弃，也不是卡住的转圈行）
    discarded = [r for r in rows if r.index_status == "failed"]
    assert discarded, f"兜底应把该行置为可见的失败行：{_dump(rows)}"
    assert discarded[0].index_error, "失败行必须有可读原因（前端 tooltip 用）"
    # 既有行（回收站那行）必须原样不动
    blocker = await db.get(Document, first.document_id)
    assert blocker.deleted_at is not None and blocker.index_status == "ready"


# --------------------------------------------------------------------------
# 腿③ 主腿（认领路径 · 桩 mark_failed）：收尾失败**不许换掉** 409
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_close_out_failure_does_not_replace_duplicate_error(
    db, tmp_path, monkeypatch
):
    """**打桩腿**：让认领行的收口（``mark_failed``）自己抛错 ⇒ 主异常必须**仍是**
    ``DuplicateDocument``（收尾失败不许把 409 换成 RuntimeError）。

    🔴 **本腿刻意不判「行有没有卡住」**：``mark_failed`` 连兜底都失败时（连接级故障），
    该行**仍可能停在 pending** —— 这是产品注释里已如实登记的**残余窗口**（未消除）。
    硬钉它会写成一条「要求产品做到它明确没承诺的事」的假判据。
    """
    bad = tmp_path / "n1.md"
    bad.write_text(_CONTENT, encoding="utf-8")
    _setup_ids = await _setup_claim_trap(db, tmp_path, monkeypatch)
    claim_id = _setup_ids[0]

    stub = _fail_mark_failed_for_claim(monkeypatch)
    with pytest.raises(DuplicateDocument) as exc_info:
        await ingest.ingest_document(db, bad.name, bad.read_bytes(), stored_path=str(bad))

    assert stub["raised"] >= 1, (
        f"桩没被触发（calls={stub['calls']}）⇒ 判据无效：认领行收口应调 mark_failed"
    )
    # 硬事实：主异常是 409 语义，不是收尾异常
    assert exc_info.value.document_id is not None, "DuplicateDocument 必须仍带既有行 id"
    assert not isinstance(exc_info.value, RuntimeError)
    # 认领行不得被静默删掉（它是用户可见的失败行）
    assert await db.get(Document, claim_id) is not None


# --------------------------------------------------------------------------
# 腿④ 负控：不打桩时，认领路径不留新行、正常重复路径不留新行
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_path_creates_no_extra_row(db, tmp_path, monkeypatch):
    """负控（认领路径）：修复只该补「收口」，不得顺手改掉「不新增行」的既有语义。

    ⚠️ 本腿**刻意不判「卡不卡住」**（那是腿①的职责）：负控的价值在于修前修后都绿，
    这样才能证明「修复没有把认领机制整个删掉 / 改跑偏」。
    """
    bad = tmp_path / "n1.md"
    bad.write_text(_CONTENT, encoding="utf-8")
    claim_id, trashed_id = await _setup_claim_trap(db, tmp_path, monkeypatch)

    with pytest.raises(DuplicateDocument):
        await ingest.ingest_document(db, bad.name, bad.read_bytes(), stored_path=str(bad))

    rows = await _rows(db)
    assert len(rows) == 2, (
        f"认领路径不得新增第三行（应恰为 认领行 + 回收站行）；实际 {_dump(rows)}"
    )
    assert {r.id for r in rows} == {claim_id, trashed_id}


@pytest.mark.asyncio
async def test_fresh_duplicate_still_leaves_no_new_row(db, _upload_dir):
    """负控（新建路径）：``created=True`` 命中重复时，刚建的行仍必须被丢弃
    （照既有 ``test_duplicate_content_raises`` 的口径；防止「修复把删行逻辑删了」）。

    本腿修前修后都绿：那条 discard 正常成功 ⇒ 不留行、也不留卡住的行。
    """
    content = "# 重复\n\n同一份内容两次上传应被拦截。\n" * 3
    r1 = await ingest.ingest_document(db, "a.md", content.encode("utf-8"))
    with pytest.raises(DuplicateDocument) as exc_info:
        await ingest.ingest_document(db, "b.md", content.encode("utf-8"))
    assert exc_info.value.document_id == r1.document_id
    rows = await _rows(db)
    assert len(rows) == 1, f"不应留下第二个文档行（无垃圾行、无卡住的行）：{_dump(rows)}"
    assert not _stuck(rows), f"不得留下 pending/processing 的行：{_dump(rows)}"
