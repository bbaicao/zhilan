"""迁移与 Agent 两种执行顺序：同一业务 logger 和 Alembic 日志都可观察。"""

import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from app.agent.bootstrap import build_registry
from app.core.config import settings
from tests.test_agent_registry import _dup_packs
from tests.test_migration_chain import EXPECTED_HEAD


@pytest.mark.parametrize("migration_first", [False, True], ids=["agent-first", "migration-first"])
def test_migration_preserves_agent_and_alembic_logs(tmp_path, monkeypatch, capsys, migration_first):
    configured_loggers = [logging.getLogger(name) for name in ("", "alembic", "sqlalchemy.engine")]
    saved_configs = [
        (item, item.handlers[:], item.level, item.disabled, item.propagate)
        for item in configured_loggers
    ]
    logger = logging.getLogger("app.agent.bootstrap")
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    saved = logger.disabled, logger.level, logger.propagate
    logger.disabled, logger.propagate = False, False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    def agent():
        registry = build_registry(mcp_packs=_dup_packs())
        names = [spec.name for spec in registry.list(include_dangerous=True)]
        assert names.count("mcp:dup:t1") == 1
        assert "mcp:dup:t2" in names and "knowledge_search" in names
        assert records[-1] == "MCP 契约名重复，已跳过：mcp:dup:t1"

    try:
        logger.info("before-migration")
        assert records == ["before-migration"]
        if not migration_first:
            agent()
        database = tmp_path / "migration-logging.db"
        monkeypatch.setattr(settings, "DB_BACKEND", "sqlite")
        monkeypatch.setattr(settings, "SQLITE_PATH", str(database))
        assert settings.is_sqlite and settings.sqlite_file == database
        backend = Path(__file__).resolve().parents[1]
        config = Config(str(backend / "alembic.ini"))
        config.set_main_option("script_location", str(backend / "alembic"))
        assert ScriptDirectory.from_config(config).get_heads() == [EXPECTED_HEAD]
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{database.as_posix()}")
        try:
            with engine.connect() as connection:
                assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == EXPECTED_HEAD
        finally:
            engine.dispose()
        assert "Running upgrade" in capsys.readouterr().err
        logger.info("after-migration")
        assert records[-1] == "after-migration", f"logger disabled={logger.disabled}"
        agent()
        assert records.count("MCP 契约名重复，已跳过：mcp:dup:t1") == (1 if migration_first else 2)
    finally:
        logger.removeHandler(handler)
        logger.disabled, logger.level, logger.propagate = saved
        # 迁移装上的 stderr handler 不能留给下一个用例的捕获上下文。
        for item, handlers, level, disabled, propagate in saved_configs:
            item.handlers[:] = handlers
            item.level, item.disabled, item.propagate = level, disabled, propagate
