"""审计 run 的**两个时间戳必须来自同一个时钟**（`回归修复` 的方言回归钉）。

## 为什么这条用例必须**自己起一个 SQLite 引擎**

``agent_runs.started_at`` 写的是 ``server_default=func.now()``（**库内时钟**），
而 ``finished_at`` 原先是 ``datetime.now()``（**Python 本机时钟**）——两个钟在 MySQL 开发态
**恰好同值**（`docker-compose.yml` 钉了 `TZ: Asia/Shanghai`，本机也是 +08:00），
⇒ 拿仓里默认的 MySQL ``db`` 夹具跑，「差几秒」在修前修后**都≈0**，**区分不出修没修**。

真差异只在 **SQLite**：``func.now()`` 编译成 ``CURRENT_TIMESTAMP`` = **UTC**，
而本机时钟是 +08:00 ⇒ 两个戳差整整 8 小时。而那正是**阶段 6 桌面交付档**（`DB_BACKEND=sqlite`）——
⇒ 这条断言必须在 SQLite 上钉，才不会「本机绿、交付档错」。

⚠️ 立此钉的代价只有一次 ``create_all``（临时文件库，用例结束即随 ``tmp_path`` 消失），
换来的是一条**跨方言**的真回归钉：把 ``_apply_run_close`` 改回 ``datetime.now()`` 即红。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agent import audit as agent_audit
from app.db.session import apply_sqlite_pragmas
from app.models import AgentRun, Base

pytestmark = pytest.mark.asyncio

#: 允许的偏差上限（秒）。同一时钟下「建行 → 收口」毫秒级完成 ⇒ 实测远小于 1s；
#: 两个时钟错位时偏差是**整时区**（+08:00 = 28800s）⇒ 这条阈值的区分度是 4 个数量级。
MAX_SKEW_SECONDS = 60


async def test_run_close_uses_the_same_clock_as_started_at(tmp_path):  # noqa: ANN001
    """收口写下的 ``finished_at`` 与插入时的 ``started_at`` **同一个时钟**（SQLite 档）。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'clock.db').as_posix()}")
    apply_sqlite_pragmas(engine.sync_engine)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as db:
            run = AgentRun(chat_id=None, mode="react", status="running")
            db.add(run)
            await db.flush()
            await db.refresh(run)  # ``started_at`` 是 server_default ⇒ 必须回读才拿得到
            started = run.started_at

            await agent_audit.finish_run(db, int(run.id), status="done")

            await db.refresh(run)
            finished = run.finished_at
            status = run.status
    finally:
        await engine.dispose()

    assert status == "done", "收口本身要照常生效（别为了钉时钟把功能改坏）"
    assert started is not None and finished is not None, "两列都该有值"
    skew = (finished - started).total_seconds()
    assert -MAX_SKEW_SECONDS <= skew <= MAX_SKEW_SECONDS, (
        "两个时间戳不是同一个时钟："
        f"started_at={started.isoformat()} finished_at={finished.isoformat()}"
        f"（差 {skew / 3600:.1f} 小时）——`started_at` 是库内时钟、`finished_at` 必须同源"
    )


async def test_db_clock_now_matches_the_column_default_on_sqlite(tmp_path):  # noqa: ANN001
    """``db_clock_now()`` 与列的 ``server_default=func.now()`` **同源**（SQLite 档差 < 5 秒）。

    这条钉的是**机制**（不是某一次调用的结果）：将来若有人把 ``db_clock_is_utc()`` 的判据改坏，
    它会先红——比「跑一轮再比两个戳」更直接、也更快。
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'clock2.db').as_posix()}")
    apply_sqlite_pragmas(engine.sync_engine)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as db:
            run = AgentRun(chat_id=None, mode="react", status="running")
            db.add(run)
            await db.flush()
            await db.refresh(run)
            column_clock = run.started_at
            # ⚠️ 按**这条连接的方言**判（不是进程级 ``DB_BACKEND``）——否则在 MySQL 进程里
            # 对 SQLite 连接做断言就只能靠 monkeypatch 全局配置，测的会变成「配置分支」。
            assert agent_audit.db_clock_is_utc(db) is True, "应按会话方言判（SQLite），而不是按进程配置"
            python_clock = agent_audit.db_clock_now(db)
    finally:
        await engine.dispose()

    skew = abs((column_clock - python_clock).total_seconds())
    assert skew <= 5, (
        f"db_clock_now() 与库内时钟不同源：库={column_clock.isoformat()} 本函数={python_clock.isoformat()}"
        f"（差 {skew / 3600:.1f} 小时）"
    )
