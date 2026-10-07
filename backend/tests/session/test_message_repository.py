"""条目 1.1 消息持久化单测：真连 MySQL 独立测试库（``zhilan_v2_test``）。

验收对照（阶段 1 设计说明 条目 1.1）：
- ``messages`` 写入含 chat_id / role / content / source_type / created_at；
- 为 ``GET /chats*`` 预留字段（kind / refs / reasoning / thinking / skill_ids /
  agent_trace / images / answer_source / filters）都能存能读；
- 不变量：消息真相源在 MySQL —— 用一个**新会话**重读验证，且代码里不碰 Redis。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Message
from app.session import repository as repo
from app.session import service


@pytest.mark.asyncio
async def test_save_round_persists_user_and_assistant(db):
    """一问一答落库：字段齐、顺序对、新连接仍读得到（= 真落库）。"""
    chat, user_msg, ai_msg = await service.save_round(
        db,
        chat_id=None,
        question="今天天气怎么样",
        answer="今天晴，气温 20 度。",
        source_type="external",
        kind="direct",
        answer_source="general",
    )

    assert chat.id is not None
    assert chat.title == "今天天气怎么样"
    assert user_msg.role == "user"
    assert user_msg.source_type == "external"
    assert ai_msg.role == "assistant"
    assert ai_msg.source_type == "external"
    assert ai_msg.kind == "direct"
    assert ai_msg.answer_source == "general"
    # created_at 由 server_default 填，refresh 后必须可读（async 下惰性加载会抛）
    assert user_msg.created_at is not None
    assert ai_msg.created_at is not None

    rows = await repo.list_messages(db, chat.id)
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[0].content == "今天天气怎么样"
    assert rows[1].content == "今天晴，气温 20 度。"


@pytest.mark.asyncio
async def test_source_type_values_roundtrip(db, fresh_db):
    """§3.8 两个内部 source_type 都能落库并原样读回（换连接读，证明进的是库）。"""
    chat = await repo.create_chat(db, title="来源类型")
    await repo.add_message(
        db, chat_id=chat.id, role="user", content="知识库里有什么", source_type="internal_db"
    )
    await repo.add_message(
        db,
        chat_id=chat.id,
        role="assistant",
        content="库内回答",
        source_type="internal_db",
        kind="normal",
        answer_source="kb",
    )
    await db.commit()

    rows = await repo.list_messages(fresh_db, chat.id)
    assert [row.source_type for row in rows] == ["internal_db", "internal_db"]
    assert rows[1].answer_source == "kb"


@pytest.mark.asyncio
async def test_message_reserved_fields_for_chats_endpoints(db):
    """为 GET /chats* 预留的字段全部可存取（契约 MessageOut 口径）。"""
    chat = await repo.create_chat(db, title="预留字段")
    msg = await repo.add_message(
        db,
        chat_id=chat.id,
        role="assistant",
        content="答案",
        kind="normal",
        refs=[{"type": "document", "title": "周报", "ordinal": 1}],
        filters={"kb_ids": [1], "categories": ["项目"]},
        reasoning="先看资料",
        thinking={"mode": "auto", "reason": "复杂判定"},
        skill_ids=["builtin:knowledge"],
        agent_trace={"mode": "off", "steps": []},
        images=["data:image/png;base64,AAA"],
        answer_source="kb",
    )
    await db.commit()
    await db.refresh(msg)

    assert msg.refs and msg.refs[0]["title"] == "周报"
    assert msg.filters == {"kb_ids": [1], "categories": ["项目"]}
    assert msg.reasoning == "先看资料"
    assert msg.thinking["mode"] == "auto"
    assert msg.skill_ids == ["builtin:knowledge"]
    assert msg.agent_trace == {"mode": "off", "steps": []}
    assert msg.images == ["data:image/png;base64,AAA"]
    assert msg.answer_source == "kb"


@pytest.mark.asyncio
async def test_rejects_invalid_enum_values(db):
    """词汇表外的取值直接拒绝，脏值不进库。"""
    chat = await repo.create_chat(db, title="校验")

    with pytest.raises(ValueError, match="source_type"):
        await repo.add_message(
            db, chat_id=chat.id, role="user", content="x", source_type="web"
        )
    with pytest.raises(ValueError, match="role"):
        await repo.add_message(db, chat_id=chat.id, role="tool", content="x")
    with pytest.raises(ValueError, match="kind"):
        await repo.add_message(db, chat_id=chat.id, role="user", content="x", kind="bogus")
    with pytest.raises(ValueError, match="answer_source"):
        await repo.add_message(
            db, chat_id=chat.id, role="assistant", content="x", answer_source="cited"
        )


@pytest.mark.asyncio
async def test_continued_round_keeps_single_chat(db):
    """续问复用同一会话：标题不变、消息按序累加。"""
    chat, _, _ = await service.save_round(
        db, chat_id=None, question="第一个问题", answer="第一答"
    )
    chat2, _, _ = await service.save_round(
        db, chat_id=chat.id, question="第二个问题", answer="第二答"
    )

    assert chat2.id == chat.id
    assert chat2.title == "第一个问题"
    rows = await service.load_history(db, chat.id)
    assert [(r.role, r.content) for r in rows] == [
        ("user", "第一个问题"),
        ("assistant", "第一答"),
        ("user", "第二个问题"),
        ("assistant", "第二答"),
    ]


@pytest.mark.asyncio
async def test_missing_or_trashed_chat_raises(db):
    """不存在的会话 → ChatNotFound；进回收站的会话按不存在处理。"""
    with pytest.raises(repo.ChatNotFound):
        await service.resolve_chat(db, chat_id=999999, message="在吗")

    chat = await repo.create_chat(db, title="待删")
    await db.commit()
    chat.deleted_at = chat.created_at  # 软删（复用非空时间戳，避免额外导入）
    await db.commit()

    assert await repo.get_chat(db, chat.id) is None
    assert await repo.get_chat(db, chat.id, include_deleted=True) is not None
    with pytest.raises(repo.ChatNotFound):
        await service.resolve_chat(db, chat_id=chat.id, message="还在吗")


@pytest.mark.asyncio
async def test_chat_listing_excludes_trash_and_archived(db):
    """列表口径：排除回收站；默认排除已归档，archived=True 只回已归档。"""
    active = await repo.create_chat(db, title="活跃")
    archived = await repo.create_chat(db, title="归档")
    trashed = await repo.create_chat(db, title="回收站")
    await db.flush()
    archived.archived_at = archived.created_at
    trashed.deleted_at = trashed.created_at
    await db.commit()

    default_ids = [c.id for c in await repo.list_chats(db)]
    archived_ids = [c.id for c in await repo.list_chats(db, archived=True)]

    assert active.id in default_ids
    assert archived.id not in default_ids
    assert trashed.id not in default_ids
    assert archived_ids == [archived.id]


@pytest.mark.asyncio
async def test_build_model_history_excludes_reasoning_and_system(db):
    """模型历史只含 role/content；思考内容只展示不回灌（坑清单）。"""
    chat = await repo.create_chat(db, title="历史")
    await repo.add_message(db, chat_id=chat.id, role="system", content="系统提示")
    await repo.add_message(db, chat_id=chat.id, role="user", content="问题")
    await repo.add_message(
        db, chat_id=chat.id, role="assistant", content="答案", reasoning="思考全文"
    )
    await db.commit()

    history = await service.build_model_history(db, chat.id)
    assert history == [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "答案"},
    ]
    assert all(set(item) == {"role", "content"} for item in history)


def test_chat_title_follows_old_baseline():
    """标题口径对齐老基线：前 40 字、换行折空格、空正文「新会话」。"""
    assert service.chat_title("今天天气怎么样") == "今天天气怎么样"
    assert service.chat_title("  多行\n问题  ") == "多行 问题"
    assert service.chat_title("") == "新会话"
    assert service.chat_title("   ") == "新会话"
    assert len(service.chat_title("字" * 100)) == 40


@pytest.mark.asyncio
async def test_messages_never_touch_redis(db, monkeypatch):
    """不变量：消息真相源在 MySQL，不进 Redis。

    做法：把 ``app.db.redis.get_redis_client`` 换成「一旦被调用就炸」的桩，
    再走完整落库+回读路径——只要 session 层碰了 Redis，本用例立刻失败。
    """

    def _boom():  # pragma: no cover - 正常路径不会执行
        raise AssertionError("session 层不得访问 Redis")

    import app.db.redis as redis_mod

    monkeypatch.setattr(redis_mod, "get_redis_client", _boom)

    chat, _, _ = await service.save_round(
        db, chat_id=None, question="只进 MySQL", answer="好的"
    )
    rows = await repo.list_messages(db, chat.id)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_delete_messages_clears_chat_rows(db):
    """彻底删除：清空会话内消息并返回行数。"""
    chat, _, _ = await service.save_round(
        db, chat_id=None, question="待清空", answer="答"
    )
    removed = await repo.delete_messages(db, chat.id)
    await db.commit()

    assert removed == 2
    remaining = (await db.execute(select(Message).where(Message.chat_id == chat.id))).scalars().all()
    assert list(remaining) == []
