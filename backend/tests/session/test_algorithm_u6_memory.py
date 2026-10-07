"""U6 记忆修订、来源、范围与相关性；只使用 pytest 临时 SQLite。"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text

from app.core.config import settings
from app.models import Chat, Memory, Message
from app.session import memory
from tests.test_migration_chain import BACKEND_DIR


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    monkeypatch.setattr(settings, "MEMORY_TOP_K", 5)


async def add_fact(db, body="用户喜欢深色主题", **kwargs):
    row_id = await memory.remember(db, text=body, kind="interaction", **kwargs)
    await db.commit()
    return row_id


@pytest.mark.asyncio
async def test_revision_round_trip_preserves_every_version(db):
    first = await add_fact(db, "用户喜欢深色主题", fact_key="theme")
    second = await add_fact(db, "用户喜欢浅色主题", fact_key="theme", supersedes_id=first)
    third = await add_fact(db, "用户喜欢深色主题", fact_key="theme", supersedes_id=second)
    rows = list((await db.execute(select(Memory).order_by(Memory.id))).scalars())
    assert [row.version for row in rows] == [1, 2, 3]
    assert [row.status for row in rows] == ["superseded", "superseded", "active"]
    assert [row.supersedes_id for row in rows] == [None, first, second]
    assert [row.id for row in await memory.recall(db, question="聊聊主题")] == [third]


@pytest.mark.asyncio
async def test_conflict_withheld_until_explicit_latest_correction(db):
    first = await add_fact(db, "用户喜欢深色主题", fact_key="theme")
    second = await add_fact(db, "用户喜欢浅色主题", fact_key="theme")
    assert await memory.recall(db, question="聊聊主题") == []
    assert await add_fact(db, "用户喜欢红色主题", fact_key="theme", supersedes_id=first) is None
    third = await add_fact(db, "用户喜欢深色主题", fact_key="theme", supersedes_id=second)
    assert [row.id for row in await memory.recall(db, question="聊聊主题")] == [third]
    assert (await db.get(Memory, first)).status == "superseded"


@pytest.mark.parametrize("offsets,expected", [
    ((-2, 2), True), ((2, 4), False), ((-4, -2), False),
])
@pytest.mark.asyncio
async def test_validity_window(db, offsets, expected):
    now = datetime.utcnow()
    row_id = await add_fact(
        db, valid_from=now + timedelta(days=offsets[0]),
        valid_until=now + timedelta(days=offsets[1]),
    )
    assert bool(await memory.recall(db, question="深色主题")) == expected
    assert await db.get(Memory, row_id) is not None


@pytest.mark.asyncio
async def test_bad_validity_is_rejected(db):
    now = datetime.utcnow()
    assert await add_fact(db, valid_from=now, valid_until=now) is None


@pytest.mark.parametrize("mutation", ["edit", "delete", "restore", "missing"])
@pytest.mark.asyncio
async def test_user_source_lifecycle(db, mutation):
    chat = Chat(title="记忆来源")
    db.add(chat)
    await db.flush()
    message = Message(chat_id=chat.id, role="user", content="我喜欢深色主题")
    db.add(message)
    await db.commit()
    row_id = await add_fact(db, source_chat_id=chat.id, source_message_id=message.id)
    assert [row.id for row in await memory.recall(db, question="深色主题")] == [row_id]
    if mutation == "edit":
        message.content = "我喜欢浅色主题"
    elif mutation in ("delete", "restore"):
        chat.deleted_at = datetime.utcnow()
    else:
        await db.delete(message)
    await db.commit()
    assert await memory.recall(db, question="深色主题") == []
    if mutation == "restore":
        chat.deleted_at = None
        await db.commit()
        assert [row.id for row in await memory.recall(db, question="深色主题")] == [row_id]
    assert await db.get(Memory, row_id) is not None


@pytest.mark.parametrize("filters,chat_id,expected", [
    ({"kb_ids": [7]}, 9, True), ({"kb_ids": [8]}, 9, False), (None, 7, False),
])
@pytest.mark.asyncio
async def test_knowledge_scope_is_exact(db, filters, chat_id, expected):
    row_id = await memory.remember(db, text="预算上限是七万元", scope="kb:7")
    await db.commit()
    rows = await memory.recall(db, question="预算上限是多少", filters=filters, chat_id=chat_id)
    assert [row.id for row in rows] == ([row_id] if expected else [])


@pytest.mark.parametrize("mutation", ["missing", "assistant", "deleted", "wrong_chat", "edited"])
@pytest.mark.asyncio
async def test_invalid_extraction_source_cannot_supersede_old_fact(db, mutation):
    old_id = await add_fact(db, fact_key="theme")
    question = "纠正：我喜欢浅色主题"
    chat = Chat(title="source")
    db.add(chat)
    await db.flush()
    message = Message(chat_id=chat.id, role="user", content=question)
    db.add(message)
    await db.commit()
    source_id, chat_id = message.id, chat.id
    if mutation == "missing":
        await db.delete(message)
    elif mutation == "assistant":
        message.role = "assistant"
    elif mutation == "deleted":
        chat.deleted_at = datetime.utcnow()
    elif mutation == "wrong_chat":
        chat_id += 1000
    else:
        message.content = "我喜欢深色主题"
    await db.commit()
    assert await memory._store_facts(
        [{"text": "用户喜欢浅色主题", "kind": "interaction",
          "key": "theme", "supersedes_id": old_id}],
        chat_id=chat_id, message_id=source_id, bound_to=db, question=question,
    ) == 0
    await db.refresh(await db.get(Memory, old_id))
    rows = list((await db.execute(select(Memory))).scalars())
    assert [(row.id, row.status) for row in rows] == [(old_id, "active")]


@pytest.mark.asyncio
async def test_legacy_global_knowledge_cannot_leak_into_answers(db):
    await memory.remember(db, text="预算是七万元")
    await db.commit()
    assert await memory.recall(db, question="预算是多少", chat_id=3) == []


@pytest.mark.asyncio
async def test_relevance_precedes_recency_and_candidate_limit(db):
    target = await add_fact(db, "用户喜欢深色主题与低蓝光")
    for index in range(270):
        db.add(Memory(
            scope="global", kind="interaction", text=f"用户喜欢苹果{index}",
            text_hash=memory._text_hash(f"用户喜欢苹果{index}"),
        ))
    await db.commit()
    rows = await memory.recall(db, question="深色主题与蓝光", limit=1)
    assert [row.id for row in rows] == [target]
    assert await memory.recall(db, question="无关的足球赛") == []


@pytest.mark.parametrize("question", [
    "只能使用2026年资料介绍主题", "不要使用深色主题", "纠正，主题改成浅色",
])
@pytest.mark.asyncio
async def test_current_hard_constraint_or_correction_suppresses_old_memory(db, question):
    await add_fact(db)
    assert await memory.recall(db, question=question) == []


@pytest.mark.asyncio
async def test_background_revision_context_never_reuses_request_session(db, monkeypatch):
    await add_fact(db, fact_key="theme")
    async def denied(*args, **kwargs):
        raise AssertionError("request session was reused by background extraction")
    monkeypatch.setattr(db, "execute", denied)
    inputs = []
    async def fake_call(model, material):
        inputs.append(material)
        return '{"facts":[]}'
    monkeypatch.setattr(memory, "_call_model", fake_call)
    assert await memory.remember_turn("聊聊主题", "回答", db=db, model=object()) == 0
    assert len(inputs) == 1 and "theme" in inputs[0]


@pytest.mark.asyncio
async def test_off_switch_has_no_sql_or_model_calls(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_ENABLE", False)
    class Denied:
        async def execute(self, *args):
            raise AssertionError("off state queried SQL")
    db = Denied()
    assert await memory.remember(db, text="深色主题") is None
    assert await memory.recall(db, question="深色主题") == []
    assert await memory.remember_turn("问题", "答案", db=db, model=object()) == 0
    assert memory.render_block([SimpleNamespace(text="深色主题")]) == ""


def test_render_budget_includes_header_newline(monkeypatch):
    body = "深色主题"
    exact = len(memory.MEMORY_HEADER) + 1 + len(memory.MEMORY_BULLET + body)
    monkeypatch.setattr(settings, "MEMORY_INJECT_MAX_CHARS", exact)
    assert len(memory.render_block([SimpleNamespace(text=body)])) == exact
    monkeypatch.setattr(settings, "MEMORY_INJECT_MAX_CHARS", exact - 1)
    assert memory.render_block([SimpleNamespace(text=body)]) == ""


def test_0010_preserves_legacy_rows_and_refuses_lossy_downgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_BACKEND", "sqlite")
    db_path = tmp_path / "memory_revisions.db"
    monkeypatch.setattr(settings, "SQLITE_PATH", str(db_path))
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "0009")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO memories(id,scope,kind,text,text_hash) "
            "VALUES(17,'global','interaction','旧事实','same')"
        ))
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT id,text,version,status,source_hash FROM memories WHERE id=17"
        )).one()
        assert tuple(row) == (17, "旧事实", 1, "active", None)
        conn.execute(text(
            "INSERT INTO memories(scope,kind,text,text_hash,version) "
            "VALUES('global','interaction','旧事实','same',2)"
        ))
    with pytest.raises(RuntimeError, match="export history"):
        command.downgrade(cfg, "0009")
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM memories")).scalar_one() == 2
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0010"
        assert "source_hash" in {col["name"] for col in inspect(conn).get_columns("memories")}
        conn.execute(text("DELETE FROM memories WHERE id != 17"))
    command.downgrade(cfg, "0009")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id,text FROM memories")).one() == (17, "旧事实")
    engine.dispose()
