"""会话分组 + 归档路由（条目 40 / 阶段 4 · 条目 4.5.4）：挂进主 router。

分组是**纯侧栏组织**：不改变提问与检索范围，也不是「先选项目再开对话」的 Project 模式。
归档 ≠ 回收站（条目 25）：归档只是从「最近」隐藏、可随时恢复；**归档视图里的「删除」仍走
既有的 ``DELETE /api/chats/{id}`` 软删进回收站**，本模块不做物理删除，也不碰 ``deleted_at``。

契约口径（逐字对照冻结前端 ``ChatGroup`` 与 ``api.ts`` 的八个调用）：

- ``ChatGroup`` = ``{id, name, sort, created_at, chat_count}``；``chat_count`` **只数未归档的
  在库会话**（界面角标口径：归档与回收站里的都不计）；
- ``GET /api/chat-groups`` 按 ``sort`` 升序、同序按创建先后；
- ``POST`` 的 ``name`` 有 1..100 的下限/上限校验（空串 ⇒ 422；只剩空白 ⇒ 400 可读中文，
  两条都不许 500）；``PUT`` 的 ``name`` / ``sort`` **不传就不动**；
- ``DELETE`` 分组：组内会话回「最近」平铺（``group_id`` 置 NULL），**会话本身不删**；
- ``PUT /api/chats/{chat_id}/group``：``group_id`` 传 ``null`` 表示移出分组；未知分组 404、
  回收站里的会话按不存在处理（404「会话不存在」）；
- 归档 / 取消归档：**幂等**，且都把响应收敛成冻结前端的 ``{ok: true}``
  （老基线这两条回的是会话对象；前端只读 ``ok``，故按前端声明为准，见证据文件的差异登记）。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models import Chat, SessionGroup
from app.session import repository as repo

router = APIRouter()


class GroupOut(BaseModel):
    """冻结前端 ``ChatGroup`` 的五个字段，逐字一致。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    sort: int
    created_at: datetime
    # 组内会话数：只数未归档的在库会话
    chat_count: int = 0


class GroupIn(BaseModel):
    """新建分组：``name`` 必填、1..100（空串走 422，与契约测试同口径）。"""

    name: str = Field(min_length=1, max_length=100)


class GroupUpdate(BaseModel):
    """改名 / 排序：``None`` = 不动该字段（二选一或同传）。"""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    sort: int | None = None


class ChatGroupIn(BaseModel):
    """会话归属：``None`` = 移出分组（回「最近」平铺）。"""

    group_id: int | None = None


async def _require_group(db: AsyncSession, group_id: int) -> SessionGroup:
    group = await repo.get_group(db, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="分组不存在")
    return group


async def _require_live_chat(db: AsyncSession, chat_id: int) -> Chat:
    """分组 / 归档只对在库会话生效；回收站里的会话按不存在处理（与 4.5.1 同口径）。"""
    chat = await repo.get_chat(db, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return chat


def _group_out(group: SessionGroup, counts: dict[int, int]) -> GroupOut:
    return GroupOut(
        id=int(group.id),
        name=group.name,
        sort=int(group.sort),
        created_at=group.created_at,
        chat_count=counts.get(int(group.id), 0),
    )


def _clean_name(raw: str) -> str:
    """分组名归一：``strip`` 后不能为空（400 可读中文），长度截到列宽 100。"""
    name = (raw or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="分组名不能为空")
    return name[:100]


@router.get("/chat-groups", response_model=list[GroupOut])
async def list_chat_groups(db: AsyncSession = Depends(get_db)) -> list[GroupOut]:
    """分组列表（含各组成员数）：按 ``sort`` 升序、同序按创建先后。"""
    groups = await repo.list_groups(db)
    counts = await repo.group_counts(db)
    return [_group_out(group, counts) for group in groups]


@router.post("/chat-groups", response_model=GroupOut)
async def create_chat_group(payload: GroupIn, db: AsyncSession = Depends(get_db)) -> GroupOut:
    """新建分组：空名 400，重名**允许**（老基线不查重，侧栏靠 id 区分）。"""
    group = await repo.create_group(db, name=_clean_name(payload.name))
    await db.commit()
    await db.refresh(group)
    return _group_out(group, {})


@router.put("/chat-groups/{group_id}", response_model=GroupOut)
async def update_chat_group(
    group_id: int, payload: GroupUpdate, db: AsyncSession = Depends(get_db)
) -> GroupOut:
    """重命名与排序（二选一或同传）：``None`` 表示不动。未知分组 404。"""
    group = await _require_group(db, group_id)
    if payload.name is not None:
        group.name = _clean_name(payload.name)
    if payload.sort is not None:
        group.sort = int(payload.sort)
    await db.commit()
    await db.refresh(group)
    return _group_out(group, await repo.group_counts(db))


@router.delete("/chat-groups/{group_id}")
async def delete_chat_group(group_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """删除分组：组内会话回「最近」平铺（``group_id`` 置 NULL），会话本身不删。"""
    await _require_group(db, group_id)
    await repo.delete_group(db, group_id)
    await db.commit()
    return {"ok": True, "message": "分组已删除，组内会话回到「最近」"}


@router.put("/chats/{chat_id}/group")
async def set_chat_group(
    chat_id: int, payload: ChatGroupIn, db: AsyncSession = Depends(get_db)
) -> dict:
    """把会话移入分组；``group_id`` 传 ``null`` 表示移出分组。未知分组 404（不静默丢归属）。"""
    chat = await _require_live_chat(db, chat_id)
    if payload.group_id is not None:
        await _require_group(db, payload.group_id)
        chat.group_id = int(payload.group_id)
    else:
        chat.group_id = None
    await db.commit()
    return {"ok": True}


@router.post("/chats/{chat_id}/archive")
async def archive_chat(chat_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """归档：从「最近」隐藏（分组归属保留，取消归档后回到原分组）。**幂等**。"""
    chat = await _require_live_chat(db, chat_id)
    if chat.archived_at is None:
        chat.archived_at = repo.utc_now()
        await db.commit()
    return {"ok": True}


@router.post("/chats/{chat_id}/unarchive")
async def unarchive_chat(chat_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """取消归档：回到「最近」（原分组还在就回原分组；分组已删则平铺）。**幂等**。"""
    chat = await _require_live_chat(db, chat_id)
    if chat.archived_at is not None:
        chat.archived_at = None
        await db.commit()
    return {"ok": True}
