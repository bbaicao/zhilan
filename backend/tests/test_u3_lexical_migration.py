"""U3 临时 SQLite 迁移回填；MySQL 只校验方言 DDL，不冒充真服务验收。"""

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.schema import CreateTable

from app.core.config import settings
from app.db.session import apply_sqlite_pragmas
from app.models import LexicalMaterial, LexicalTerm
from app.pipeline.lexical_index import backfill_connection, note_fragments

BACKEND_DIR = Path(__file__).resolve().parent.parent


def test_mysql_binary_posting_key_and_sqlite_compact_primary_key():
    mysql_ddl = str(CreateTable(LexicalTerm.__table__).compile(dialect=mysql.dialect()))
    sqlite_ddl = str(CreateTable(LexicalTerm.__table__).compile(dialect=sqlite.dialect()))
    assert "VARBINARY(33)" in mysql_ddl
    assert "PRIMARY KEY (term_hash, owner_material_id)" in mysql_ddl
    assert "FULLTEXT" not in mysql_ddl
    assert "WITHOUT ROWID" in sqlite_ddl
    assert LexicalMaterial.__table__.c.chunk_id.foreign_keys
    assert {key.ondelete for column in LexicalMaterial.__table__.columns for key in column.foreign_keys} == {"CASCADE"}


def test_0009_populated_upgrade_rerun_and_downgrade_preserve_sources(tmp_path, monkeypatch):
    path = tmp_path / "u3_lexical_migration.db"
    monkeypatch.setattr(settings, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "SQLITE_PATH", str(path))
    assert settings.is_sqlite and settings.sqlite_file == path
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "0008")
    note_content = "# 长笔记\n" + "向量检索🙂。" * 250
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO documents (id,filename,file_type,status,index_status,chunk_count) "
            "VALUES (21,'legacy.md','md','ready','ready',2)"
        )
        connection.execute(
            "INSERT INTO chunks (id,document_id,content,ordinal) VALUES "
            "(31,21,'oldsignal 原正文',0),(32,21,'数字 100 2026',1)"
        )
        connection.execute(
            "INSERT INTO notes (id,title,content,category) VALUES (41,?,?,'')",
            ("同名笔记", note_content),
        )
        source_rows = [
            connection.execute(sql).fetchall() for sql in (
                "SELECT id,filename,active_build_id,content_build_id FROM documents",
                "SELECT id,document_id,content,ordinal,build_id FROM chunks",
                "SELECT id,title,content FROM notes",
            )
        ]
    command.upgrade(cfg, "0009")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    apply_sqlite_pragmas(engine)
    try:
        with engine.begin() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            expected_fragments = len(note_fragments(note_content))
            assert connection.exec_driver_sql("SELECT count(*) FROM lexical_materials").scalar_one() == 2 + expected_fragments
            assert connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name='lexical_terms'"
            ).scalar_one().rstrip().endswith("WITHOUT ROWID")
            before_count = connection.exec_driver_sql("SELECT count(*) FROM lexical_terms").scalar_one()
            first = backfill_connection(connection, batch_size=1)
            second = backfill_connection(connection, batch_size=1)
            assert first == second
            assert (first["chunk_count"], first["note_count"], first["fragment_count"]) == (2, 1, expected_fragments)
            assert connection.exec_driver_sql("SELECT count(*) FROM lexical_terms").scalar_one() == before_count
            for sql, expected in zip((
                "SELECT id,filename,active_build_id,content_build_id FROM documents",
                "SELECT id,document_id,content,ordinal,build_id FROM chunks",
                "SELECT id,title,content FROM notes",
            ), source_rows):
                assert [tuple(row) for row in connection.exec_driver_sql(sql)] == expected
    finally:
        engine.dispose()
    command.downgrade(cfg, "0008")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0008",)
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'lexical_%'").fetchall()
        assert connection.execute("SELECT id,title,content FROM notes").fetchall() == source_rows[2]
