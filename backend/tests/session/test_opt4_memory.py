"""OPT4.6：来源失效仅禁止召回；当前明确事实不能被相反旧记忆覆盖。"""

from datetime import datetime

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import Chat, Memory, Message
from app.session import memory
from tests.session.test_algorithm_u6_memory import add_fact


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    monkeypatch.setattr(settings, "MEMORY_TOP_K", 5)


@pytest.mark.parametrize("change", ["message_edited", "message_removed", "chat_removed", "restored"])
@pytest.mark.asyncio
async def test_invalid_source_is_not_recalled_and_manual_global_memory_is_retained(db, change):
    chat = Chat(title="有来源的主题记忆")
    db.add(chat)
    await db.flush()
    message = Message(chat_id=chat.id, role="user", content="我喜欢深色主题")
    db.add(message)
    await db.commit()
    sourced = await add_fact(db, source_chat_id=chat.id, source_message_id=message.id)
    manual = await add_fact(db, "用户希望主题字迹清晰", fact_key="manual-theme")
    assert sourced in [row.id for row in await memory.recall(db, question="主题方案")]
    if change == "message_edited":
        message.content = "我喜欢浅色主题"
    elif change == "message_removed":
        await db.delete(message)
    else:
        chat.deleted_at = datetime.utcnow()
    await db.commit()
    assert [row.id for row in await memory.recall(db, question="主题方案")] == [manual]
    assert set((await db.execute(select(Memory.id))).scalars()) == {sourced, manual}
    if change == "restored":
        chat.deleted_at = None
        await db.commit()
        assert set(row.id for row in await memory.recall(db, question="主题方案")) == {sourced, manual}


@pytest.mark.parametrize("fact,matching,contradicting", [
    ("用户项目预算为70000元", "预算为70000元，请安排方案", "本轮预算为30000元，请安排方案"),
    ("用户项目采用2025年配置", "按2025年配置安排项目", "本轮按2026年配置安排项目"),
    ("用户喜欢深色主题", "本轮使用深色主题", "本轮不使用深色主题"),
])
@pytest.mark.asyncio
async def test_current_explicit_numeric_date_or_negation_overrides_conflicting_memory(db, fact, matching, contradicting):
    row_id = await add_fact(db, fact)
    assert [row.id for row in await memory.recall(db, question=matching)] == [row_id], "同值正常问题不能被全面抑制"
    assert await memory.recall(db, question=contradicting) == [], "当前明确取值或否定不能被旧记忆覆盖"
    assert await db.get(Memory, row_id) is not None, "拒绝注入不能擅自物理删除记忆"


@pytest.mark.asyncio
async def test_number_and_negative_fact_are_preserved_without_blanket_suppression(db):
    row_id = await add_fact(db, "用户不喜欢深色主题，显示预算为70000元")
    rows = await memory.recall(db, question="介绍主题与70000元预算")
    assert [row.id for row in rows] == [row_id]
    rendered = memory.render_block(rows)
    assert "不喜欢深色主题" in rendered and "70000元" in rendered


@pytest.mark.asyncio
async def test_knowledge_scope_positive_negative_pair_keeps_global_manual_fact(db):
    scoped = await memory.remember(db, text="项目预算为70000元", scope="kb:7", kind="knowledge")
    manual = await add_fact(db, "用户关注项目预算", fact_key="manual-budget")
    await db.commit()
    inside = await memory.recall(db, question="项目预算方案", filters={"kb_ids": [7]})
    outside = await memory.recall(db, question="项目预算方案", filters={"kb_ids": [8]})
    assert set(row.id for row in inside) == {scoped, manual}
    assert [row.id for row in outside] == [manual]
    assert set((await db.execute(select(Memory.id))).scalars()) == {scoped, manual}
