"""`回归修复` 的永久回归钉：**笔记材料必须与文档块同强度地过缓存存活校验**。

## 缺陷是什么（`5.6` 的增强组合矩阵在**三开格**里用只读探针实测暴露）

图通道带回的**笔记材料**用负数 ``chunk_id`` 占位（``app/retrieval/types.py::note_chunk_id``），
真正的身份在 ``Material.note_id``。而修复前：

- **写回侧** ``app/api/chat_routes.py::_cache_write_entry`` 只收 ``chunk_id > 0`` 的材料
  ⇒ 笔记材料**根本不进指纹表**（探针实测 ``fingerprints == {}``）；
- **命中侧** ``app/cache/entry.py::fingerprints_live`` 把非正 id **一律跳过**
  ⇒ 笔记材料**永不被校验**。

合起来的后果：**笔记被（软）删掉之后，缓存照样命中，``refs`` 里还带着那条已经不存在的笔记**
（探针实测：``hits=1``、模型调用 0 次）。

## 为什么钉在函数级，而不是再写一遍端到端

发现路径已经常驻在 ``tests/api/test_enh_matrix_5_6.py::_note_material_probe``（三开格里）——
但那条探针按开工包 §7.2 第 14 条的要求**只探只报、不判红**（它是 8 格判据的一部分，
不该被一条边界拖红）。⇒ 本文件把**两半各自钉死**：

1. **写回侧**：笔记材料进 ``fingerprints`` 的**键形态**（``note:<id>``，而不是那个负数）；
2. **命中侧**：三态（活的 ⇒ 命中 / 软删 ⇒ 不命中 / 改过内容 ⇒ 不命中）
   **外加**「既有的 ``chunks`` 路径未被改动」这条防回归。

两半任一退回去，本文件都会红。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import chat_routes
from app.cache import entry as cache_entry
from app.models import Chunk, Note
from app.retrieval.types import Material
from tests.retrieval.conftest import seed_document

pytestmark = pytest.mark.asyncio


def _note_material(note_id: int, content: str) -> Material:
    """照图通道构造笔记材料（``app/retrieval/graph.py`` 的笔记分支）。

    ⚠️ 两个细节必须与产品一致，否则这条用例会**假绿**：
    ``chunk_id`` 是 **负数占位**、``content`` 是 ``note.content.strip()`` 之后的值。
    """
    return Material(
        chunk_id=-int(note_id),
        document_id=0,
        content=content.strip(),
        filename="存活校验探针.md",
        category="探针分类",
        kb_id=1,
        note_id=int(note_id),
    )


# ==========================================================================
# 一、写回侧：键形态
# ==========================================================================


async def test_write_back_records_note_material_fingerprint(monkeypatch):
    """笔记材料必须进 ``fingerprints``，键是 ``note:<id>`` 而**不是**那个负数目。

    同一轮里三类材料一起过：**文档块**（口径不变，纯数字键）/ **笔记**（新）/
    **网页材料**（没有库内行 ⇒ 继续跳过——不许因为「查不到」就把它算进去，那会让每一轮都判未命中）。
    """
    monkeypatch.setattr(chat_routes, "attempt_provider_model", lambda: ("probe", "probe-model"))
    note = _note_material(7, "  笔记正文（前后带空白，与图通道口径一致）  ")
    document_chunk = Material(chunk_id=12, document_id=1, content="文档块正文", filename="a.md")
    web = Material(
        chunk_id=-1, document_id=0, content="网页正文", filename="web", url="https://example.test/x"
    )
    plan = SimpleNamespace(
        question="探针问题",
        materials=[note, document_chunk, web],
        materials_text="",
        retrieval_warning=None,
        retrieval_stats=None,
        memory_text="",
    )

    entry = chat_routes._cache_write_entry(plan, "探针答案", None)
    fingerprints = dict(entry.fingerprints)

    assert fingerprints["note:7"] == cache_entry.content_fingerprint(note.content), (
        "写回侧必须给笔记材料单列 `note:<id>` 键（`回归修复` 修复前这里是空的）"
    )
    assert "-7" not in fingerprints, "负数 `chunk_id` **不许**再当键——它认不出该查哪张表"
    assert fingerprints["12"] == cache_entry.content_fingerprint("文档块正文"), "文档块口径不许变"
    assert len(fingerprints) == 2, f"网页材料仍应跳过；实际 keys={sorted(fingerprints)}"


# ==========================================================================
# 二、命中侧：三态 + 既有 chunks 路径防回归
# ==========================================================================


async def _make_note(db, content: str) -> Note:
    note = Note(title="存活校验探针", content=content, category="探针分类", kb_id=1)
    db.add(note)
    await db.commit()
    return note


def _note_fingerprint(note: Note) -> dict[str, str]:
    """写回侧对笔记材料算的就是这个值（`Material.content` = `(note.content or "").strip()`）。"""
    return {
        f"note:{int(note.id)}": cache_entry.content_fingerprint((note.content or "").strip())
    }


async def test_liveness_accepts_untouched_note(db):
    """**正例（防「判据永远红」）**：笔记没被动过 ⇒ 仍然命中。

    没有这一条，把存活校验写成「笔记一律不命中」也能让另外两条绿——那是**假证据**
    （并且等于把带笔记的轮次永久踢出缓存）。
    """
    note = await _make_note(db, "原始正文")
    assert await cache_entry.fingerprints_live(db, _note_fingerprint(note)) is True


async def test_liveness_rejects_soft_deleted_note(db):
    """**缺陷本体**：笔记被软删（`deleted_at` 非空）⇒ 必须判「不命中」。

    ⚠️ 只查「``notes`` 行还在不在」是**不够**的：本仓删笔记是**软删**——行一直留着
    （``app/api/note_routes.py::delete_note`` 只置 ``deleted_at``）。
    """
    note = await _make_note(db, "原始正文")
    fingerprints = _note_fingerprint(note)
    note.deleted_at = datetime.now()
    await db.commit()
    assert await cache_entry.fingerprints_live(db, fingerprints) is False


async def test_liveness_rejects_edited_note(db):
    """笔记正文被改过 ⇒ 必须判「不命中」（与文档块「内容指纹」同强）。"""
    note = await _make_note(db, "原始正文")
    fingerprints = _note_fingerprint(note)
    note.content = "被改过的正文"
    await db.commit()
    assert await cache_entry.fingerprints_live(db, fingerprints) is False


async def test_liveness_skips_materials_without_backing_row(db):
    """没有库内行的材料（网页等）继续**跳过**：它们的键既不是纯数字、也不是 ``note:``。"""
    assert await cache_entry.fingerprints_live(db, {"-3": "whatever", "web:abc": "whatever"}) is True


async def test_liveness_keeps_chunk_path_unchanged(db):
    """防回归：既有的 ``chunks`` 分支**一字未变**（在 ⇒ 命中 / 不在 ⇒ 不命中）。"""
    await seed_document(db, ["文档块甲。", "文档块乙。"], filename="liveness.md")
    rows = (await db.execute(select(Chunk.id, Chunk.content))).all()
    fingerprints = {str(int(row[0])): cache_entry.content_fingerprint(row[1]) for row in rows}
    assert fingerprints, "前提校验：`seed_document` 应当建出至少一个块"
    assert await cache_entry.fingerprints_live(db, fingerprints) is True

    missing = dict(fingerprints)
    missing["999999999"] = cache_entry.content_fingerprint("根本不存在的块")
    assert await cache_entry.fingerprints_live(db, missing) is False, "块被硬删 ⇒ 必须不命中"
