"""SQLite 方言回归（阶段 3 · 条目 3.0 / 待修正事项 B5；对应验收 ①⑥⑦）。

改造前 `tests/conftest.py` 开篇明写「本仓测试连真 MySQL：`app/models` 用了 MySQL 方言的
``LONGTEXT`` / ``JSON``，SQLite 建表都过不去」——条目 3.0 把这条前提改掉了，所以这里
**用 SQLite 自己建库自己验**：不依赖容器、不占端口、秒级完成。

覆盖：

1. 两方言都能编译出 DDL，且 **MySQL 侧类型未被 variant 污染**（仍是 LONGTEXT / JSON / BIGINT）；
2. SQLite 侧**外键与级联真生效**（``PRAGMA foreign_keys=ON`` + 级联删净 + 孤儿插入被拒）；
3. ``session_events.id`` 在 SQLite 上**能自增**——``BIGINT PRIMARY KEY`` 不是 rowid 别名，
   改造前这是「每轮问答写事件流直接失败」的隐藏坑（NOT NULL 违约）；
4. 并发写：规定负载（8 写者 × 5 行）全部成功，无 ``database is locked`` 逃逸；
5. ``app/db/verify.py::_check_sqlite`` 真能跑通（SQLite 分支不查 ``information_schema``）。
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from app.core.config import settings
from app.db.session import apply_sqlite_pragmas
from app.models import Base, Chat, Chunk, Document, Note, SessionEvent


def _render(dialect, table_name: str) -> str:
    return str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=dialect()))


@pytest_asyncio.fixture
async def sqlite_engine(tmp_path):
    """临时 SQLite 库：同步建表 + 异步引擎（PRAGMA 复用产品同一份 ``apply_sqlite_pragmas``）。"""
    db_file = tmp_path / "zhilan_dialect.db"
    sync_engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    # ⚠️ ``sqlalchemy.event`` 的监听器**按监听顺序调用**，而 ``connect`` 事件的监听器
    # 在**创建它的那次连接**之前就已注册；因此**必须先挂 PRAGMA 监听器、再 create_all**
    # —— 否则 create_all 那条连接拿不到 PRAGMA 设置。反过来，**建表之后**挂的监听器
    # 只对**之后新建**的连接生效，而 ``aiosqlite`` 的连接池一旦把连接建出来（且
    # ``busy_timeout`` 从未设置），后续 8 个并发写者共用的那条连接就**永远**是
    # 默认 0ms 超时 ⇒ 争用立刻报 ``attempt to write a readonly database``
    # （回归检查 基线复核实测：13F 里那条极不稳定的 `test_sqlite_concurrent_writes_all_succeed`
    # 就是本问题，而不是「产品有缺陷」）。
    apply_sqlite_pragmas(sync_engine)
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    eng = create_async_engine(
        f"sqlite+aiosqlite:///{db_file.as_posix()}",
        connect_args={"timeout": settings.SQLITE_BUSY_TIMEOUT_MS / 1000},
    )
    apply_sqlite_pragmas(eng.sync_engine)
    try:
        yield eng
    finally:
        await eng.dispose()


# --------------------------------------------------------------------------
# ① 两方言 DDL
# --------------------------------------------------------------------------


def test_metadata_compiles_on_both_dialects():
    """所有表在 sqlite / mysql 两方言下都能编译出 DDL（改造前 SQLite 侧直接抛错）。"""
    for table in Base.metadata.sorted_tables:
        sqlite_sql = _render(sqlite_dialect.dialect, table.name).upper()
        mysql_sql = _render(mysql_dialect.dialect, table.name)
        assert sqlite_sql.strip(), f"{table.name} 在 SQLite 下编译为空"
        assert mysql_sql.strip(), f"{table.name} 在 MySQL 下编译为空"
        # SQLite 侧不得残留 MySQL 专有类型
        assert "LONGTEXT" not in sqlite_sql, f"{table.name} 的 SQLite DDL 残留 LONGTEXT"
        assert "BIGINT" not in sqlite_sql, f"{table.name} 的 SQLite DDL 残留 BIGINT"


def test_mysql_side_types_are_unchanged():
    """MySQL 侧最终类型逐字保留：长文本仍是 LONGTEXT、JSON 仍是 JSON、主键仍是 BIGINT。"""
    chunks = _render(mysql_dialect.dialect, "chunks")
    assert "LONGTEXT" in chunks.upper()
    notes = _render(mysql_dialect.dialect, "notes")
    assert "LONGTEXT" in notes.upper()
    assert "JSON" in notes.upper()
    session_events = _render(mysql_dialect.dialect, "session_events").upper()
    assert "BIGINT" in session_events
    assert "JSON" in session_events


def test_sqlite_session_events_id_is_rowid_alias():
    """SQLite 侧 ``session_events.id`` 必须是 ``INTEGER``（rowid 别名才会自动赋值）。"""
    sql = _render(sqlite_dialect.dialect, "session_events")
    assert "id INTEGER NOT NULL" in sql


# --------------------------------------------------------------------------
# ⑦ 外键与级联（与 MySQL 基线同断言）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_fk_pragma_is_on(sqlite_engine):
    """SQLite 默认 ``foreign_keys=0``；产品路径必须把它打开（否则 8 处级联静默失效）。"""
    async with sqlite_engine.connect() as conn:
        value = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
    assert int(value) == 1


@pytest.mark.asyncio
async def test_sqlite_cascade_delete_matches_mysql(sqlite_engine):
    """documents → chunks 的 ``ON DELETE CASCADE`` 真生效（删父行即清子行）。"""
    factory = async_sessionmaker(sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        doc = Document(filename="a.md", file_type="md", status="ready")
        session.add(doc)
        await session.commit()
        session.add(Chunk(document_id=doc.id, content="正文", ordinal=0))
        await session.commit()

        await session.execute(delete(Document).where(Document.id == doc.id))
        await session.commit()

        left = await session.scalar(select(func.count()).select_from(Chunk))
    assert left == 0, "级联未生效：chunks 仍残留子行"


@pytest.mark.asyncio
async def test_sqlite_rejects_orphan_child(sqlite_engine):
    """指向不存在父行的子行必须被拒（外键约束真生效，而不是只写在 DDL 里）。"""
    factory = async_sessionmaker(sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(Chunk(document_id=99999, content="孤儿块", ordinal=0))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_sqlite_session_events_id_autoincrements(sqlite_engine):
    """``session_events.id`` 不给值也能落库（BIGINT PRIMARY KEY 在 SQLite 上不会自增）。"""
    factory = async_sessionmaker(sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        chat = Chat(title="会话")
        session.add(chat)
        await session.commit()

        event = SessionEvent(
            chat_id=chat.id, seq=0, type="route/decision", data={"route": "regex"}
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)

    assert isinstance(event.id, int)
    assert event.id >= 1


# --------------------------------------------------------------------------
# ⑥ 并发写
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_concurrent_writes_all_succeed(sqlite_engine):
    """规定负载：8 个并发写者各写 5 行 —— 全部成功，无 ``database is locked`` 逃逸。

    口径（条目 3.0 交付物 ⑤②）：**不承诺**「任何并发下都无锁等待」——SQLite 是单写者，
    这里承诺的是「等待有界（busy_timeout）、超时才是错误、数据不损坏」。
    """
    factory = async_sessionmaker(sqlite_engine, class_=AsyncSession, expire_on_commit=False)

    async def writer(n: int) -> None:
        async with factory() as session:
            for i in range(5):
                session.add(Note(title=f"并写-{n}-{i}", content="内容", category="并发"))
            await session.commit()

    # ⚠️ 必须传 return_exceptions 并把异常**取出来看**：
    # 本用例只断言 ``total == 40``，而 SQLAlchemy 的 ``AsyncSession`` 在 commit 失败时会
    # **回滚并让任务静默结束**（异常被 gather 吞掉或根本不到断言）⇒ 实测出现
    # 「4 行落库、零异常、断言假绿」的形态。回归检查 基线复核时按「异常真实暴露」修正。
    results = await asyncio.gather(*(writer(n) for n in range(8)), return_exceptions=True)
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"并发写出现 {len(failures)} 个异常：{[repr(f) for f in failures]}"

    async with factory() as session:
        total = await session.scalar(select(func.count()).select_from(Note))
    assert total == 40


@pytest.mark.asyncio
async def test_sqlite_concurrent_read_then_write_is_bounded(sqlite_engine):
    """「先读后写」并发：允许**有界失败**，但不允许静默丢写或数据损坏。

    口径（条目 3.0 交付物 ⑤②，实测于 2026-09-18）：SQLite 的锁升级（读事务中遇到并发写）
    在上游**立即**返回 ``SQLITE_BUSY`` 且**不等待** ``busy_timeout``（避免死锁的既有行为）。
    故这里承诺的是「要么成功、要么抛**可识别**错误，且落库行数与成功任务数一致」——
    **不承诺**任何并发下都不出现锁等待/失败。
    """
    factory = async_sessionmaker(sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(Note(title="基线", content="x", category="并发"))
        await session.commit()

    async def read_then_write(n: int) -> str:
        async with factory() as session:
            await session.execute(select(Note).limit(1))  # 先读 → 已持有读事务
            await asyncio.sleep(0.05)  # 让并发写者先拿到写锁
            session.add(Note(title=f"读写-{n}", content="x", category="并发"))
            try:
                await session.commit()
            except OperationalError as exc:
                await session.rollback()
                assert "locked" in str(exc).lower(), f"锁失败必须是可识别的 locked 错误：{exc}"
                return "locked"
            return "ok"

    results = await asyncio.gather(*(read_then_write(n) for n in range(4)))

    async with factory() as session:
        total = await session.scalar(select(func.count()).select_from(Note))
    assert total == 1 + results.count("ok"), "落库行数必须与成功任务数一致（不许静默丢写）"


# --------------------------------------------------------------------------
# ④ 自检的 SQLite 分支
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_selfcheck_passes(sqlite_engine, monkeypatch):
    """``_check_sqlite()`` 真能跑通（连的是 SQLite 会话工厂，不查 ``information_schema``）。"""
    from app.db import verify as verify_mod

    factory = async_sessionmaker(sqlite_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(verify_mod, "SessionLocal", factory)

    await verify_mod._check_sqlite()
