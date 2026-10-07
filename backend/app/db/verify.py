"""数据层连通性校验（阶段 2 · 2.1.3；阶段 3 · 条目 3.0 起方言感知）。

MYSQL_HINT / REDIS_HINT 文案与老仓库逐字一致——503 契约与 main.py 异常处理器依赖
（I1 只加不减）。SQLite 分支**新增独立常量** ``SQLITE_HINT``，不改旧常量。

方言口径（阶段 3 · 条目 3.0 交付物 ④）：

- MySQL（``DB_BACKEND=mysql``，开发态）：行为与改造前**逐字不变**——
  查 ``information_schema`` 业务表 + 笔记写读删 + Redis set/get 往返；
- SQLite（``DB_BACKEND=sqlite``，桌面态）：**不查 ``information_schema``**（SQLite 没有），
  改为等价自检——``sqlite_master`` 业务表齐备 + 笔记写读删（同时证明数据文件可写）。

Redis 分支两者相同：``app/db/redis.py`` 已是**进程内垫片**（零外部依赖），
``REDIS_HINT`` 因此只在垫片自身异常（正常不会）时出现，常量保留以满足契约与兼容。
"""

import asyncio
import sys
from uuid import uuid4

from sqlalchemy import select, text

from app.core.config import settings
from app.db.redis import get_redis_client
from app.db.session import SessionLocal, engine
from app.models import Note

MYSQL_HINT = "无法连接 MySQL，请先执行 docker compose up -d mysql redis，并确认 .env 中 MYSQL_* 配置正确"
REDIS_HINT = "无法连接 Redis，请先执行 docker compose up -d mysql redis，并确认 .env 中 REDIS_* 配置正确"
# 桌面语境提示（新增，不动上面两条）：SQLite 没有"起服务"这一步，问题在文件本身
SQLITE_HINT = (
    "无法打开本地数据库文件，请确认数据目录可写、磁盘空间充足，"
    "并关闭其它正在占用该文件的程序后重试"
)

#: 业务表全量（顺序同 `app/models/__init__.py` 的「表清单」docstring 与迁移产出顺序）。
#:
#: ⚠️ **单一真相源**：MySQL 分支的 `IN (...)` 串与两处「N 张表」文案都从它派生——
#: 5.1 之前这三处是各写一份的字面量，于是 `memories` 加进来时只补了集合、两处文案停留在 11
#: （实测登记：`阶段5/证据/回归检查1口径.md` §5）。现在加表只改这一处。
TABLE_NAMES: tuple[str, ...] = (
    "notes",
    "documents",
    "document_builds",
    "chunks",
    "chats",
    "messages",
    "session_groups",
    "wiki_links",
    "knowledge_bases",
    "usage_events",
    "session_events",
    # 0003：引用归因 + Agent 审计两张（原集合漏登记，2026-09-19 随 5.3 开工前置补齐）
    "message_attributions",
    "agent_runs",
    "agent_steps",
    # 阶段 5 · 条目 5.1：跨会话记忆
    "memories",
    # U3：普通 SQL 词项倒排和笔记内部片段。
    "lexical_materials",
    "lexical_terms",
)

EXPECTED_TABLES = set(TABLE_NAMES)

#: MySQL 自检用的 `table_name IN (...)` 片段（从 :data:`TABLE_NAMES` 派生，**不再手写**）。
_TABLE_IN_LIST = ",".join(f"'{name}'" for name in TABLE_NAMES)


async def _check_mysql() -> None:
    async with SessionLocal() as session:
        try:
            rows = (
                await session.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() AND table_name IN "
                        f"({_TABLE_IN_LIST}) "
                        "ORDER BY table_name"
                    )
                )
            ).scalars().all()
        except Exception as exc:
            raise RuntimeError(MYSQL_HINT) from exc

        if set(rows) != EXPECTED_TABLES:
            raise RuntimeError(f"业务表不完整：{rows}。请先执行 alembic upgrade head")

        note = Note(title="阶段02校验", content="用于确认异步写入读取", category="校验")
        session.add(note)
        await session.commit()
        await session.refresh(note)

        loaded = (
            await session.execute(select(Note).where(Note.id == note.id))
        ).scalar_one()
        if loaded.title != "阶段02校验":
            raise RuntimeError("笔记写入后读回内容不一致")

        await session.delete(loaded)
        await session.commit()


async def _check_sqlite() -> None:
    """SQLite 等价自检：业务表齐 + 数据文件可写（写读删一条笔记）。

    刻意**不查 ``information_schema``**（SQLite 不提供），改查 ``sqlite_master``；
    表缺失的提示与 MySQL 分支同义（先跑 ``alembic upgrade head``）。
    """
    async with SessionLocal() as session:
        try:
            rows = (
                await session.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'table'")
                )
            ).scalars().all()
        except Exception as exc:
            raise RuntimeError(SQLITE_HINT) from exc

        missing = sorted(EXPECTED_TABLES - set(rows))
        if missing:
            raise RuntimeError(f"业务表不完整：缺 {missing}。请先执行 alembic upgrade head")

        try:
            note = Note(title="阶段02校验", content="用于确认异步写入读取", category="校验")
            session.add(note)
            await session.commit()
            await session.refresh(note)

            loaded = (
                await session.execute(select(Note).where(Note.id == note.id))
            ).scalar_one()
            if loaded.title != "阶段02校验":
                raise RuntimeError("笔记写入后读回内容不一致")

            await session.delete(loaded)
            await session.commit()
        except RuntimeError:
            raise
        except Exception as exc:
            # 只读目录 / 磁盘满 / 文件被其它进程独占 —— 都归到桌面语境提示
            raise RuntimeError(SQLITE_HINT) from exc


async def _check_redis() -> None:
    redis = get_redis_client()
    key = f"zhilan:verify:{uuid4().hex}"
    try:
        await redis.set(key, "ok", ex=30)
        value = await redis.get(key)
        await redis.delete(key)
    except Exception as exc:
        raise RuntimeError(REDIS_HINT) from exc
    finally:
        await redis.aclose()
    if value != "ok":
        raise RuntimeError("Redis set/get 校验失败")


async def _run() -> None:
    try:
        if settings.is_sqlite:
            await _check_sqlite()
        else:
            await _check_mysql()
        await _check_redis()
    finally:
        await engine.dispose()
    if settings.is_sqlite:
        print(f"数据层校验通过：{len(TABLE_NAMES)} 张表、笔记读写、Redis 垫片 set/get 均正常（SQLite）")
    else:
        print(f"数据层校验通过：{len(TABLE_NAMES)} 张表、笔记读写、Redis set/get 均正常")


def main() -> None:
    try:
        asyncio.run(_run())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        if exc.__cause__ is not None:
            print(f"底层原因：{exc.__cause__}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
