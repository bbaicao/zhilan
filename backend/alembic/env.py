from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 供 autogenerate 校对；0002 业务迁移仍手写明确 op，不依赖在线库
target_metadata = Base.metadata


def _sync_url() -> str:
    """迁移用**同步**驱动（阶段 3 · 条目 3.0 方言可切换）。

    - MySQL：``mysql+aiomysql://`` → ``mysql+pymysql://``（alembic 走同步引擎）；
    - SQLite：``sqlite+aiosqlite://`` → ``sqlite://``（缺省 pysqlite）。

    两个方言共用同一份迁移（0001–0004 + 将来的 0005），迁移脚本里的类型写法
    已经过方言无关化（``with_variant``），故这里只需换驱动、不动脚本。
    """
    url = settings.database_dsn
    if settings.is_sqlite:
        return url.replace("+aiosqlite", "")
    return url.replace("+aiomysql", "+pymysql")


def run_migrations_offline() -> None:
    url = _sync_url()
    context.configure(
        url=url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite 的 ALTER 能力有限：批量模式让将来的「改约束/改列」迁移能自动
        # 走「建新表→搬数据→改名」路径（MySQL 侧行为不受影响）。
        render_as_batch=settings.is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _sync_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            render_as_batch=settings.is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
