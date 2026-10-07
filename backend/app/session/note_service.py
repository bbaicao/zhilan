"""笔记域的行级读写（阶段 4 · 条目 4.5.2）。

本模块是笔记域**唯一**的落库口径：行级读写（列表 / 新建 / 更新 / 软删）都从这里走，
HTTP 面（``app/api/note_routes.py``）只做参数校验与响应组装。放 ``session/`` 层是因为
它按仓库根 设计说明 的分包边界属于「db 之上的数据访问」——``api → retrieval → session``，
反向依赖（session import api）不存在。

三条口径（对齐老基线 ``<legacy-repo>\\backend\\app\\api\\__init__.py`` 的 475-538 段）：

1. **软删**：``DELETE /api/notes/{id}`` 只置 ``Note.deleted_at``（条目 25 回收站），
   行与链接边都保留——``POST /api/trash/restore`` 能原样恢复。彻底删除（连边一起清）
   是回收站域的职责（``DELETE /api/trash/note/{id}``），不在本模块。
2. **回收站里的笔记对笔记域不可见**：列表不列、更新与再删按「不存在」处理（404）。
   老基线的 PUT 漏了这一个判据（软删后仍可改），v2 补齐——与 chats 域
   （``GET/DELETE /api/chats/{id}`` 对软删会话一律 404）同一口径。
3. **双链维护是派生数据**：保存成功后调 ``wikilink.maintain_note_links``（重扫出链 +
   反向解析 + 改名重判入链）。它内部自带失败隔离——维护失败只记日志，**绝不让笔记保存失败**
   （I5：派生数据不反噬主流程）。

``tags`` / ``generation_meta`` 是 JSON 列：**只在有值时赋**——显式传 ``None`` 会被写成
JSON ``'null'`` 而不是 SQL ``NULL``（老基线 486-493 的同款注释）。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import invalidate_all
from app.core import derived_counters
from app.models import Note
from app.pipeline import lexical_index, wikilink  # noqa: F401 — 注册笔记 ORM 同事务词项维护。
from app.session import repository as repo

log = logging.getLogger(__name__)


async def list_notes(session: AsyncSession, category: str | None = None) -> list[Note]:
    """在库笔记（不含回收站），``id`` 倒序；``category`` 给了就按它过滤。

    排序与老基线逐字一致（新的在前）；``category`` 的语义也一致：**不传 = 不限制**，
    传空串 = 只要分类为空的那些（不做「空串等于不传」的归一，免得悄悄多回别的行）。
    """
    stmt = select(Note).where(Note.deleted_at.is_(None)).order_by(Note.id.desc())
    if category is not None:
        stmt = stmt.where(Note.category == category)
    return list((await session.execute(stmt)).scalars().all())


async def get_note(session: AsyncSession, note_id: int) -> Note | None:
    """按 id 取**在库**笔记；回收站里的按「不存在」返回 ``None``（口径见模块 docstring）。"""
    note = await session.get(Note, int(note_id))
    if note is None or note.deleted_at is not None:
        return None
    return note


async def create_note(
    session: AsyncSession,
    *,
    title: str,
    content: str,
    category: str = "",
    source_document_id: int | None = None,
    tags: list | None = None,
    generation_meta: dict | None = None,
) -> Note:
    """新建笔记并维护双链，返回落库后的行。

    顺序与老基线一致：**先提交笔记行 → 再维护链接边**。维护失败不改返回结果
    （``maintain_note_links`` 自己吞异常并记日志）。
    """
    note = Note(title=title, content=content, category=category)
    if source_document_id is not None:
        note.source_document_id = int(source_document_id)
    if tags is not None:
        note.tags = tags
    if generation_meta is not None:
        note.generation_meta = generation_meta
    session.add(note)
    await session.commit()
    await session.refresh(note)
    await maintain_links(session, note)
    # 157 · 语义缓存失效面：排在**写路径的最后一步**（``maintain_links`` 自己也会写双链边 ⇒
    # 失效必须落在它之后；它内部自吞异常并只回滚自己那部分，不改本行必达）。
    # 裸名 + 显式 import 是**产品入口契约**（app/cache/__init__.py 的 invalidate_all）；
    # ⛔ 不得退化成属性式 `_cache_store.invalidate_all(...)`（静态腿捕获不到 = 视同规避）。
    # 不 try/except：invalidate_all 是正确性动作，失败必须可见（store.invalidate_all docstring ②）。
    invalidate_all(reason=f"note_create:{note.id}")
    return note


async def update_note(
    session: AsyncSession,
    note_id: int,
    *,
    title: str | None = None,
    content: str | None = None,
    category: str | None = None,
    tags: list | None = None,
    generation_meta: dict | None = None,
) -> Note | None:
    """更新笔记（``None`` = 该字段不动）；不存在 / 在回收站返回 ``None``。

    ``source_document_id`` 不收（契约 ``NoteUpdate`` 没有它）——覆盖保存时不动溯源字段。
    """
    note = await get_note(session, note_id)
    if note is None:
        return None
    old_title = note.title
    if title is not None:
        note.title = title
    if content is not None:
        note.content = content
    if category is not None:
        note.category = category
    if tags is not None:
        note.tags = tags
    if generation_meta is not None:
        note.generation_meta = generation_meta
    await session.commit()
    await session.refresh(note)
    await maintain_links(session, note, old_title=old_title)
    # 157 · 语义缓存失效面：**元数据也算变化** —— 存活校验（app/cache/entry.py 的
    # fingerprints_live）只比 Note.content 三候选指纹，改标题 / 分类 / 标签查不出来，
    # 所以更新路径一律全失效（拍板 #6 = A）。裸名形态与理由同上；
    # 排在**写路径最后一步**（``maintain_links`` 亦写双链边，失败在其内部吞掉并回滚）。
    invalidate_all(reason=f"note_update:{note.id}")
    return note


async def soft_delete_note(session: AsyncSession, note_id: int) -> Note | None:
    """软删并重判相关链接；出链标题保留用于恢复，不存在 / 已删除返回 None。"""
    note = await get_note(session, note_id)
    if note is None:
        return None
    note.deleted_at = repo.utc_now()
    await session.commit()
    await maintain_links(session, note)
    # 157 · 语义缓存失效面：软删后笔记材料在缓存答案里仍会被引用（存活校验的
    # note 支路只看 content 指纹，不看 deleted_at 之外的行状态变化窗口）⇒ 全失效。
    invalidate_all(reason=f"note_soft_delete:{note.id}")
    return note


async def maintain_links(session: AsyncSession, note: Note, old_title: str | None = None) -> bool:
    """双链维护（派生数据，失败只记日志）——``回归修复`` 的**边写入调用点**。

    ``回归修复`` 登记的根因是「``wiki_links`` 没有任何写入者」：``maintain_note_links``
    本身是现成的，但全仓零调用点 ⇒ 补一行接线就等于零效果。本函数把调用点钉在笔记域的
    保存路径上；文档侧的边写入在 ``app/pipeline/ingest.py`` 的入库收口（同批补）。
    """
    note_id = int(note.id)
    try:
        return await wikilink.maintain_note_links(session, note, old_title=old_title)
    except Exception as exc:  # noqa: BLE001 —— 派生数据失败不反噬保存（I5）
        try:
            await session.rollback()
            await session.refresh(note)
        except Exception as rollback_exc:  # noqa: BLE001
            log.warning("笔记 %s 的已提交状态恢复失败：%s", note_id, rollback_exc)
        log.warning("笔记 %s 的双链维护失败（不影响保存）：%s", note_id, exc)
        # `回归修复`：外层吞点同样要计数。⚠️ **与内层重复计数是【有意的】** ——
        # 两层吞的是**不同的失败**（内层＝链接维护本身出错；外层＝连 rollback 都没兜住的）
        # ⇒ 计数只增不减，宁可多一个信号也不要漏。
        derived_counters.bump("note_links")
        return False


__all__ = [
    "create_note",
    "get_note",
    "list_notes",
    "maintain_links",
    "soft_delete_note",
    "update_note",
]
