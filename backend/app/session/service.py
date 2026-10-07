"""消息与会话的业务读写入口（阶段 1 · 条目 1.1）。

对上层提供**轮次级**语义，把「事务提交」与「会话取回或新建」收在一处，
让 1.5 的 ``/chat`` 与 ``/chat/stream`` 只关心「存一问一答」：

    chat = await resolve_chat(db, chat_id=None, message="今天天气怎么样")
    await append_user_message(db, chat.id, "今天天气怎么样", ...)
    await append_assistant_message(db, chat.id, "……", kind="direct", ...)

行为对齐老系统基线（老系统源码只读快照对照，不整文件搬运）：

- 会话标题取首问前 40 字、换行折成空格；空正文回「新会话」；
- 历史只取 user / assistant 两角色（system 不进模型历史）；
- 会话在回收站里（``deleted_at`` 非空）按不存在处理。

不变量：消息真相源在 MySQL；本模块**不碰 Redis**。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chat, Message
from app.session import repository as repo

logger = logging.getLogger(__name__)

# 老基线口径：标题取首问前 40 字
TITLE_MAX_LEN = 40


def chat_title(message: str) -> str:
    """按首问推导会话标题（老基线 ``_chat_title`` 的文本分支）。

    图片分支（「图片问答」）属 1.5 的 /chat 请求解析，本条目不做图片。
    """
    text = (message or "").strip().replace("\n", " ")
    return text[:TITLE_MAX_LEN] if text else "新会话"


async def resolve_chat(
    db: AsyncSession, *, chat_id: int | None, message: str
) -> Chat:
    """取回既有会话，或按首问新建一个。

    ``chat_id`` 为 None → 新建；给定但不存在/已进回收站 → ``ChatNotFound``
    （api 层映射成 404「会话不存在」，与老基线一致）。

    ⚠️ **新建的会话行在这里就 ``commit``**（阶段 4 · 条目 4.6 真机实测，
    `回归修复`；原先只 ``flush``）。为什么必须提前落地：

    一轮的 DB 工作**攒在同一个事务里**（会话行 / 路由事件 / 工具事件 / 审计行 / 最终消息），
    直到本轮第一次 ``commit`` 才一起落库。而「停止」是一条**取消**链：真机实测取消正好
    命中请求会话在途的那条语句（`tool/result` 的 INSERT）⇒ SQLAlchemy 把事务标成
    **pending-rollback**（后续语句一律 `PendingRollbackError`）⇒ 唯一出路是
    ``runner._safe_finish`` 里那次恢复性 ``rollback()`` ⇒ **尚未提交的会话行被一起抹掉**；
    紧接着 `persist_stopped_turn` 要落「用户消息」，`messages.chat_id → chats.id` 的
    **外键直接冲突**（1452）⇒ 这一轮**连提问一起消失**（真机 `stop-before`：chat 42 整条
    不存在，而 `AUTO_INCREMENT` 已消耗 44/100/278 —— 行被 flush 过、又被回滚掉）。

    会话行先落地之后，无论后面怎么取消/回滚，**提问始终还有落点**（本轮其余留痕仍可能
    随事务丢，那由 `events_incomplete` 如实记着）。语义也回到老基线：会话是在生成之前
    就存在的，不是回答的副产品。
    """
    if chat_id is None:
        chat = await repo.create_chat(db, title=chat_title(message))
        await db.commit()
        return chat
    chat = await repo.get_chat(db, chat_id)
    if chat is None:
        raise repo.ChatNotFound(f"会话不存在：{chat_id}")
    return chat


async def append_user_message(
    db: AsyncSession,
    chat_id: int,
    content: str,
    *,
    source_type: str | None = None,
    filters: list | dict | None = None,
    skill_ids: list | None = None,
    images: list | None = None,
) -> Message:
    """追加一条用户消息（不提交）。"""
    return await repo.add_message(
        db,
        chat_id=chat_id,
        role="user",
        content=content,
        source_type=source_type,
        filters=filters,
        skill_ids=skill_ids,
        images=images,
    )


async def append_assistant_message(
    db: AsyncSession,
    chat_id: int,
    content: str,
    *,
    source_type: str | None = None,
    kind: str = "normal",
    refs: list | dict | None = None,
    filters: list | dict | None = None,
    reasoning: str | None = None,
    thinking: dict | None = None,
    skill_ids: list | None = None,
    agent_trace: dict | None = None,
    answer_source: str | None = None,
) -> Message:
    """追加一条助手消息（不提交）。

    ``kind`` 为契约分路结果（normal/clarify/refuse/direct/stopped），
    ``answer_source`` 为答案级来源标记（kb/general/empty_scope/clarify）——
    两者由 1.3/1.4 的分路结果决定，本层只负责按 §3.8 的口径落库。
    """
    return await repo.add_message(
        db,
        chat_id=chat_id,
        role="assistant",
        content=content,
        source_type=source_type,
        kind=kind,
        refs=refs,
        filters=filters,
        reasoning=reasoning,
        thinking=thinking,
        skill_ids=skill_ids,
        agent_trace=agent_trace,
        answer_source=answer_source,
    )


async def load_history(db: AsyncSession, chat_id: int) -> list[Message]:
    """按 id 升序取会话内全部消息行（含元数据，供详情端点回读）。"""
    return await repo.list_messages(db, chat_id)


async def model_history_rows(db: AsyncSession, chat_id: int) -> list[Message]:
    """取**参与模型历史**的消息行（只要 user / assistant，按 id 升序）。

    与 :func:`build_model_history` **同一个筛选口径**（system 不进、reasoning 不回灌），
    但返回 ORM 行——压缩要用 ``Message.id`` 把 ``compressed_up_to`` 映射到 history 下标，
    并算出新的 ``compressed_up_to``。两个函数分开是为了保住 ``build_model_history``
    的**对外形状**（``[{"role", "content"}]``，且**只有这两个键**）：
    那是既有用例与 ``/api/chats/{id}`` 侧共用的口径，不该为了压缩多塞一个 ``id`` 进去。
    """
    rows = await repo.list_messages(db, chat_id)
    return [row for row in rows if row.role in ("user", "assistant")]


async def build_model_history(
    db: AsyncSession,
    chat_id: int,
    *,
    chat: Chat | None = None,
    model=None,
    turn: int | None = None,
    trace: str | None = None,
) -> list[dict[str, str]]:
    """组装发给模型的历史：只含 role / content 两键。

    对齐老基线与坑清单：**思考内容只展示不回灌**——历史里不带 ``reasoning`` /
    ``thinking``；``system`` 角色不进模型历史。

    ``chat``（阶段 4 · 条目 4.3.3）：给了它就走**长会话压缩**——
    ``app/session/compaction.py::compact_history`` 会按 ``CHAT_COMPRESS_THRESHOLD``
    决定是否把早期轮次压成摘要，并把摘要以「user + assistant 一对消息」放在历史开头。
    **返回值形状一个字不变**（仍是 ``[{"role", "content"}]``），所以调用方无感。

    不给 ``chat``（旧调用方 / 单测）⇒ 逐字走原路径：全量、不压缩。压缩是优化项，
    缺参数时**宁可什么都不做**也不该猜一个会话出来。
    """
    rows = await model_history_rows(db, chat_id)
    if chat is None:
        return [{"role": row.role, "content": row.content} for row in rows]

    from app.session import compaction

    result = await compaction.compact_history(
        db, chat, rows, model=model, turn=turn, trace=trace
    )
    for warning in result.warnings:
        logger.warning("会话历史压缩提示：%s", warning)
    return list(result.history)


async def save_round(
    db: AsyncSession,
    *,
    chat_id: int | None,
    question: str,
    answer: str,
    source_type: str | None = None,
    kind: str = "normal",
    answer_source: str | None = None,
    refs: list | dict | None = None,
    reasoning: str | None = None,
    thinking: dict | None = None,
    filters: list | dict | None = None,
    skill_ids: list | None = None,
) -> tuple[Chat, Message, Message]:
    """一问一答一次落库并提交（1.5 的 /chat 直接用这个）。

    返回 ``(chat, user_message, assistant_message)``。任一步失败整体回滚，
    不会留下半轮（半轮会让下一轮的历史带上无答案的提问）。
    """
    chat = await resolve_chat(db, chat_id=chat_id, message=question)
    user_message = await append_user_message(
        db,
        chat.id,
        question,
        source_type=source_type,
        filters=filters,
        skill_ids=skill_ids,
    )
    assistant_message = await append_assistant_message(
        db,
        chat.id,
        answer,
        source_type=source_type,
        kind=kind,
        answer_source=answer_source,
        refs=refs,
        reasoning=reasoning,
        thinking=thinking,
        filters=filters,
        skill_ids=skill_ids,
    )
    await db.commit()
    await db.refresh(chat)
    await db.refresh(user_message)
    await db.refresh(assistant_message)
    return chat, user_message, assistant_message


__all__ = [
    "TITLE_MAX_LEN",
    "append_assistant_message",
    "append_user_message",
    "build_model_history",
    "chat_title",
    "load_history",
    "model_history_rows",
    "resolve_chat",
    "save_round",
]
