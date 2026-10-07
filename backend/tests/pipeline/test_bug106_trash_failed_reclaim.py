"""回归修复 判据载体：回收站里的 failed 行不得被「就地重试」复活。

## 回收站行的隔离要求

``ingest._find_failed_by_path`` 的 where 缺 ``deleted_at IS NULL``（它的 ``load_only`` 里
却明明 load 了 ``deleted_at`` 列）。于是「导入坏文件 → 行 ``failed`` → 软删进回收站
（``document_routes.py:527-537`` 只置 ``deleted_at``）→ 重传同一文件」会命中**回收站里那行**
并就地重试：解析成功 ⇒ 置 ``ready``，但 ``deleted_at`` 原样保留（``_finish_ingest`` 全程
不碰它）⇒ ``list_documents``（``deleted_at IS NULL`` 过滤）看不见它。用户拿到
「已写入 N 个分块」的成功回执，列表里却没有这篇文档。

## ⚠️ 本文件最容易写错的地方：**重传的终态取决于第一次失败发生在哪一阶段**

``_finish_ingest`` 的 except 分支会**补写并提交** ``content_hash``（``ingest.py:573``）——
所以「收尾阶段失败」的回收站行**带着 hash**，「解析阶段失败」的行 hash 仍是 NULL。
两条腿修后的终态**完全不同**，必须分开写（A 路审计 BLOCKER-2 的教训：把收尾阶段那条
照「新行 ready 可见」写，就是一条**修前修后都红**的零判别力假判据）：

| 第一次失败在 | 回收站那行有 hash？ | 修后重传终态 | 修前重传终态（缺陷） |
|---|---|---|---|
| **解析阶段**（``parse_document_pages`` 抛） | **没有**（NULL） | 跳过回收站行 ⇒ **新建一行**、ready、列表可见 | 认领回收站行 ⇒ 就地 ready、``deleted_at`` 仍在 ⇒ **列表看不见** |
| **收尾阶段**（``_finish_ingest`` 抛） | **有**（except 里补写） | 新建行 ⇒ ``_find_by_hash`` 命中回收站行 ⇒ **409** + 丢弃新行 | 同上：就地 ready 但不可见（**不抛异常**） |

判别力来自**同一处**：``_find_failed_by_path`` 跳过回收站行之后，「回收站那行是否被复活」
与「调用方是否拿到 409」两个面上修前修后必然相反。

## 主腿为什么用「import-file 语义」（stored_path = 原文件绝对路径）

``save_upload`` 是**内容寻址**的（``<sha256[:8]>-<安全名>``，``storage.py:50-74``），
单文件导入（``POST /documents/import-file``，``document_routes.py:287-291``）**不复制**，
直接记原文件绝对路径 ⇒ ``file_path`` 恒相同、与内容无关 ⇒ 是 ``_find_failed_by_path``
最干净的触发面。腿①c 另用上传端点（bytes）走一遍同款口径。

⚠️ **桩必须「只抛一次」且内容必须合法**（计数驱动、不依赖时序）：
「坏文件恒失败」会让第二次也在解析处抛 ⇒ 永远到不了「新行 ready」；
用**损坏的 PDF 字节**当「合法内容」同样错——第 2 次透传到的仍是那串坏字节。
故本文件一律用**能被真解析的 `.md` 正文**，只让桩的**第一次**调用抛。

## 与既有 ``tests/pipeline/test_ingest.py`` 的关系

夹具（``_clean_documents`` / ``_isolate_vector`` / ``_upload_dir``）与断言口径照抄该文件
既有范式（:24-63），但**不复用它的用例**：既有 12 条没有一条覆盖 ``deleted_at`` 与
``_find_failed_by_path`` 的交互（`:244`` 那条只测「不在回收站」的 failed 行）。
本文件自带全部夹具，可单独跑。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.core.config import settings
from app.core.errors import DocumentParseError, DuplicateDocument
from app.models import Chunk, Document
from app.pipeline import ingest

# 软删时间戳：naive UTC（与 document_routes._utc_now / Document.deleted_at 同口径）
_TRASHED_AT = datetime(2026, 9, 27, 12, 0, 0)

# 正文用同一份常量：同内容 ⇒ 同 content_hash（收尾阶段那条腿要用），
# 且 .md 是**真能被解析**的格式 ⇒ 只抛一次的桩之后第二次能真正跑完。
_CONTENT = "# 回收站复活\n\n重传必须新建一行，而不是就地复活回收站里那行。\n" * 3


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
    """隔离真 embedding / 真 Qdrant。

    本文件只关心 MySQL 状态机与重复判定口径：embed 快速失败 ⇒ ``vectorized=false``
    （与既有用例同款，避免连真 Qdrant、避免任何出网）。
    """

    class _NoEmbed:
        dim = 4

        async def embed_texts(self, texts):
            raise RuntimeError("测试隔离：不调用真实 embedding")

    class _NoStore:
        def ensure_collection(self, dim):
            raise RuntimeError("测试隔离：不连 Qdrant")

        def upsert(self, points):
            raise RuntimeError("测试隔离：不连 Qdrant")

        def delete(self, ids):
            raise RuntimeError("测试隔离：不连 Qdrant")

    monkeypatch.setattr(ingest, "get_embedding_client", lambda *, fake=False: _NoEmbed())
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: _NoStore())


@pytest.fixture
def _upload_dir(monkeypatch, tmp_path):
    """把上传留档目录重定向到 tmp_path（property 不能被实例属性覆盖，改类属性）。"""
    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path))
    return tmp_path


# --------------------------------------------------------------------------
# 打桩：只让**第一次**失败，之后透传（计数驱动，不依赖时序）
# --------------------------------------------------------------------------


def _fail_parse_once(monkeypatch) -> dict:
    """解析桩：第一次抛 ``DocumentParseError``、之后透传（**失败落在解析阶段**）。

    解析阶段失败 ⇒ 那行 ``content_hash`` 仍是 NULL（那一步没跑完）⇒ 重传时新建行不会
    撞上内容级重复判定，能真正走到「新行 ready 可见」。
    """
    state = {"calls": 0}
    orig = ingest.parse_document_pages

    def _boom_once(filename, data):
        state["calls"] += 1
        if state["calls"] == 1:
            raise DocumentParseError("该文件未提取到文字，请上传文字版或 txt/md。")
        return orig(filename, data)

    monkeypatch.setattr(ingest, "parse_document_pages", _boom_once)
    return state


def _fail_persist_once(monkeypatch) -> dict:
    """写 chunks 桩：第一次抛、之后透传（**失败落在收尾阶段**）。

    ``_finish_ingest`` 的失败分支会**补写** ``content_hash``（``ingest.py:573``）——
    这正是它与「解析阶段失败」终态不同的原因（见模块 docstring 的表）。
    """
    state = {"calls": 0}
    orig = ingest._persist_chunks

    async def _boom_once(session, doc, prepared):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("模拟第一次写 chunks 失败")
        return await orig(session, doc, prepared)

    monkeypatch.setattr(ingest, "_persist_chunks", _boom_once)
    return state


# --------------------------------------------------------------------------
# 观测辅助
# --------------------------------------------------------------------------


async def _rows(db) -> list[Document]:
    """按 id 升序取全部 documents 行（失败信息里带上状态快照，便于定位）。"""
    await db.rollback()
    return list((await db.execute(select(Document).order_by(Document.id))).scalars().all())


def _dump(rows) -> list[tuple]:
    """行状态快照：``(id, index_status, has_hash, 是否在回收站)``。"""
    return [
        (r.id, r.index_status, r.content_hash is not None, r.deleted_at is not None)
        for r in rows
    ]


def _stuck(rows) -> list[tuple]:
    """停在 pending/processing 的行（腿③的不变量）。"""
    return [t for t in _dump(rows) if t[1] in ("pending", "processing")]


async def _soft_delete(db, doc_id: int) -> None:
    """软删：照 ``DELETE /api/documents/{id}``（只置 ``deleted_at``，不动 index_status）。"""
    doc = await db.get(Document, doc_id)
    doc.deleted_at = _TRASHED_AT
    await db.commit()


# --------------------------------------------------------------------------
# 腿① 主腿（解析阶段失败 · import-file 语义）：重传**新建一行**且在列表里可见
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reimport_after_soft_delete_creates_new_visible_row(db, tmp_path, monkeypatch):
    """**主腿**：解析阶段失败 ⇒ 软删 ⇒ 同一路径重传（等价于对同一个文件点两次
    ``POST /documents/import-file``）。

    修前：命中回收站那行就地重试 ⇒ 无异常返回、那行置 ``ready`` 但 ``deleted_at`` 仍在
    ⇒ **列表里看不见**（缺陷症状；本用例死在「修后应当 409？不，解析阶段这条不抛」——
    判别力在**行数与可见性**上：修前 1 行且不可见，修后 2 行且新行可见）。

    修后：跳过回收站行 ⇒ 新行 ``deleted_at IS NULL``、``ready``、列表可见，
    回收站那行**原样不动**（hash 仍 NULL、仍 failed）。
    """
    src = tmp_path / "same.md"
    src.write_text(_CONTENT, encoding="utf-8")
    data = src.read_bytes()
    _fail_parse_once(monkeypatch)

    with pytest.raises(DocumentParseError):
        await ingest.ingest_document(db, src.name, data, stored_path=str(src))

    rows = await _rows(db)
    assert len(rows) == 1 and rows[0].index_status == "failed", f"前置条件不成立：{_dump(rows)}"
    trashed_id = rows[0].id
    assert rows[0].content_hash is None, (
        "前置条件：解析阶段失败的行没有 content_hash（只能按 file_path 认领）"
    )
    await _soft_delete(db, trashed_id)

    result = await ingest.ingest_document(db, src.name, data, stored_path=str(src))

    rows = await _rows(db)
    assert len(rows) == 2, (
        f"重传后应恰好 2 行（回收站 1 + 新行 1），实际 {len(rows)} 行：{_dump(rows)}"
    )
    assert result.document_id != trashed_id, (
        "重传不得复用回收站那行的 id（那就是「就地复活」）"
    )

    # 可见性：这是缺陷清单写的症状面（「报成功但列表看不见」）
    visible = await ingest.list_documents(db)
    assert [d.id for d in visible] == [result.document_id], (
        f"重传后新建的行必须在 list_documents 里可见；实际可见 "
        f"{[d.id for d in visible]}，新行 = {result.document_id}，全部行 = {_dump(rows)}"
    )

    new_row = await db.get(Document, result.document_id)
    assert new_row.index_status == "ready"
    assert new_row.status == "ready"
    assert new_row.deleted_at is None, "新行必须在库（deleted_at IS NULL）"
    assert new_row.chunk_count > 0

    # 回收站那行原样不动
    old_row = await db.get(Document, trashed_id)
    assert old_row.deleted_at is not None, "回收站那行必须原样留在回收站"
    assert old_row.index_status == "failed", "回收站那行不得被复活成 ready"


@pytest.mark.asyncio
async def test_upload_bytes_reimport_after_soft_delete_creates_new_row(
    db, _upload_dir, monkeypatch
):
    """腿①c（**上传端点 · bytes 路径**）：``save_upload`` 内容寻址 ⇒ 同字节得同一
    ``file_path``，于是同样会命中 ``_find_failed_by_path`` —— 两条入口口径必须一致。

    ⚠️ 用**合法 `.md` 正文**（不是损坏的 PDF）：桩只抛第一次，第 2 次必须能真跑完，
    否则「新行 ready」永远不可达（A 路审计 REAL-4）。
    """
    data = _CONTENT.encode("utf-8")
    _fail_parse_once(monkeypatch)

    with pytest.raises(DocumentParseError):
        await ingest.ingest_document(db, "reclaim.md", data)

    rows = await _rows(db)
    assert len(rows) == 1 and rows[0].index_status == "failed", f"前置条件不成立：{_dump(rows)}"
    trashed_id = rows[0].id
    assert rows[0].content_hash is None
    await _soft_delete(db, trashed_id)

    result = await ingest.ingest_document(db, "reclaim.md", data)  # 重传同一字节

    rows = await _rows(db)
    assert len(rows) == 2, (
        f"重传同一字节后应恰好 2 行（回收站 1 + 新行 1），实际 {len(rows)} 行：{_dump(rows)}"
    )
    assert result.document_id != trashed_id
    visible = await ingest.list_documents(db)
    assert [d.id for d in visible] == [result.document_id], (
        f"新行必须在列表里可见；实际 {[d.id for d in visible]}，全部行 = {_dump(rows)}"
    )
    new_row = await db.get(Document, result.document_id)
    assert new_row.index_status == "ready" and new_row.deleted_at is None
    old_row = await db.get(Document, trashed_id)
    assert old_row.deleted_at is not None and old_row.index_status == "failed", (
        "回收站那行必须原样不动"
    )


@pytest.mark.asyncio
async def test_reimport_after_soft_delete_of_finish_stage_failure_gets_409(
    db, tmp_path, monkeypatch
):
    """腿①b（**收尾阶段失败** ⇒ 修后终态是 409，**不是**「新行 ready 可见」）。

    那行在 ``_finish_ingest`` 的失败分支里被补写了 ``content_hash`` ⇒ 重传时新建行会撞上
    **内容级重复判定**并命中回收站里那行 ⇒ 按既有口径 409 + 丢弃新行。

    **实测行数**（本机 MySQL 档）：修后 = **1 行**（只剩回收站那行），``list_documents == []``；
    修前 = **1 行**且被复活成 ``ready``（``deleted_at`` 仍在）、**不抛任何异常** ⇒ 本用例
    死在 ``pytest.raises(DuplicateDocument)`` 上（红点锚）。

    ⚠️ 这条腿的判别力是「**修前不抛且复活 / 修后抛 409 且回收站行原样**」——
    不要照腿①写成「新行 ready 可见」，那是同内容下**不可达**的假判据。
    """
    src = tmp_path / "same.md"
    src.write_text(_CONTENT, encoding="utf-8")
    data = src.read_bytes()
    _fail_persist_once(monkeypatch)

    with pytest.raises(RuntimeError):
        await ingest.ingest_document(db, src.name, data, stored_path=str(src))

    rows = await _rows(db)
    assert len(rows) == 1 and rows[0].index_status == "failed", f"前置条件不成立：{_dump(rows)}"
    trashed_id = rows[0].id
    assert rows[0].content_hash, (
        "前置条件：收尾阶段失败的行会被补写 content_hash（这就是终态不同的原因）"
    )
    await _soft_delete(db, trashed_id)

    with pytest.raises(DuplicateDocument) as exc_info:
        await ingest.ingest_document(db, src.name, data, stored_path=str(src))
    assert exc_info.value.document_id == trashed_id, "409 必须指向回收站那行"

    rows = await _rows(db)
    assert len(rows) == 1, (
        f"修后重传不该新增行（新建行被丢弃）、也不该复活回收站行；实际 {_dump(rows)}"
    )
    old_row = await db.get(Document, trashed_id)
    assert old_row.deleted_at is not None, "回收站那行必须原样留在回收站"
    assert old_row.index_status == "failed", "回收站那行不得被复活成 ready"
    assert await ingest.list_documents(db) == [], "回收站行不该出现在列表里"


# --------------------------------------------------------------------------
# 腿③ 不变量：重传后没有 pending/processing 残留、也没有第二行垃圾行
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reimport_after_soft_delete_leaves_no_residue(db, tmp_path, monkeypatch):
    """腿③ 不变量：行数精确、无 pending/processing 残留、分块归属正确。"""
    src = tmp_path / "same.md"
    src.write_text(_CONTENT, encoding="utf-8")
    data = src.read_bytes()
    _fail_parse_once(monkeypatch)

    with pytest.raises(DocumentParseError):
        await ingest.ingest_document(db, src.name, data, stored_path=str(src))
    rows = await _rows(db)
    trashed_id = rows[0].id
    await _soft_delete(db, trashed_id)

    result = await ingest.ingest_document(db, src.name, data, stored_path=str(src))

    rows = await _rows(db)
    assert len(rows) == 2, f"行数必须精确为 2（回收站 1 + 新行 1）：{_dump(rows)}"
    assert not _stuck(rows), f"重传后不得留下卡在 pending/processing 的行：{_dump(rows)}"
    assert {r.index_status for r in rows} == {"failed", "ready"}, (
        f"两行状态应恰为 {{failed(回收站), ready(新行)}}：{_dump(rows)}"
    )
    # 新行的分块真的落库了（不是空壳 ready）；回收站那行不得被写入新分块
    chunks = (
        await db.execute(select(Chunk).where(Chunk.document_id == result.document_id))
    ).scalars().all()
    assert chunks, "新行必须有分块"
    old_chunks = (
        await db.execute(select(Chunk).where(Chunk.document_id == trashed_id))
    ).scalars().all()
    assert old_chunks == [], "回收站那行不该被写入新分块"


# --------------------------------------------------------------------------
# 腿② 负控：**不在回收站**的 failed 行仍照旧被就地重试（id 不变）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_failed_row_is_still_reclaimed_in_place(db, tmp_path, monkeypatch):
    """负控：修复只该排除**回收站行**，不得把「按 file_path 认领失败行」一刀砍掉。

    这条修前修后都绿：它守的是「同一个坏文件每传一次就多一行 failed 垃圾行」的
    既有语义（``test_ingest.py:244`` 同口径），防止修复把认领机制整个删掉。
    """
    src = tmp_path / "same.md"
    src.write_text(_CONTENT, encoding="utf-8")
    data = src.read_bytes()
    _fail_persist_once(monkeypatch)

    with pytest.raises(RuntimeError):
        await ingest.ingest_document(db, src.name, data, stored_path=str(src))

    rows = await _rows(db)
    assert len(rows) == 1 and rows[0].index_status == "failed"
    failed_id = rows[0].id
    assert rows[0].deleted_at is None, "本腿的前提：失败行**不在**回收站"

    result = await ingest.ingest_document(db, src.name, data, stored_path=str(src))

    rows = await _rows(db)
    assert len(rows) == 1, f"不在回收站的 failed 行仍应就地重试（不留垃圾行）：{_dump(rows)}"
    assert result.document_id == failed_id, "就地重试必须复用同一 document id"
    fresh = await db.get(Document, failed_id)
    assert fresh.index_status == "ready" and fresh.deleted_at is None


# --------------------------------------------------------------------------
# 腿④ 直调单测：_find_failed_by_path 的口径（「最小复现」的机器化）
# --------------------------------------------------------------------------


def _doc(path: Path, *, status: str, deleted: bool) -> Document:
    """直接造一行（不跑入库）：本腿只判 ``_find_failed_by_path`` 的 where 口径。"""
    return Document(
        filename=path.name,
        file_type="md",
        status="failed" if status == "failed" else "ready",
        index_status=status,
        file_path=str(path),
        deleted_at=_TRASHED_AT if deleted else None,
    )


@pytest.mark.asyncio
async def test_find_failed_by_path_skips_trashed_row(db, tmp_path):
    """回收站里的 failed 行 ⇒ ``None``（**修前必红**：修前会把它返回，就是「复活」的入口）。"""
    path = tmp_path / "only-trashed.md"
    db.add(_doc(path, status="failed", deleted=True))
    await db.commit()

    found = await ingest._find_failed_by_path(db, path)
    assert found is None, (
        f"回收站里的 failed 行不得被认领；实际返回 id={getattr(found, 'id', None)} "
        f"(deleted_at={getattr(found, 'deleted_at', None)})"
    )


@pytest.mark.asyncio
async def test_find_failed_by_path_returns_live_row_even_below_trashed(db, tmp_path):
    """两行都在（回收站行 id 更大）⇒ 必须返回**在库**那行，不得被回收站行「压住」。

    id 顺序是刻意的：``_find_failed_by_path`` 取 ``id desc`` ⇒ 回收站行后插入、id 更大。
    修前会返回回收站那行（红），修后返回在库那行（绿）。
    """
    path = tmp_path / "both.md"
    live = _doc(path, status="failed", deleted=False)
    db.add(live)
    await db.commit()
    live_id = live.id

    db.add(_doc(path, status="failed", deleted=True))
    await db.commit()

    found = await ingest._find_failed_by_path(db, path)
    assert found is not None, "在库的 failed 行必须仍可被认领（不得把认领机制砍掉）"
    assert found.id == live_id, (
        f"应返回在库那行 id={live_id}（回收站行 id 更大，不得压住在库行）；实际 id={found.id}"
    )
    assert found.deleted_at is None
