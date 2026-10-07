"""SQLAlchemy async engine / session 工厂 + FastAPI 依赖注入（阶段 2 · 2.1.2）。

行为照老仓库 `app/db/session.py` + `app/core/deps.py` 的 get_db；
import 面按 v2 调整：依赖注入入口收在本模块（分包边界：db 不 import api）。

**阶段 3 · 条目 3.0（方言可切换）**：engine 的 DSN 由 `settings.database_dsn` 决定——
开发态 `mysql+aiomysql://`（逐字不变），桌面态 `sqlite+aiosqlite://`。
SQLite 分支额外做四件事，为的是「外键与级联语义和 MySQL 基线一致」＋「并发写有界」
（对应条目验收 ⑥⑦，口径见 `证据/S3-方言切换.log`）：

1. ``PRAGMA foreign_keys=ON``——**必须显式开**：SQLite 默认 ``foreign_keys=0``，
   `app/models` 里 8 处 ``ForeignKey(ondelete=…)`` 的级联会**静默失效**；
2. ``PRAGMA journal_mode=WAL``——读写并发（写不阻塞读），桌面单机够用；
3. ``PRAGMA busy_timeout=<SQLITE_BUSY_TIMEOUT_MS>``——写锁等待**有界**：等而不是
   立刻 ``database is locked``；超时仍抛错（不静默丢写）；
4. ``PRAGMA synchronous=NORMAL``——WAL 下的常规折中（断电最多丢最后一个事务，库不损坏）。

PRAGMA 是**连接级**状态，所以挂在 ``connect`` 事件上、每条新连接都设一遍。

**并发写口径（条目 3.0 交付物 ⑤②，2026-09-18 实测）**：

- SQLite 是**单写者**：写与写互斥、写不阻塞读（WAL）。``busy_timeout`` 让**纯写**事务
  在争用时**等待**而不是立刻失败——规定负载（8 写者 × 5 行短事务）实测全部成功；
- **边界（如实登记，不掩盖）**：事务「先读后写」（deferred 事务的锁升级）在上游会
  **立即**返回 ``database is locked``，``busy_timeout`` 对它**不生效**
  （SQLite 为避免死锁的既有行为，WAL 下同样如此）。这类写入若并发，需调用点
  做**有界重试**或改用 ``BEGIN IMMEDIATE``；
- ⇒ 本条承诺的是「等待有界、失败可识别、无数据损坏」，**不承诺**任何并发下都不出现锁等待。
"""

from collections.abc import AsyncGenerator, AsyncIterator
import contextlib
import logging
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

_engine_kwargs: dict[str, Any] = {"pool_pre_ping": True}

if settings.is_sqlite:
    # 数据目录不存在时 SQLite 打不开库（静默创建父目录，桌面首次启动的常见路径）
    settings.sqlite_file.parent.mkdir(parents=True, exist_ok=True)
    # 驱动层也收一个同口径的等待上限（PRAGMA busy_timeout 生效前的兜底）
    _engine_kwargs["connect_args"] = {"timeout": settings.SQLITE_BUSY_TIMEOUT_MS / 1000}

engine = create_async_engine(settings.database_dsn, **_engine_kwargs)


def apply_sqlite_pragmas(sync_engine) -> None:
    """把 SQLite 的四条**连接级** PRAGMA 挂到引擎的 ``connect`` 事件上。

    抽成函数是为了让测试能在自己的临时库上复用**同一份**逻辑（不复制粘贴）。
    对非 SQLite 引擎调用无意义（PRAGMA 会被拒），调用方自行判定方言。
    """

    @event.listens_for(sync_engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={int(settings.SQLITE_BUSY_TIMEOUT_MS)}")
        finally:
            cursor.close()


if settings.is_sqlite:
    apply_sqlite_pragmas(engine.sync_engine)


SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 路由依赖：每个请求一个 AsyncSession，请求结束自动关闭。"""
    async with SessionLocal() as session:
        yield session


def _async_engine_of(session: AsyncSession | None) -> AsyncEngine | None:
    """取某个会话**绑定**的异步引擎（取不到返回 ``None``，绝不抛）。

    先看 ``session.bind``（``async_sessionmaker`` 建出来的会话就带它），
    再退回 ``session.get_bind()``——两个都要判类型：``get_bind()`` 在部分实现下返回的是
    **同步**引擎，拿它去建异步会话是不合法的。
    """
    if session is None:
        return None
    bind = getattr(session, "bind", None)
    if isinstance(bind, AsyncEngine):
        return bind
    try:
        bind = session.get_bind()
    except Exception:  # noqa: BLE001 —— 取不到就当没有（调用方回落）
        return None
    return bind if isinstance(bind, AsyncEngine) else None


def new_independent_session(bound_to: AsyncSession | None = None) -> AsyncSession | None:
    """按 ``bound_to`` **所属的引擎**新开一个独立会话（阶段 4 · 条目 4.4 · 4.4.4）。

    为什么要有它（``回归修复`` 后半的根治）：Agent 的工具执行跑在 **CubeLoop 自己的
    asyncio 任务**上，而事件/审计落库跑在**消费端任务**上——两者原先共用**同一个请求级
    ``AsyncSession``**。数据库驱动不允许同一连接并发使用：实测撞出
    ``This session is in 'prepared' state`` / ``readexactly() called while another coroutine
    is already waiting`` ⇒ 收口失败 ⇒ 库里留一行永远 ``running`` 的僵尸行。
    4.3 加的那把 ``runner._session_lock`` 只是**绕**（把并发压成串行），这里才是根治：
    工具任务拿**自己的**会话，请求会话就只剩消费端一个使用者。

    **为什么按「调用方会话的引擎」而不是直接用全局 ``SessionLocal``**：单测里的会话绑在
    用例自己的临时引擎上（``tests/api/conftest.py`` 的 ``get_db`` 覆盖），直接用全局引擎会
    跨事件循环（连接池绑定在创建它的那个循环上）——按调用方引擎建，测的就是产品同一条路径。

    返回 ``None`` = 「建不出来」（没有绑定引擎 / 引擎不是异步的）⇒ 调用方**回落**到共用会话
    （旧行为），绝不因为一个优化项让功能下降。``bound_to`` 为 ``None`` 时同样返回 ``None``
    （工具级调用与单测大量走这条路，零开销直通）。
    """
    engine_or_none = _async_engine_of(bound_to)
    if engine_or_none is None:
        return None
    return async_sessionmaker(engine_or_none, class_=AsyncSession, expire_on_commit=False)()


@contextlib.asynccontextmanager
async def independent_session(
    bound_to: AsyncSession | None = None,
) -> AsyncIterator[AsyncSession | None]:
    """:func:`new_independent_session` 的上下文管理器：退出时**关闭**独立会话。

    ``yield`` 出去的可能就是 ``None``（建不出来）⇒ 调用方自己回落。关闭失败只记 warning
    ——收口路径上绝不因为「关不掉一个辅助会话」而炸穿（I5）。
    """
    session = new_independent_session(bound_to)
    try:
        yield session
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception as exc:  # noqa: BLE001 —— 关不掉也要让主流程走完
                logging.getLogger(__name__).warning(
                    "独立会话关闭失败（已隔离）：%s: %s", exc.__class__.__name__, exc
                )
