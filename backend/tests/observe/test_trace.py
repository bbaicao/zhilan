"""trace 器官用例（阶段 3 · 条目 3.3）：``retrieval/start`` / ``retrieval/result`` 两条事件。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import SessionEvent
from app.observe import trace as organ
from app.retrieval.types import Material, number
from app.session import events as event_log
from app.session import repository as repo


async def _chat(db) -> int:
    """``session_events.chat_id`` 有外键指向 ``chats.id`` ⇒ 先建一行真会话。"""
    chat = await repo.create_chat(db, title="器官用例")
    await db.commit()
    return chat.id


def _materials() -> list[Material]:
    return number(
        [
            Material(
                chunk_id=11,
                document_id=7,
                content="向量库使用 Qdrant。",
                filename="a.md",
                vector_score=0.83,
                keyword_score=1.2,
            ),
            Material(
                chunk_id=12,
                document_id=8,
                content="另一段。",
                filename="b.md",
                keyword_score=0.9,
            ),
        ]
    )


@pytest.mark.asyncio
async def test_retrieval_events_are_paired_by_retrieval_id(db):
    chat_id = await _chat(db)
    await organ.log_retrieval_start(
        db,
        chat_id,
        question="向量库用什么存储？",
        filters={"kb_ids": [2]},
        retrieval_id="rl-aaaaaaaabbbb",
        turn=1,
    )
    await organ.log_retrieval_result(
        db, chat_id, materials=_materials(), retrieval_id="rl-aaaaaaaabbbb", turn=1
    )
    await db.commit()

    rows = await event_log.list_events(db, chat_id)
    assert [row.type for row in rows] == [organ.START, organ.RESULT]
    assert rows[0].data["question"] == "向量库用什么存储？"
    assert rows[0].data["filters"] == {"kb_ids": [2]}
    assert rows[1].data["retrieval_id"] == "rl-aaaaaaaabbbb"
    assert rows[1].data["hits"] == 2
    assert rows[1].data["from_vector"] == 1  # 只有第一条带 vector_score
    assert rows[1].data["from_keyword"] == 2
    assert rows[1].data["degraded"] is False
    assert rows[1].data["top_chunk_id"] == 11
    assert rows[1].data["top_ordinal"] == 1
    # 配对走 trace 列；request_id 保持 NULL（唯一约束 (chat_id, request_id) 一轮只容一条事件）
    assert {row.trace for row in rows} == {"rl-aaaaaaaabbbb"}
    assert {row.request_id for row in rows} == {None}


@pytest.mark.asyncio
async def test_degraded_flag_and_empty_result(db):
    chat_id = await _chat(db)
    await organ.log_retrieval_result(
        db,
        chat_id,
        materials=[],
        retrieval_id="rl-ccccccccdddd",
        warning="向量检索不可用（RuntimeError），本次仅使用关键词检索。",
    )
    await db.commit()

    rows = await event_log.list_events(db, chat_id)
    assert len(rows) == 1
    payload = rows[0].data
    assert payload["degraded"] is True
    assert payload["hits"] == 0
    assert payload["top_chunk_id"] is None
    assert "仅使用关键词检索" in payload["warning"]


@pytest.mark.asyncio
async def test_no_chat_id_writes_nothing(db):
    """拿不到会话（chat_id 为 None）时**零 DB 触碰**。"""
    await organ.log_retrieval_start(
        db, None, question="q", filters=None, retrieval_id="rl-x"
    )
    await organ.log_retrieval_result(db, None, materials=_materials(), retrieval_id="rl-x")
    await db.commit()
    rows = (await db.execute(select(SessionEvent))).scalars().all()
    assert list(rows) == []
