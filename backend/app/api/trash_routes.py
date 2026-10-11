"""回收站路由（条目 25 / 阶段 4 · 条目 4.5.4）：挂进主 router，最终路径 /api/trash/*。

**语义与 4.5.1 的软删是一体两面**（本轮必须自洽）：

- 三类删除端点（``DELETE /api/chats/{id}`` 等）只置 ``deleted_at``，**子行不动**
  （会话的消息、文档的分块与向量、笔记的链接边都留在原位）⇒ 恢复零成本；
- 本站负责**站内列表 / 恢复 / 彻底删除 / 清空**四件事；
  - **恢复**：清掉 ``deleted_at`` 并把行还原到原位（消息/分块还在，界面上就「原样回来」）；
  - **彻底删除**：这才连同子行一起清——会话走 ``repository.hard_delete_chat``
    （先清 ``messages`` 再删会话），文档走既有的 ``pipeline.ingest.purge_document``
    （分块 + 向量一次清空），笔记删行后由 ``pipeline.wikilink.remove_links_for_deleted``
    清理链接边（与老基线 ``purge_note`` 同序：先删行、再重解析入边）；
  - **清空**：对站内全部条目逐条走与「彻底删除」**同一条**清理路径，返回三类条数。

``kind`` 取值域 = ``note`` / ``document`` / ``chat``（软删的三张表，冻结前端 ``TrashKind``
同值）。**路径参数写成 Literal**：非法 kind 由 FastAPI 给 **422**（契约 §4 的全局中文文案），
不是 400、更不是 500（``tests/contract/test_error_semantics.py::test_trash_invalid_kind_422``）。

响应字段（逐字对照冻结前端 ``TrashItem`` / ``TrashOut``）：

- 列表三组各是 ``{id, title, category?, deleted_at}``——**文档的 ``title`` 是文件名**，
  会话没有分类维度故不带 ``category``；按删除时间倒序；
- 恢复 / 彻底删除回 ``{"ok": true, "message": ...}``；
- 清空回 ``{"ok": true, "notes": n, "documents": n, "chats": n}``。
"""

from __future__ import annotations

from typing import Literal
from app.api.response_time import response_time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import invalidate_all
from app.db.session import get_db
from app.models import Note
from app.pipeline import wikilink
from app.pipeline.ingest import purge_document
from app.session import repository as repo

router = APIRouter()

#: ``kind`` 的封闭取值域（非法值由 FastAPI 给 422，见模块 docstring）。
TrashKind = Literal["note", "document", "chat"]


def _not_in_trash(kind: str) -> HTTPException:
    """404：回收站里没有这条（不存在，或者还在库 / 还没进站）。"""
    return HTTPException(status_code=404, detail=f"回收站中没有这条{repo.trash_label(kind)}")


def _note_item(row) -> dict:
    return {
        "id": int(row.id),
        "title": row.title or "",
        "category": row.category or "",
        "deleted_at": response_time(row.deleted_at, utc=True),
    }


def _document_item(row) -> dict:
    # 文档的展示名走 filename：api.ts 的 TrashItem 约定「文档 = 文件名」放进 title
    return {
        "id": int(row.id),
        "title": row.filename or "",
        "category": row.category or "",
        "deleted_at": response_time(row.deleted_at, utc=True),
    }


def _chat_item(row) -> dict:
    # 会话没有分类维度：TrashItem.category 是可选的，这里**不带**这个键（不发空占位）
    return {"id": int(row.id), "title": row.title or "", "deleted_at": response_time(row.deleted_at, utc=True)}


@router.get("/trash")
async def list_trash(db: AsyncSession = Depends(get_db)) -> dict:
    """回收站列表：三类各给最小字段，按删除时间倒序（刚删的在上）。"""
    rows = await repo.list_trash_rows(db)
    return {
        "notes": [_note_item(row) for row in rows["note"]],
        "documents": [_document_item(row) for row in rows["document"]],
        "chats": [_chat_item(row) for row in rows["chat"]],
    }


class RestoreIn(BaseModel):
    """恢复入参（冻结前端 ``restoreTrash(kind, id)`` 发的就是 ``{type, id}``）。"""

    type: TrashKind
    id: int = Field(ge=1)


@router.post("/trash/restore")
async def restore_trash(payload: RestoreIn, db: AsyncSession = Depends(get_db)) -> dict:
    """清掉 deleted_at 恢复原记录，再按当前同库资料重新判断双链目标。"""
    restored = await repo.restore_trashed_row(db, payload.type, payload.id)
    if not restored:
        raise _not_in_trash(payload.type)
    await db.commit()
    if payload.type in ("note", "document"):
        await wikilink.maintain_source_links(db, payload.type, payload.id)
    # 157 · 语义缓存失效面：恢复让行重新进入检索面（软删期间攒下的旧答案不得再被命中）⇒ 全失效。
    # 裸名 + 显式 import = 产品入口契约（app/cache/__init__.py 的 invalidate_all）。
    invalidate_all(reason=f"trash_restore:{payload.type}:{payload.id}")
    return {"ok": True, "message": "已恢复"}


async def _purge_one(db: AsyncSession, kind: str, item_id: int) -> None:
    """彻底删除一条（会话 / 文档 / 笔记各自的子行清理路径，**唯一登记处**）。"""
    if kind == "document":
        # 既有清理路径：分块 + 向量一次清空（自带 commit；向量清不掉只 warning）
        await purge_document(db, item_id)
    elif kind == "note":
        note = await db.get(Note, item_id)
        old_kb_id = int(note.kb_id or 1) if note is not None else None
        old_title = note.title if note is not None else None
        await repo.hard_delete_note(db, item_id)
        await db.commit()
        # 链接边是派生数据：删行**之后**重解析入边（查不到同名目标就转「未创建」）
        await wikilink.remove_links_for_deleted(
            db, "note", item_id, old_kb_id=old_kb_id, old_title=old_title
        )
    else:
        await repo.hard_delete_chat(db, item_id)  # 先清 messages 再删会话
        await db.commit()
    # 157 · 语义缓存失效面：本函数是彻底删除的**唯一登记处** ⇒ 单条彻底删除与「清空回收站」
    # 共用这一条失效接线（empty_trash 逐条走这里：首条已清空、其后各次返回 0）。
    invalidate_all(reason=f"trash_purge:{kind}:{item_id}")


@router.delete("/trash/{kind}/{item_id}")
async def purge_trash(
    kind: TrashKind, item_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    """彻底删除：连子行一起清（分块 / 向量 / 消息 / 链接边），不可恢复。"""
    row = await repo.get_trashed_row(db, kind, item_id)
    if row is None:
        raise _not_in_trash(kind)
    await _purge_one(db, kind, item_id)
    return {"ok": True, "message": "已彻底删除"}


@router.post("/trash/empty")
async def empty_trash(db: AsyncSession = Depends(get_db)) -> dict:
    """清空回收站：逐条走与「彻底删除」同一条清理路径，返回各类型条数。

    计数取**清理前**的站内行数（就是这次真的删掉的条数）；空站返回全 0，不报错。
    """
    rows = await repo.list_trash_rows(db)
    note_ids = [int(row.id) for row in rows["note"]]
    document_ids = [int(row.id) for row in rows["document"]]
    chat_ids = [int(row.id) for row in rows["chat"]]
    for item_id in note_ids:
        await _purge_one(db, "note", item_id)
    for item_id in document_ids:
        await _purge_one(db, "document", item_id)
    for item_id in chat_ids:
        await _purge_one(db, "chat", item_id)
    return {
        "ok": True,
        "notes": len(note_ids),
        "documents": len(document_ids),
        "chats": len(chat_ids),
    }
