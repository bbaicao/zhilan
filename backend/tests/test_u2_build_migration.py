"""U2 迁移的最小真库检查：旧引用保留、版本回退拒绝、DDL 失败可回滚。

全部在 tmp_path SQLite 上，强制迁移连接开启外键，禁止落到真实配置库。
"""

from contextlib import contextmanager
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.operations import Operations
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.core.config import settings

BACKEND_DIR = Path(__file__).resolve().parent.parent


@contextmanager
def _foreign_keys_on():
    restored = []

    def connect(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    def checkin(dbapi_connection, _record):
        if dbapi_connection is not None:
            restored.append(dbapi_connection.execute("PRAGMA foreign_keys").fetchone()[0])

    event.listen(Engine, "connect", connect)
    event.listen(Engine, "checkin", checkin)
    try:
        yield restored
    finally:
        event.remove(Engine, "checkin", checkin)
        event.remove(Engine, "connect", connect)


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    path = tmp_path / "u2_migration.db"
    monkeypatch.setattr(settings, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "SQLITE_PATH", str(path))
    assert settings.is_sqlite and settings.sqlite_file == path
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    with _foreign_keys_on() as restored:
        command.upgrade(cfg, "0007")
    assert restored and set(restored) == {1}
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO documents "
            "(id,filename,file_type,status,index_status,chunk_count,raw_content) "
            "VALUES (41,'legacy.md','md','ready','ready',1,'旧正文 100')"
        )
        connection.execute(
            "INSERT INTO chunks (id,document_id,content,ordinal) VALUES (71,41,'旧正文 100',0)"
        )
        connection.execute("INSERT INTO chats (id,title) VALUES (81,'历史回答')")
        connection.execute(
            "INSERT INTO messages (id,chat_id,role,content,refs) "
            "VALUES (91,81,'assistant','历史回答','[{\"chunk_id\":71}]')"
        )
        connection.execute(
            "INSERT INTO message_attributions (id,message_id,chunk_id,ordinal,snippet) "
            "VALUES (101,91,71,1,'旧正文 100')"
        )
    return path, cfg


def _legacy_rows(path):
    with sqlite3.connect(path) as connection:
        return [
            connection.execute(query).fetchall()
            for query in (
                "SELECT id,raw_content,chunk_count FROM documents",
                "SELECT id,document_id,content,ordinal FROM chunks",
                "SELECT id,content,refs FROM messages",
                "SELECT id,message_id,chunk_id,ordinal,snippet FROM message_attributions",
            )
        ]


def _schema(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()


def test_u2_legacy_upgrade_and_downgrade_preserve_citations(legacy_db):
    path, cfg = legacy_db
    before = _legacy_rows(path)
    with _foreign_keys_on() as restored:
        command.upgrade(cfg, "0008")
        assert _legacy_rows(path) == before
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            assert connection.execute(
                "SELECT active_build_id,content_build_id FROM documents"
            ).fetchall() == [(None, None)]
            assert connection.execute("SELECT build_id FROM chunks").fetchall() == [(None,)]
            assert [row[2] for row in connection.execute(
                "PRAGMA index_info(uk_chunks_document_build_ordinal)"
            )] == ["document_id", "build_id", "ordinal"]
        command.downgrade(cfg, "0007")
    assert restored and set(restored) == {1}
    assert _legacy_rows(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO chunks (document_id,content,ordinal) VALUES (41,'同旧版本重复',0)"
            )


def test_u2_versioned_downgrade_refuses_before_any_ddl(legacy_db):
    path, cfg = legacy_db
    with _foreign_keys_on():
        command.upgrade(cfg, "0008")
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO document_builds "
            "(id,document_id,content_build_id,fingerprint,point_ids,snapshot) "
            "VALUES ('build-v2',41,'build-v2','{}','[]','{}')"
        )
        connection.execute(
            "INSERT INTO chunks (document_id,build_id,content,ordinal) "
            "VALUES (41,'build-v2','新正文 200',0)"
        )
        # 同文档跨版本序号可共存；同一个明确版本内仍由数据库拒重复。
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO chunks (document_id,build_id,content,ordinal) "
                "VALUES (41,'build-v2','同构建重复',0)"
            )
        connection.execute(
            "UPDATE documents SET active_build_id='build-v2',content_build_id='build-v2' WHERE id=41"
        )
    before_rows, before_schema = _legacy_rows(path), _schema(path)
    with _foreign_keys_on() as restored:
        with pytest.raises(RuntimeError, match="零结构改动"):
            command.downgrade(cfg, "0007")
    assert restored and set(restored) == {1}
    assert _legacy_rows(path) == before_rows
    assert _schema(path) == before_schema
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0008",)
        assert connection.execute("SELECT count(*) FROM document_builds").fetchone() == (1,)


def test_u2_ddl_failure_rolls_back_and_restores_foreign_keys(legacy_db, monkeypatch):
    path, cfg = legacy_db
    before_rows, before_schema = _legacy_rows(path), _schema(path)
    original = Operations.create_index

    def fail_last_index(self, index_name, *args, **kwargs):
        if index_name == "uk_chunks_document_build_ordinal":
            raise RuntimeError("injected U2 DDL failure")
        return original(self, index_name, *args, **kwargs)

    monkeypatch.setattr(Operations, "create_index", fail_last_index)
    with _foreign_keys_on() as restored:
        with pytest.raises(RuntimeError, match="injected U2 DDL failure"):
            command.upgrade(cfg, "0008")
    assert restored and set(restored) == {1}
    assert _legacy_rows(path) == before_rows
    assert _schema(path) == before_schema
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0007",)
