"""messages / chats 行级数据访问（阶段 1 · 条目 1.1）。

职责边界（分包：``api → routing → session → db/core``）：

- 本模块**只做行级读写**：不写业务规则、不管事务提交——提交归 ``service.py``；
- 上层（routing / generation / api）一律经 ``app/session/service.py`` 使用，
  不直接构造 ORM 行；
- 真相源在 MySQL ``messages`` 表；**禁止**把消息放进 Redis（统筹 §2.4 不变量）。

取值词汇表来自对外契约与统筹 §3.8，落库前校验，避免脏值进库：
- ``role``：user / assistant / system
- ``source_type``：internal_db（库内命中）/ external（库外直答）
- ``kind``：normal / clarify / refuse / direct / stopped（02 §7.1 + 停止轮）
- ``answer_source``：kb / general / empty_scope / clarify（02 §7.1）

写入返回**已填充**的 ORM 行（server_default 列已 refresh），调用方拿到的对象
可直接读 ``id`` / ``created_at``，不会在 async 下踩到惰性加载。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chat, Document, Message, MessageAttribution, Note, SessionGroup


def utc_now() -> datetime:
    """naive UTC 时间戳（与全库 ``created_at`` / ``deleted_at`` 列同口径）。

    软删（条目 25 回收站）在各域都要置时间戳：``documents`` 域在
    ``document_routes._utc_now`` 里有一份同形实现（先于本函数落地，**本轮不代改**），
    chats / notes 域从这里取，避免第三、第四份副本。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)

# --- 取值词汇表 ---------------------------------------------------------

ROLES: tuple[str, ...] = ("user", "assistant", "system")

# 统筹 §3.8：内部 source_type 与契约 kind / answer_source 的映射由 generation 收口，
# 本层只保证存进去的是这两个合法值之一。
SOURCE_TYPES: tuple[str, ...] = ("internal_db", "external")

KINDS: tuple[str, ...] = ("normal", "clarify", "refuse", "direct", "stopped")

# 🔴 2026-09-27 v1.0.11（回归修复）：第五值 ``web``（联网材料轮，answer_source_for_materials 覆写）。
ANSWER_SOURCES: tuple[str, ...] = ("kb", "general", "empty_scope", "clarify", "web")


class ChatNotFound(Exception):
    """会话不存在或已进回收站。

    领域异常：session 层不 import fastapi，由 api 层（1.5）映射成 404「会话不存在」。
    """


def _check(value: str, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise ValueError(f"{field} 取值不合法：{value!r}（允许：{'/'.join(allowed)}）")
    return value


# --- chats --------------------------------------------------------------


async def create_chat(db: AsyncSession, *, title: str) -> Chat:
    """新建会话（不提交）。``title`` 由 service 按老基线口径推导。"""
    chat = Chat(title=title)
    db.add(chat)
    await db.flush()
    await db.refresh(chat)
    return chat


async def get_chat(
    db: AsyncSession, chat_id: int, *, include_deleted: bool = False
) -> Chat | None:
    """按 id 取会话。

    默认把回收站里的会话视为不存在（老基线：``deleted_at`` 非空按 404 处理）；
    回收站列表/恢复要看到它们时传 ``include_deleted=True``。
    """
    chat = await db.get(Chat, chat_id)
    if chat is None:
        return None
    if chat.deleted_at is not None and not include_deleted:
        return None
    return chat


async def list_chats(db: AsyncSession, *, archived: bool = False) -> list[Chat]:
    """会话列表：排除回收站；默认排除已归档（老基线 ``GET /api/chats`` 口径）。"""
    stmt = select(Chat).where(Chat.deleted_at.is_(None))
    if archived:
        stmt = stmt.where(Chat.archived_at.is_not(None))
    else:
        stmt = stmt.where(Chat.archived_at.is_(None))
    result = await db.execute(stmt.order_by(Chat.id.desc()))
    return list(result.scalars().all())


# --- messages -----------------------------------------------------------


async def add_message(
    db: AsyncSession,
    *,
    chat_id: int,
    role: str,
    content: str,
    source_type: str | None = None,
    kind: str = "normal",
    refs: list | dict | None = None,
    filters: list | dict | None = None,
    reasoning: str | None = None,
    thinking: dict | None = None,
    skill_ids: list | None = None,
    agent_trace: dict | None = None,
    images: list | None = None,
    answer_source: str | None = None,
) -> Message:
    """追加一条消息（不提交）。

    字段集与契约 ``MessageOut``（02 §7.4）对齐，供 1.5 的 /chat 落库与 4.5 的
    ``GET /chats/{id}`` 回读使用；``refs`` 由阶段 3 填，本阶段恒为空。
    """
    _check(role, ROLES, "role")
    _check(kind, KINDS, "kind")
    if source_type is not None:
        _check(source_type, SOURCE_TYPES, "source_type")
    if answer_source is not None:
        _check(answer_source, ANSWER_SOURCES, "answer_source")

    message = Message(
        chat_id=chat_id,
        role=role,
        content=content,
        source_type=source_type,
        kind=kind,
        refs=refs,
        filters=filters,
        reasoning=reasoning,
        thinking=thinking,
        skill_ids=skill_ids,
        agent_trace=agent_trace,
        images=images,
        answer_source=answer_source,
    )
    db.add(message)
    await db.flush()
    # server_default 列（created_at）在 flush 后仍是未加载状态；async 下惰性加载会抛
    # MissingGreenlet，所以这里显式 refresh，保证返回的行可直接读全部字段。
    await db.refresh(message)
    return message


async def add_attributions(db: AsyncSession, rows: list[dict]) -> int:
    """批量写「消息 ↔ 分块」归因（阶段 3 · 条目 3.2），返回写入条数（不提交）。

    行由 ``app.generation.citation.attribution_rows`` 生成——它保证
    ``ordinal`` 与资料区 ``[n]`` 同源（条目 3.1 冻结的快照编号）、``chunk_id`` 是 ``chunks.id``。
    本函数**只管落库**：不做引用判定，也不替调用方决定写哪些行。
    """
    items = [MessageAttribution(**row) for row in rows or []]
    if not items:
        return 0
    db.add_all(items)
    await db.flush()
    return len(items)


async def list_messages(db: AsyncSession, chat_id: int) -> list[Message]:
    """按 id 升序取会话内全部消息（老基线 ``GET /api/chats/{id}`` 口径）。"""
    result = await db.execute(
        select(Message).where(Message.chat_id == chat_id).order_by(Message.id)
    )
    return list(result.scalars().all())


async def delete_messages(db: AsyncSession, chat_id: int) -> int:
    """清空会话内消息（回收站「彻底删除」用；软删不调用本函数）。返回删除行数。"""
    result = await db.execute(delete(Message).where(Message.chat_id == chat_id))
    return int(result.rowcount or 0)


# --- 回收站（条目 25；阶段 4 · 条目 4.5.4）-------------------------------


#: 回收站的 kind 取值域（= 软删的三张表）；冻结前端 ``TrashKind`` 同值。
TRASH_KINDS: tuple[str, ...] = ("note", "document", "chat")

#: kind → (模型, 可读名)。列表 / 恢复 / 彻底删除共用这一份登记，不各写一套映射。
_TRASH_MODELS: dict[str, tuple[type, str]] = {
    "note": (Note, "笔记"),
    "document": (Document, "文档"),
    "chat": (Chat, "会话"),
}


def trash_label(kind: str) -> str:
    """kind 的可读中文名（笔记 / 文档 / 会话）——404 文案与统计共用。"""
    return _TRASH_MODELS[kind][1]


async def list_trash_rows(db: AsyncSession) -> dict[str, list]:
    """回收站列表：三类各给**全部行**，按删除时间倒序（刚删的在上）。

    返回 ``{kind: [row, ...]}``；字段裁剪与响应组装归 api 层（本层只做行级读写）。
    """
    rows: dict[str, list] = {}
    for kind, (model, _name) in _TRASH_MODELS.items():
        result = await db.execute(
            select(model)
            .where(model.deleted_at.is_not(None))
            .order_by(model.deleted_at.desc(), model.id.desc())
        )
        rows[kind] = list(result.scalars().all())
    return rows


async def get_trashed_row(db: AsyncSession, kind: str, item_id: int):
    """取回收站里的一行；不存在**或还没进站**（``deleted_at`` 为空）都返回 ``None``。

    调用方（trash 路由）据此给 404——回收站里的东西对用户是「不存在」，
    在库的东西对回收站也是「不存在」，两个方向都不串味。
    """
    model, _name = _TRASH_MODELS[kind]
    row = await db.get(model, item_id)
    if row is None or getattr(row, "deleted_at", None) is None:
        return None
    return row


async def restore_trashed_row(db: AsyncSession, kind: str, item_id: int) -> bool:
    """从回收站恢复：只把 ``deleted_at`` 置回 ``NULL``（**不提交**）。

    子行（``messages`` / ``chunks``）软删期间一行没动，所以恢复是零成本的：
    会话的消息、文档的分块与向量都还在原位（与 4.5.1 的软删口径互为一体）。
    返回是否真的恢复了一行。
    """
    row = await get_trashed_row(db, kind, item_id)
    if row is None:
        return False
    row.deleted_at = None
    return True


async def hard_delete_note(db: AsyncSession, note_id: int) -> None:
    """彻底删除笔记行（**不提交**）。

    链接边（``wiki_links``）的清理**不在这里**：那是 ``app.pipeline.wikilink`` 的职责，
    而分包方向是 ``api → pipeline → session``——``session`` 层 import ``pipeline`` 是
    反向依赖。故由 api 层（``trash_routes``）在删行之后调
    ``wikilink.remove_links_for_deleted``（与老基线 purge_note 同序）。
    """
    row = await db.get(Note, note_id)
    if row is not None:
        await db.delete(row)


async def hard_delete_chat(db: AsyncSession, chat_id: int) -> int:
    """彻底删除会话：**连消息一起清**（软删不调用本函数）。返回删掉的消息行数（不提交）。

    会话事件（``session_events``）随 ``chats`` 的外键级联删除（``ondelete=CASCADE``，
    见 models 的不变量 3），无需手工清。
    """
    deleted = await delete_messages(db, chat_id)
    row = await db.get(Chat, chat_id)
    if row is not None:
        await db.delete(row)
    return deleted


# --- 会话分组 / 归档（条目 40；阶段 4 · 条目 4.5.4）----------------------


async def list_groups(db: AsyncSession) -> list[SessionGroup]:
    """分组列表：按 ``sort`` 升序、同序按创建先后（老基线口径）。"""
    result = await db.execute(select(SessionGroup).order_by(SessionGroup.sort, SessionGroup.id))
    return list(result.scalars().all())


async def group_counts(db: AsyncSession) -> dict[int, int]:
    """一次聚合出各分组的**未归档在库**会话数（界面角标口径）。

    回收站里的（``deleted_at`` 非空）与已归档的（``archived_at`` 非空）都不计入
    ——与老基线 ``_group_counts`` 逐条一致。
    """
    rows = await db.execute(
        select(Chat.group_id, func.count(Chat.id))
        .where(Chat.deleted_at.is_(None), Chat.archived_at.is_(None), Chat.group_id.is_not(None))
        .group_by(Chat.group_id)
    )
    return {int(group_id): int(count) for group_id, count in rows.all()}


async def get_group(db: AsyncSession, group_id: int) -> SessionGroup | None:
    """按 id 取分组；不存在返回 ``None``（调用方给 404「分组不存在」）。"""
    return await db.get(SessionGroup, group_id)


async def create_group(db: AsyncSession, *, name: str, sort: int = 0) -> SessionGroup:
    """新建分组（不提交）：``name`` 已由调用方 strip / 截断 / 校验。"""
    group = SessionGroup(name=name, sort=int(sort))
    db.add(group)
    await db.flush()
    await db.refresh(group)
    return group


async def delete_group(db: AsyncSession, group_id: int) -> int:
    """删除分组（不提交）：组内会话**回「最近」平铺**（``group_id`` 置 NULL），会话本身不删。

    分组表没有外键约束（纯侧栏组织），所以归属要在应用层放回去；返回放回的会话数。
    """
    result = await db.execute(
        update(Chat)
        .where(Chat.group_id == group_id)
        .values(group_id=None)
        .execution_options(synchronize_session=False)
    )
    group = await db.get(SessionGroup, group_id)
    if group is not None:
        await db.delete(group)
    return int(result.rowcount or 0)


__all__ = [
    "ANSWER_SOURCES",
    "KINDS",
    "ROLES",
    "SOURCE_TYPES",
    "TRASH_KINDS",
    "ChatNotFound",
    "add_attributions",
    "add_message",
    "create_chat",
    "create_group",
    "delete_group",
    "delete_messages",
    "get_chat",
    "get_group",
    "get_trashed_row",
    "group_counts",
    "hard_delete_chat",
    "hard_delete_note",
    "list_chats",
    "list_groups",
    "list_messages",
    "list_trash_rows",
    "restore_trashed_row",
    "trash_label",
    "utc_now",
]
