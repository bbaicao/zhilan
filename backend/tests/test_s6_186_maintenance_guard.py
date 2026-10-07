"""回归检查: automatic migration/startup must stop before unsafe KB maintenance."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from tests.test_s6_186_kb_identity import (
    BACKEND_DIR,
    MIGRATION_0007,
    _engine,
    _rows,
    _seed_duplicate_0006,
    _upgrade,
    sqlite_db,
)
from tests.test_migration_chain import EXPECTED_HEAD


def _target(db_path):
    return {"dialect": "sqlite", "database": os.path.normcase(str(db_path.resolve()))}


def _token(target):
    raw = json.dumps(target, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pending(tmp_path, db_path, monkeypatch):
    root = tmp_path / "maintenance"
    root.mkdir()
    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(root))
    target = _target(db_path)
    operation = "guard-operation"
    path = root / f"kb-maintenance-{_token(target)}-{operation}.pending.json"
    path.write_text(
        json.dumps({
            "schema": "kb-maintenance/v1", "target": target, "operation_id": operation,
            "operation_dir": str(tmp_path / "operation"), "required_collections": ["zhilan_chunks"],
            "artifacts_sha256": {},
        }),
        encoding="utf-8",
    )
    return path


def test_auto_upgrade_refuses_compatible_duplicates_before_any_write(sqlite_db, monkeypatch):
    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(sqlite_db.parent / "materials"))
    _upgrade("0006")
    _seed_duplicate_0006(sqlite_db)
    engine = _engine(sqlite_db)
    try:
        kb_before = _rows(engine, "SELECT * FROM knowledge_bases ORDER BY id")
        refs_before = {table: _rows(engine, f"SELECT id,kb_id FROM {table} ORDER BY id")
                       for table in ("notes", "documents", "wiki_links")}
        with pytest.raises(RuntimeError, match="维护|maintenance") as caught:
            _upgrade("head")
        assert getattr(caught.value, "kb_maintenance_required", False)
        assert _rows(engine, "SELECT * FROM knowledge_bases ORDER BY id") == kb_before
        assert "path_or_url_key" not in {c["name"] for c in inspect(engine).get_columns("knowledge_bases")}
        assert _rows(engine, "SELECT version_num FROM alembic_version") == [{"version_num": "0006"}]
        for table, expected in refs_before.items():
            assert _rows(engine, f"SELECT id,kb_id FROM {table} ORDER BY id") == expected
    finally:
        engine.dispose()


def test_auto_upgrade_refuses_pending_even_after_sql_has_no_duplicates(sqlite_db, monkeypatch, tmp_path):
    _upgrade("0006")
    _pending(tmp_path, sqlite_db, monkeypatch)
    with pytest.raises(RuntimeError, match="维护|maintenance"):
        _upgrade("head")
    engine = _engine(sqlite_db)
    try:
        assert "path_or_url_key" not in {c["name"] for c in inspect(engine).get_columns("knowledge_bases")}
        assert _rows(engine, "SELECT version_num FROM alembic_version") == [{"version_num": "0006"}]
    finally:
        engine.dispose()


def test_head_noop_cannot_accept_a_missing_unique_index(sqlite_db):
    import main
    from app.core.kb_identity import KnowledgeBaseMaintenanceRequired

    _upgrade("head")
    engine = _engine(sqlite_db)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX uk_knowledge_bases_path_or_url_key"))
        with pytest.raises(KnowledgeBaseMaintenanceRequired, match="索引|约束"):
            main.run_migrations()
        assert _rows(engine, "SELECT version_num FROM alembic_version") == [{"version_num": EXPECTED_HEAD}]
        with engine.begin() as conn:
            conn.execute(text("CREATE UNIQUE INDEX uk_knowledge_bases_path_or_url_key ON knowledge_bases(path_or_url_key)"))
        main.run_migrations()
        assert any(i["name"] == "uk_knowledge_bases_path_or_url_key" and i["unique"]
                   for i in inspect(engine).get_indexes("knowledge_bases"))
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_startup_does_not_yield_after_maintenance_rejection(monkeypatch):
    import main

    class MaintenanceRequired(RuntimeError):
        kb_maintenance_required = True

    def blocked():
        raise MaintenanceRequired("S6-186 维护尚未完成")

    monkeypatch.setattr(main, "setup_app_logging", lambda: None)
    monkeypatch.setattr(main, "ensure_service_registry", lambda: False)
    monkeypatch.setattr(main, "migrate_legacy_rerank", lambda: {})
    monkeypatch.setattr(main, "run_migrations", blocked)
    if hasattr(main, "ensure_kb_maintenance_complete"):
        monkeypatch.setattr(main, "ensure_kb_maintenance_complete", lambda dsn, **kwargs: None)
    with pytest.raises(RuntimeError, match="维护尚未完成"):
        async with main.lifespan(main.app):
            pytest.fail("maintenance rejection must stop startup before yielding")


@pytest.mark.asyncio
async def test_startup_checks_pending_when_alembic_head_is_noop(sqlite_db, monkeypatch, tmp_path):
    import main

    _upgrade("head")
    _pending(tmp_path, sqlite_db, monkeypatch)
    monkeypatch.setattr(main, "setup_app_logging", lambda: None)
    monkeypatch.setattr(main, "ensure_service_registry", lambda: False)
    monkeypatch.setattr(main, "migrate_legacy_rerank", lambda: {})
    monkeypatch.setattr(main, "run_migrations", lambda: None)
    with pytest.raises(RuntimeError, match="维护|maintenance"):
        async with main.lifespan(main.app):
            pytest.fail("already-head startup must not bypass a pending operation")


@pytest.mark.asyncio
@pytest.mark.parametrize("vector_url", ["", "invalid-url"])
async def test_no_pending_preserves_ordinary_offline_startup(sqlite_db, monkeypatch, vector_url):
    import main

    async def closed():
        return None

    def offline():
        raise RuntimeError("ordinary offline database")

    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(sqlite_db.parent / "no-maintenance"))
    monkeypatch.setattr(main.settings, "QDRANT_URL", vector_url)
    monkeypatch.setattr(main.settings, "QDRANT_PATH", "")
    monkeypatch.setattr(main, "setup_app_logging", lambda: None)
    monkeypatch.setattr(main, "ensure_service_registry", lambda: False)
    monkeypatch.setattr(main, "migrate_legacy_rerank", lambda: {})
    monkeypatch.setattr(main, "run_migrations", offline)
    from types import SimpleNamespace
    monkeypatch.setattr(main, "engine", SimpleNamespace(dispose=closed))
    monkeypatch.setattr(main, "close_redis", closed)
    monkeypatch.setattr(main, "close_vector_store", lambda: None)
    async with main.lifespan(main.app):
        pass  # entering without raising is the behavior under test


def test_target_and_pending_checks_match_historical_migration(tmp_path, monkeypatch):
    from app.core import kb_identity

    spec = importlib.util.spec_from_file_location("kb_guard_migration", MIGRATION_0007)
    migration = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(migration)
    for dsn in ("sqlite:///" + (tmp_path / "db.sqlite").as_posix(),
                "mysql+pymysql://example-user:unused@example.invalid:3307/kb_test"):
        target = kb_identity.kb_target_identity(dsn)
        assert target == migration._target_identity(dsn)
        assert kb_identity.kb_target_token(target) == migration._target_token(target)
        assert "unused" not in repr(target) and "example-user" not in repr(target)
    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(tmp_path / "custom"))
    assert kb_identity.kb_maintenance_dir() == migration._journal_dir() == tmp_path / "custom"


def _complete_batch(root, target, operation="verified-op"):
    """Build one complete immutable batch; tests then corrupt one real bound file at a time."""
    from app.core import kb_identity

    directory = root / operation
    directory.mkdir()
    journal = directory / "redirects.json"
    journal.write_text(json.dumps({"redirects": [{"from": 3, "to": 2}]}), encoding="utf-8")

    def write(name, value):
        path = directory / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    snapshot = write("snapshot.json", {
        "schema": "kb-vector-points/v1", "target": target, "operation_id": operation,
        "collection": "zhilan_chunks", "points_total": 1,
        "vector_target": {"mode": "path", "location": "expected-vector"},
        "journal_sha256": hashlib.sha256(journal.read_bytes()).hexdigest(),
        "entries": [{"from": 3, "to": 2, "point_ids": ["point1"], "points_total": 1}],
    })
    pending = {
        "schema": "kb-maintenance/v1", "target": target, "operation_id": operation,
        "operation_dir": str(directory), "required_collections": ["zhilan_chunks"],
        "artifacts_sha256": {str(journal): hashlib.sha256(journal.read_bytes()).hexdigest()},
    }
    pending_path = kb_identity.kb_pending_path(target, operation)
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text(json.dumps(pending), encoding="utf-8")
    sql = write("sql.json", {
        "schema": "kb-sql-receipt/v1", "target": target, "operation_id": operation,
        "outcome": "applied", "journal_sha256": pending["artifacts_sha256"][str(journal)],
    })
    receipt = write("receipt.json", {
        "schema": "kb-vector-receipt/v1", "target": target, "operation_id": operation,
        "collection": "zhilan_chunks", "vector_target": {"mode": "path", "location": "expected-vector"},
        "outcome": "verified", "ok": True, "snapshot": snapshot["path"],
        "snapshot_sha256": snapshot["sha256"], "journal_sha256": pending["artifacts_sha256"][str(journal)],
        "checks": {"expected_points": 1, "checked_points": 1, "missing": 0, "wrong": 0, "leftover_old": 0},
    })
    completion = {
        "schema": "kb-maintenance-complete/v1", "target": target, "operation_id": operation,
        "pending_sha256": hashlib.sha256(pending_path.read_bytes()).hexdigest(),
        "required_collections": ["zhilan_chunks"], "outcome": "verified",
        "sql_receipt": sql, "receipts": {"zhilan_chunks": receipt},
    }
    completion_path = pending_path.with_name(pending_path.name.replace(".pending.json", ".complete.json"))
    completion_path.write_text(json.dumps(completion), encoding="utf-8")
    return directory, pending_path, completion_path


def _guard_functions():
    from app.core.kb_identity import ensure_kb_maintenance_complete
    spec = importlib.util.spec_from_file_location("kb_guard_completed_migration", MIGRATION_0007)
    migration = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(migration)
    return ensure_kb_maintenance_complete, migration._check_maintenance


@pytest.mark.parametrize("damaged", ["pending", "journal", "completion", "sql", "receipt", "snapshot"])
def test_both_guards_fail_closed_when_any_bound_material_is_changed(tmp_path, monkeypatch, damaged):
    from app.core import kb_identity

    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(tmp_path / "markers"))
    dsn = "sqlite:///" + (tmp_path / "db.sqlite").as_posix()
    directory, pending, completion = _complete_batch(tmp_path, kb_identity.kb_target_identity(dsn))
    for guard in _guard_functions():
        guard(dsn)
    target_path = {"pending": pending, "journal": directory / "redirects.json", "completion": completion,
                   "sql": directory / "sql.json", "receipt": directory / "receipt.json",
                   "snapshot": directory / "snapshot.json"}[damaged]
    target_path.write_text("{}", encoding="utf-8")
    for guard in _guard_functions():
        with pytest.raises(RuntimeError, match="维护"):
            guard(dsn)


def test_every_pending_batch_and_current_vector_target_must_be_complete(tmp_path, monkeypatch):
    from app.core import kb_identity

    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(tmp_path / "markers"))
    dsn = "sqlite:///" + (tmp_path / "db.sqlite").as_posix()
    target = kb_identity.kb_target_identity(dsn)
    _complete_batch(tmp_path, target)
    kb_identity.ensure_kb_maintenance_complete(dsn, expected_vector_target={"mode": "path", "location": "expected-vector"})
    with pytest.raises(RuntimeError, match="维护"):
        kb_identity.ensure_kb_maintenance_complete(dsn, expected_vector_target={"mode": "path", "location": "other-vector"})
    pending = kb_identity.kb_pending_path(target, "second-op")
    pending.write_text(json.dumps({"schema": "kb-maintenance/v1", "target": target,
                                  "operation_id": "second-op", "artifacts_sha256": {}}), encoding="utf-8")
    for guard in _guard_functions():
        with pytest.raises(RuntimeError, match="维护"):
            guard(dsn)


def test_maintenance_target_scope_and_frozen_directory_are_consistent(tmp_path, monkeypatch):
    from app.core import kb_identity
    import sys

    dsn = "sqlite:///" + (tmp_path / "one" / "same.sqlite").as_posix()
    other = "sqlite:///" + (tmp_path / "two" / "same.sqlite").as_posix()
    assert kb_identity.kb_target_identity(dsn) != kb_identity.kb_target_identity(other)
    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(tmp_path / "markers"))
    _complete_batch(tmp_path, kb_identity.kb_target_identity(other))
    for guard in _guard_functions():
        guard(dsn)  # unrelated immutable history cannot block this SQL target
    monkeypatch.delenv("ZHILAN_KB_MERGE_DIR")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "installed" / "zhilan.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "readonly-unpacked"), raising=False)
    spec = importlib.util.spec_from_file_location("kb_guard_frozen_migration", MIGRATION_0007)
    migration = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(migration)
    assert kb_identity.kb_maintenance_dir() == migration._journal_dir() == tmp_path / "installed" / "data" / "kb-merge"


def test_development_maintenance_directory_uses_shared_root_not_working_directory(tmp_path, monkeypatch):
    from app.core import env_store, kb_identity
    import sys

    monkeypatch.delenv("ZHILAN_KB_MERGE_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(env_store, "resolve_data_root", lambda: tmp_path / "repository")
    monkeypatch.chdir(tmp_path)
    assert kb_identity.kb_maintenance_dir() == tmp_path / "repository" / "backend" / "data" / "kb-merge"


def _aborted_batch(root, target):
    """A zero-write transaction can close its immutable batch without any vector operation."""
    from app.core import kb_identity

    operation = "aborted-op"
    directory = root / operation
    directory.mkdir()

    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    journal = write(directory / "redirects.json", {"redirects": [{"from": 3, "to": 2}]})
    pending = {
        "schema": "kb-maintenance/v1", "target": target, "operation_id": operation,
        "operation_dir": str(directory), "required_collections": ["zhilan_chunks"],
        "artifacts_sha256": {journal["path"]: journal["sha256"]},
    }
    pending_path = kb_identity.kb_pending_path(target, operation)
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    write(pending_path, pending)
    state = {"knowledge_bases": [{"id": 2, "name": "externally changed"}],
             "references": {"notes": [], "documents": [], "wiki_links": []}}
    abort_state = write(directory / "sql-abort-state.json", {
        "schema": "kb-sql-abort-state/v1", "target": target, "operation_id": operation,
        "before": state, "after": state,
    })
    sql = write(directory / "sql-aborted.json", {
        "schema": "kb-sql-receipt/v1", "target": target, "operation_id": operation,
        "outcome": "aborted", "journal_sha256": journal["sha256"],
        "rollback_confirmed": True, "no_writes": True, "abort_state": abort_state,
    })
    complete = write(pending_path.with_name(pending_path.name.replace(".pending.json", ".complete.json")), {
        "schema": "kb-maintenance-complete/v1", "target": target, "operation_id": operation,
        "pending_sha256": hashlib.sha256(pending_path.read_bytes()).hexdigest(),
        "required_collections": ["zhilan_chunks"], "outcome": "aborted",
        "sql_receipt": sql, "receipts": {},
    })
    return directory, sql, complete


def test_both_guards_accept_a_bound_zero_write_abort_without_vector_receipts(tmp_path, monkeypatch):
    from app.core import kb_identity

    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(tmp_path / "markers"))
    dsn = "sqlite:///" + (tmp_path / "db.sqlite").as_posix()
    _aborted_batch(tmp_path, kb_identity.kb_target_identity(dsn))
    for guard in _guard_functions():
        guard(dsn)
    # No vector operation took place, so changing the configured vector target cannot invalidate abort.
    kb_identity.ensure_kb_maintenance_complete(dsn, expected_vector_target={"mode": "path", "location": "unused"})


@pytest.mark.parametrize("damaged", ["state_changed", "state_target", "rollback_unknown", "state_hash_missing",
                                   "sql_hash_missing", "vector_receipts"])
def test_abort_cannot_close_unknown_or_unbound_transaction_state(tmp_path, monkeypatch, damaged):
    from app.core import kb_identity

    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(tmp_path / "markers"))
    dsn = "sqlite:///" + (tmp_path / "db.sqlite").as_posix()
    directory, sql_ref, complete_ref = _aborted_batch(tmp_path, kb_identity.kb_target_identity(dsn))
    sql = json.loads(Path(sql_ref["path"]).read_text(encoding="utf-8"))
    complete = json.loads(Path(complete_ref["path"]).read_text(encoding="utf-8"))
    if damaged.startswith("state_") and damaged != "state_hash_missing":
        path = directory / "sql-abort-state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        if damaged == "state_changed":
            state["after"] = {"knowledge_bases": []}
        else:
            state["target"] = {"dialect": "sqlite", "database": "another.sqlite"}
        path.write_text(json.dumps(state), encoding="utf-8")
        sql["abort_state"]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif damaged == "rollback_unknown":
        sql["rollback_confirmed"] = False
    elif damaged == "state_hash_missing":
        del sql["abort_state"]["sha256"]
    elif damaged == "sql_hash_missing":
        del complete["sql_receipt"]["sha256"]
    else:
        complete["receipts"] = {"zhilan_chunks": {"path": "irrelevant", "sha256": "unused"}}
    Path(sql_ref["path"]).write_text(json.dumps(sql), encoding="utf-8")
    if damaged != "sql_hash_missing":
        complete["sql_receipt"]["sha256"] = hashlib.sha256(Path(sql_ref["path"]).read_bytes()).hexdigest()
    Path(complete_ref["path"]).write_text(json.dumps(complete), encoding="utf-8")
    for guard in _guard_functions():
        with pytest.raises(RuntimeError, match="维护"):
            guard(dsn)


@pytest.mark.parametrize("filename", ["sql-applied.json", "sql-restored-aborted-op.json",
                                     "vector-points-original.json", "vector-verified-aborted-op.json"])
def test_abort_cannot_bypass_any_commit_or_vector_evidence(tmp_path, monkeypatch, filename):
    from app.core import kb_identity

    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(tmp_path / "markers"))
    dsn = "sqlite:///" + (tmp_path / "db.sqlite").as_posix()
    directory, _, _ = _aborted_batch(tmp_path, kb_identity.kb_target_identity(dsn))
    (directory / filename).write_text("{}", encoding="utf-8")
    for guard in _guard_functions():
        with pytest.raises(RuntimeError, match="维护"):
            guard(dsn)


@pytest.mark.parametrize("field", ["vector_target", "journal_sha256"])
def test_snapshot_cannot_disagree_with_its_verified_receipt(tmp_path, monkeypatch, field):
    from app.core import kb_identity

    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(tmp_path / "markers"))
    dsn = "sqlite:///" + (tmp_path / "db.sqlite").as_posix()
    directory, _, complete_path = _complete_batch(tmp_path, kb_identity.kb_target_identity(dsn))
    snapshot_path, receipt_path = directory / "snapshot.json", directory / "receipt.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot[field] = {"mode": "path", "location": "another-vector"} if field == "vector_target" else "0" * 64
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["snapshot_sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    complete = json.loads(complete_path.read_text(encoding="utf-8"))
    complete["receipts"]["zhilan_chunks"]["sha256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    complete_path.write_text(json.dumps(complete), encoding="utf-8")
    for guard in _guard_functions():
        with pytest.raises(RuntimeError, match="维护"):
            guard(dsn)


def _inject_index_failure(monkeypatch):
    from alembic.operations import Operations
    from sqlalchemy.exc import OperationalError

    def failed_index(*args, **kwargs):
        raise OperationalError("CREATE UNIQUE INDEX probe", {}, RuntimeError("injected index failure"))

    monkeypatch.setattr(Operations, "create_index", failed_index)


def test_0007_ddl_failure_is_a_maintenance_blocker_after_schema_write(sqlite_db, monkeypatch):
    from sqlalchemy.exc import OperationalError

    _upgrade("0006")
    engine = _engine(sqlite_db)
    before = _rows(engine, "SELECT * FROM knowledge_bases ORDER BY id")
    _inject_index_failure(monkeypatch)
    try:
        with pytest.raises(RuntimeError, match="维护|约束") as caught:
            _upgrade("head")
        assert getattr(caught.value, "kb_maintenance_required", False)
        assert isinstance(caught.value.__cause__, OperationalError)
        assert _rows(engine, "SELECT id,name,path_or_url FROM knowledge_bases ORDER BY id") == [
            {key: row[key] for key in ("id", "name", "path_or_url")} for row in before
        ]
        assert _rows(engine, "SELECT version_num FROM alembic_version") == [{"version_num": "0006"}]
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_startup_does_not_open_after_actual_0007_ddl_failure(sqlite_db, monkeypatch):
    import main

    _upgrade("0006")
    _inject_index_failure(monkeypatch)
    monkeypatch.setattr(main, "setup_app_logging", lambda: None)
    monkeypatch.setattr(main, "ensure_service_registry", lambda: False)
    monkeypatch.setattr(main, "migrate_legacy_rerank", lambda: {})
    with pytest.raises(RuntimeError, match="维护|约束") as caught:
        async with main.lifespan(main.app):
            pytest.fail("partial 0007 schema must not be treated as an ordinary offline database")
    assert getattr(caught.value, "kb_maintenance_required", False)
