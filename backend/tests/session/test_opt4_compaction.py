"""OPT4.2：真实事务故障不能回滚摘要之前的业务数据；零模型出网。"""

import pytest
from sqlalchemy import event, select

from app.core.config import settings
from app.models import Chat, Message, SessionEvent
from app.session import compaction, events
from tests.session.test_compaction import FakeCompactionModel, _fail_summary_update, _seed, _tighten


async def _pending_turn(db, monkeypatch):
    compaction.reset_breakers()
    _tighten(monkeypatch, threshold=80, keep=1)
    monkeypatch.setattr(settings, "CHAT_EVENT_LOG_ENABLE", True)
    chat, rows, ids, _ = await _seed(db, rounds=4)
    chat.context_summary = "旧摘要：预算70000元，2025年截止。"
    chat.compressed_up_to = ids[1]
    await db.commit()
    chat_id = int(chat.id)
    old = (chat.context_summary, chat.compressed_up_to)
    pending = Message(chat_id=chat_id, role="user", content="这条业务提问必须保留")
    db.add(pending)
    await db.flush()  # 真正开启外层 SQLite 写事务，避免 SAVEPOINT 自动提交假象。
    pending_id = int(pending.id)
    await events.append_event(
        db, chat_id, "recovery/synthesized", {"fixture": "opt4-before-compaction"},
        turn=5, trace="opt4-compaction",
    )
    return chat, rows, chat_id, pending_id, old


@pytest.mark.asyncio
async def test_store_failure_preserves_pending_message_recovery_event_and_old_summary(db, engine, monkeypatch):
    chat, rows, chat_id, pending_id, old = await _pending_turn(db, monkeypatch)
    fired, listener = _fail_summary_update(engine)
    try:
        result = await compaction.compact_history(db, chat, rows, model=FakeCompactionModel(), turn=5)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", listener)
    assert len(fired) == 1, "故障必须击中真实摘要 UPDATE，不能在别处失败而假绿"
    assert result.attempted and not result.applied and result.reason == "store_failed"
    assert result.warnings and "自动压缩失败" in result.warnings[0]
    await db.commit()
    assert await db.get(Message, pending_id) is not None, "摘要失败丢失了之前的业务消息"
    saved = (await db.execute(select(Chat).where(Chat.id == chat_id))).scalar_one()
    assert (saved.context_summary, saved.compressed_up_to) == old
    recovered = list((await db.execute(
        select(SessionEvent).where(SessionEvent.chat_id == chat_id, SessionEvent.type == "recovery/synthesized")
    )).scalars())
    assert len(recovered) == 1, "摘要失败丢失了之前的恢复事件"
    assert old[0] in result.history[0]["content"], "退回历史必须继续使用已有摘要"


@pytest.mark.asyncio
async def test_store_failure_cannot_leave_successful_compaction_end(db, engine, monkeypatch):
    chat, rows, chat_id, _, _ = await _pending_turn(db, monkeypatch)
    original_title = chat.title
    fired, listener = _fail_summary_update(engine)
    try:
        result = await compaction.compact_history(db, chat, rows, model=FakeCompactionModel(), turn=5)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", listener)
    assert fired and result.reason == "store_failed"
    assert chat.id == chat_id and chat.title == original_title, "回滚后请求仍须直接读取 Chat 身份"
    await db.commit()
    ends = list((await db.execute(
        select(SessionEvent).where(SessionEvent.chat_id == chat_id, SessionEvent.type == "compaction/end")
    )).scalars())
    assert len(ends) == 1, "恢复读方需要恰好一个失败闭合事件"
    assert ends[0].data["ok"] is False
    assert ends[0].data.get("detail") == "store_failed"


@pytest.mark.asyncio
async def test_success_preserves_pending_business_data_and_source_rows(db, fresh_db, monkeypatch):
    chat, rows, chat_id, pending_id, old = await _pending_turn(db, monkeypatch)
    result = await compaction.compact_history(db, chat, rows, model=FakeCompactionModel(), turn=5)
    assert result.applied and not result.reason
    assert await fresh_db.get(Message, pending_id) is None, "摘要不得提前提交整个请求中的业务消息"
    other_chat = (await fresh_db.execute(select(Chat).where(Chat.id == chat_id))).scalar_one()
    assert (other_chat.context_summary, other_chat.compressed_up_to) == old, "摘要由调用方统一提交"
    await fresh_db.rollback()
    await db.commit()
    assert await fresh_db.get(Message, pending_id) is not None, "调用方提交后业务消息必须可见"
    assert await db.get(Message, pending_id) is not None
    kept = list((await db.execute(select(Message).where(Message.chat_id == chat_id))).scalars())
    assert len(kept) == len(rows) + 1
    assert chat.compressed_up_to > old[1]
    assert len(list((await db.execute(
        select(SessionEvent).where(SessionEvent.chat_id == chat_id, SessionEvent.type == "recovery/synthesized")
    )).scalars())) == 1
