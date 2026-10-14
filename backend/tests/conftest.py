"""全仓测试夹具（阶段 1 · 条目 1.1 起；阶段 3 · 条目 3.0 起支持两种方言）。

DB 夹具放这里而不是某个子目录的 conftest：``tests/session``（消息持久化）与
``tests/routing``（决策日志落库）都要连真库，夹具只该有一份真相。

**两种方言**（阶段 3 · 条目 3.0 验收①：「两个方言下 alembic 三段迁移通过、契约子集绿」）：

- ``DB_BACKEND=mysql``（默认）：连容器 MySQL 的**独立测试库**（默认 ``zhilan_v2_test``），
  与开发库 ``zhilan_v2`` 隔离——避免踩坑清单里「pytest 与手工探针/老仓库测试互相污染」；
- ``DB_BACKEND=sqlite``：用 ``tmp_path_factory`` 下的**临时 SQLite 文件**，不依赖任何容器；
  PRAGMA（外键 / WAL / busy_timeout）复用产品同一份 ``apply_sqlite_pragmas``，
  保证测的就是产品路径。

**这些夹具不是 autouse**：只有显式请求 ``db`` / ``fresh_db`` / ``engine`` 的用例
才会碰数据库。纯函数用例（pipeline / vector / registry 等）在数据库没起时照样能跑。

建表用**同步**引擎做一次（session 级），异步夹具保持 function 级——
pytest-asyncio 的 session 级异步夹具会跑在另一个事件循环里，与 function 级夹具
不同环，aiomysql / aiosqlite 连接会跨环报错。
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.session import apply_sqlite_pragmas
from app.models import Base

# 独立测试库；容器里已建好并授权（见证据 S1-消息用例.log）
TEST_DATABASE = os.environ.get("V2_TEST_DATABASE", "zhilan_v2_test")


@pytest.fixture(autouse=True)
def isolate_event_failure_buckets():
    from app.session import events

    snapshot = events.failure_snapshot()
    events.reset_event_write_failures()
    try:
        yield
    finally:
        events.restore_event_write_failures(snapshot)

# 用例间要清空的表（顺序：先子后父，避免外键挡住）
#
# documents / chunks 于阶段 2 条目 2.1 起纳入：入库用例会往这两张表写行，
# 不清就会漏到下一条用例（``tests/api`` 里「列表应为空」这类断言会莫名失败，
# 也会让「重传同一份内容」的重复判定看到别的用例留下的行）。
#
# ⚠️ **阶段 4 · 条目 4.5 补齐（2026-09-19 实测踩到）**：新域用到的五张表
# （``notes`` / ``wiki_links`` / ``knowledge_bases`` / ``session_groups`` / ``usage_events``）
# 原先**不在清单里** ⇒ 用例写完的行会漏给后面的用例。实测症状：笔记用例先跑，
# 泄漏的笔记被**关键词路**（4.5.2 把笔记并进语料）检索到 ⇒ ``tests/api/test_chat_contract.py``
# 那三条「库内零命中 ⇒ direct/refuse」的用例变成 ``kind="normal"``（**3 红**）。
# 原注释写「本仓库装了 ``pytest-randomly``（默认**乱序**）」——**该注释过时**：
# 2026-09-19 实测本环境**未安装**该插件（``find_spec('pytest_randomly') → None``），
# 用例按文件顺序跑。但**表泄漏的风险依然在**（用例共享测试库），所以清表清单一条都不能省。
_CLEAN_ORDER = (
    "message_attributions",
    "agent_steps",
    "agent_runs",
    "session_events",
    "messages",
    "memories",
    "chats",
    "lexical_terms",
    "lexical_materials",
    "chunks",
    "document_builds",
    "document_similarity_edges",
    "documents",
    "notes",
    "wiki_links",
    "knowledge_bases",
    "session_groups",
    "usage_events",
)


def _mysql_url(driver: str) -> str:
    return settings.mysql_dsn.replace("+aiomysql", f"+{driver}").replace(
        f"/{settings.MYSQL_DATABASE}?", f"/{TEST_DATABASE}?"
    )


@pytest.fixture(scope="session")
def test_database() -> str:
    return TEST_DATABASE


@pytest.fixture(scope="session")
def test_async_url(tmp_path_factory) -> str:
    """测试库的**异步** DSN：MySQL 模式 = 独立测试库；SQLite 模式 = 临时文件。"""
    if settings.is_sqlite:
        db_file = tmp_path_factory.mktemp("sqlite") / "v2_test.db"
        return f"sqlite+aiosqlite:///{db_file.as_posix()}"
    return _mysql_url("aiomysql")


@pytest.fixture(scope="session")
def test_sync_url(test_async_url: str) -> str:
    """同步驱动版本（建表用）：aiomysql→pymysql；aiosqlite→pysqlite（缺省）。"""
    if settings.is_sqlite:
        return test_async_url.replace("+aiosqlite", "")
    return test_async_url.replace("+aiomysql", "+pymysql")


@pytest.fixture(scope="session")
def _schema(test_sync_url: str) -> Iterator[None]:
    """按模型定义建/重建表一次（与 alembic 同一份 ``Base.metadata``）。

    用 **drop_all + create_all** 而非仅 ``create_all``：测试库是一次性的，
    ``create_all`` 对**已存在的表不会补新列**，新增列会报 ``Unknown column``。
    先整库重建保证 schema 永远与当前 ``Base.metadata`` 一致（自包含、可复跑、利于
    变异测试复现）。真实库走 alembic 迁移，不靠这里。
    """
    sync_engine = create_engine(test_sync_url, pool_pre_ping=True)
    try:
        Base.metadata.drop_all(sync_engine)
        Base.metadata.create_all(sync_engine)
        yield
    finally:
        sync_engine.dispose()


@pytest_asyncio.fixture
async def engine(_schema, test_async_url: str) -> AsyncGenerator:
    """function 级异步引擎：与用例跑在同一个事件循环里。"""
    eng = create_async_engine(test_async_url, pool_pre_ping=True)
    if settings.is_sqlite:
        # 与产品同一份 PRAGMA：外键真生效、并发写等待有界（条目 3.0 验收 ⑥⑦）
        apply_sqlite_pragmas(eng.sync_engine)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def db(engine) -> AsyncGenerator[AsyncSession, None]:
    """每个用例一个干净会话：先清表，再让用例自由读写。"""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        for table in _CLEAN_ORDER:
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()
        try:
            yield session
        finally:
            await session.rollback()


@pytest_asyncio.fixture
async def fresh_db(engine) -> AsyncGenerator[AsyncSession, None]:
    """另一个独立会话：用于验证「换一个连接还能读到」= 真的落库了。"""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
