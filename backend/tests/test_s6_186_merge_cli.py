"""回归检查 · 存量合并 / 恢复 CLI（backend/scripts/kb_merge_duplicates.py）的机器可读出口与硬边界判据。

为什么单独钉这组：交付项 D 要求「预检 + 合并 + 恢复材料」这个入口能被运维与自动化消费。恢复
材料演练（_s6-scratch/s6-db/s6_186_restore_drill.py）真跑时实测到 `--preflight` / `--apply`
带 `--json` **什么都不打印**（JSON 只落到 --out 文件里）⇒ 自动化读不到结果。本文件把三种
模式的机器可读出口、以及恢复侧两条硬边界（唯一索引没撤不许恢复 / 撤了才恢复）钉成判据。

目标库一律是 pytest tmp_path 下的独立 SQLite 文件：先用真 alembic 升到 0006（造出 0007 之前
的形态），再用裸 sqlite3 插重复行；断言里逐条核对目标与 .env 无关。
"""
from __future__ import annotations

import json
import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND_DIR / "scripts" / "kb_merge_duplicates.py"
PATH_A = "D:/s6/cli/dup"
PATH_B = "D:/s6/cli/other"
REF_TABLES = ("notes", "documents", "wiki_links")
KB_COLUMNS = "(id, name, kind, path_or_url, collection, read_only, status, last_sync_at)"
KB_VALUES = "VALUES (?, ?, ?, ?, ?, 0, ?, NULL)"
NOTE_SQL = "INSERT INTO notes (id, title, content, category, kb_id, deleted_at) VALUES (?, ?, ?, ?, ?, ?)"
DOC_SQL = ("INSERT INTO documents (id, filename, file_type, status, index_status, chunk_count,"
           " vectorized, kb_id, category, deleted_at) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?)")
WIKI_SQL = ("INSERT INTO wiki_links (id, source_type, source_id, kb_id, target_title, resolved)"
            " VALUES (?, ?, ?, ?, ?, 0)")


@pytest.fixture(autouse=True)
def isolated_maintenance(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(tmp_path / "maintenance-gates"))


def _dsn(db: Path) -> str:
    return "sqlite:///" + db.as_posix()


def _alembic_0006(db: Path) -> None:
    env = dict(os.environ)
    env["DB_BACKEND"] = "sqlite"
    env["SQLITE_PATH"] = str(db)
    env["PYTHONPATH"] = str(BACKEND_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("ZHILAN_KB_MERGE_DIR", None)
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "alembic", "upgrade", "0006"],
        cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, "alembic upgrade 0006 失败：" + (proc.stderr or "")[-500:]


def _run_tool(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *args],
        cwd=str(BACKEND_DIR), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _json_of(text: str) -> dict:
    decoder = json.JSONDecoder()
    cursor = text.find("{")
    while cursor != -1:
        try:
            obj, _end = decoder.raw_decode(text[cursor:])
            return obj
        except json.JSONDecodeError:
            cursor = text.find("{", cursor + 1)
    raise AssertionError("stdout 里没有可解析的 JSON：" + text[-600:])


def _kbs(db: Path) -> list[dict]:
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT id, name, kind, path_or_url, collection, status FROM knowledge_bases ORDER BY id"
        ).fetchall()
        return [
            {"id": int(r[0]), "name": str(r[1]), "kind": str(r[2]), "path_or_url": str(r[3]),
             "collection": str(r[4]), "status": str(r[5])}
            for r in rows
        ]
    finally:
        conn.close()


def _kb_ids(db: Path) -> list[int]:
    return [row["id"] for row in _kbs(db)]


def _ref_counts(db: Path) -> dict:
    out: dict = {}
    conn = sqlite3.connect(str(db))
    try:
        for table in REF_TABLES:
            out[table] = {
                int(k): int(c)
                for k, c in conn.execute(
                    "SELECT kb_id, COUNT(*) FROM " + table + " GROUP BY kb_id ORDER BY kb_id"
                )
            }
    finally:
        conn.close()
    return out


def _columns(db: Path) -> list[str]:
    conn = sqlite3.connect(str(db))
    try:
        return [str(r[1]) for r in conn.execute("PRAGMA table_info(knowledge_bases)")]
    finally:
        conn.close()


def _seed_compatible_duplicates(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        for row in (
            (4, "A4", "imported", PATH_A, "c1", "ready"),
            (7, "A7", "imported", "  " + PATH_A + "  ", "c1", "ready"),
            (9, "A9", "imported", PATH_A, "c1", "ready"),
            (11, "B11", "imported", PATH_B, "c1", "ready"),
            (12, "B12", "imported", PATH_B, "c1", "ready"),
        ):
            conn.execute("INSERT INTO knowledge_bases " + KB_COLUMNS + " " + KB_VALUES, row)
        conn.execute(NOTE_SQL, (101, "n101", "x", "c", 4, None))
        conn.execute(NOTE_SQL, (102, "n102", "x", "c", 7, "2026-01-02 03:04:05"))
        conn.execute(NOTE_SQL, (103, "n103", "x", "c", 9, None))
        conn.execute(NOTE_SQL, (104, "n104", "x", "c", 12, None))
        conn.execute(DOC_SQL, (201, "d201", "txt", "ready", "done", 4, "c", None))
        conn.execute(DOC_SQL, (202, "d202", "txt", "ready", "done", 7, "c", "2026-01-02 03:04:05"))
        conn.execute(DOC_SQL, (203, "d203", "txt", "ready", "done", 11, "c", None))
        conn.execute(WIKI_SQL, (301, "note", 101, 9, "t301"))
        conn.execute(WIKI_SQL, (302, "note", 104, 12, "t302"))
        conn.commit()
    finally:
        conn.close()


def test_preflight_json_is_machine_readable_and_read_only(tmp_path: Path) -> None:
    db = tmp_path / "preflight.sqlite"
    out = tmp_path / "materials"
    _alembic_0006(db)
    _seed_compatible_duplicates(db)
    before_ids = _kb_ids(db)
    before_refs = _ref_counts(db)
    before_cols = _columns(db)

    proc = _run_tool("--preflight", "--dsn", _dsn(db), "--out", str(out), "--json")
    assert proc.returncode == 0, (proc.stdout or "")[-500:] + (proc.stderr or "")[-800:]
    report = _json_of(proc.stdout)
    assert report["mode"] == "preflight"
    assert report["incompatible"] == []
    groups = {int(g["kept_id"]): [int(x) for x in g["merged_ids"]] for g in report["groups"]}
    assert groups == {4: [7, 9], 11: [12]}, str(report["groups"])
    assert report["ref_totals"]["notes"] == 4
    assert report["ref_totals"]["documents"] == 3
    assert report["ref_totals"]["wiki_links"] == 2
    # 只读：行 / 引用 / 列一个都没动，也没建 key 列与唯一索引
    assert _kb_ids(db) == before_ids
    assert _ref_counts(db) == before_refs
    assert _columns(db) == before_cols
    assert "path_or_url_key" not in before_cols
    assert list(out.glob("*preflight*")), "预检报告没有落盘"
    assert not list(out.glob("before-image-*.json")), "预检不许产出 before-image"


def test_apply_fails_closed_on_incompatible_then_merges_and_writes_materials(tmp_path: Path) -> None:
    db = tmp_path / "apply.sqlite"
    out = tmp_path / "materials"
    _alembic_0006(db)
    _seed_compatible_duplicates(db)
    conn = sqlite3.connect(str(db))
    try:
        # 同一 path 的三行里混进一个 collection 不同的行 ⇒ 整组不许自动合并
        conn.execute("INSERT INTO knowledge_bases " + KB_COLUMNS + " " + KB_VALUES, (13, "C13", "imported", PATH_B, "c2", "ready"))
        conn.commit()
    finally:
        conn.close()
    before_ids = _kb_ids(db)

    blocked = _run_tool("--apply", "--dsn", _dsn(db), "--out", str(out), "--json")
    assert blocked.returncode == 2, "不兼容组必须整体中止（rc=2），实际 " + str(blocked.returncode)
    payload = _json_of(blocked.stdout)
    assert payload["error"] == "incompatible_groups"
    assert "无法自动合并" in payload["message"]
    assert "collection" in payload["message"]
    assert _kb_ids(db) == before_ids, "中止路径必须零写入"
    assert "path_or_url_key" not in _columns(db), "中止路径连列都不该建"
    assert not list(out.glob("before-image-*.json"))
    assert not list(out.glob("redirects-*.json"))

    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM knowledge_bases WHERE id = 13")
        conn.commit()
    finally:
        conn.close()

    proc = _run_tool("--apply", "--dsn", _dsn(db), "--out", str(out), "--json")
    assert proc.returncode == 0, (proc.stdout or "")[-500:] + (proc.stderr or "")[-800:]
    report = _json_of(proc.stdout)
    assert report["mode"] == "apply"
    assert _kb_ids(db) == [1, 4, 11], "保留 MIN(id) 不成立：" + str(_kb_ids(db))
    refs = _ref_counts(db)
    assert refs["notes"] == {4: 3, 11: 1}, str(refs["notes"])
    assert refs["documents"] == {4: 2, 11: 1}, str(refs["documents"])
    assert refs["wiki_links"] == {4: 1, 11: 1}, str(refs["wiki_links"])

    images = sorted(out.rglob("before-image-*.json"))
    assert len(images) == 1, "before-image 数量不对：" + str(images)
    image = json.loads(images[0].read_text(encoding="utf-8"))
    assert image["schema"] == "kb-merge-before-image/v1"
    assert sorted(int(r["id"]) for g in image["groups"] for r in g["rows"]) == [4, 7, 9, 11, 12]
    assert {int(r["from"]): int(r["to"]) for r in image["redirects"]} == {7: 4, 9: 4, 12: 11}
    ledgers = sorted(out.rglob("redirects-*.json"))
    assert len(ledgers) == 1, "重定向账本数量不对：" + str(ledgers)
    ledger = json.loads(ledgers[0].read_text(encoding="utf-8"))
    assert {int(r["from"]): int(r["to"]) for r in ledger["redirects"]} == {7: 4, 9: 4, 12: 11}
    assert list(out.rglob("restore-*.sql")), "恢复 SQL 没有落盘"
    assert len(report["materials"]) == 3, str(report["materials"])


def test_restore_refuses_while_unique_index_exists_then_reverts(tmp_path: Path) -> None:
    from app.core.kb_identity import kb_path_key

    db = tmp_path / "restore.sqlite"
    out = tmp_path / "materials"
    _alembic_0006(db)
    _seed_compatible_duplicates(db)
    before_ids = _kb_ids(db)
    before_refs = _ref_counts(db)

    proc = _run_tool("--apply", "--dsn", _dsn(db), "--out", str(out), "--json")
    assert proc.returncode == 0, (proc.stderr or "")[-800:]
    assert _kb_ids(db) == [1, 4, 11]
    image = sorted(out.rglob("before-image-*.json"))[-1]

    # 把库推回 0007 之后的形态：key 列 + 唯一索引都在
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("ALTER TABLE knowledge_bases ADD COLUMN path_or_url_key VARCHAR(64)")
        for kid, path in conn.execute("SELECT id, path_or_url FROM knowledge_bases").fetchall():
            conn.execute(
                "UPDATE knowledge_bases SET path_or_url_key = ? WHERE id = ?",
                (kb_path_key(str(path)), int(kid)),
            )
        conn.execute(
            "CREATE UNIQUE INDEX uk_knowledge_bases_path_or_url_key"
            " ON knowledge_bases (path_or_url_key)"
        )
        conn.commit()
    finally:
        conn.close()

    refused = _run_tool("--restore", str(image), "--dsn", _dsn(db), "--json")
    assert refused.returncode != 0, "唯一索引还在时不许恢复"
    blob = (refused.stdout or "") + (refused.stderr or "")
    assert "REFUSED" in blob and "--drop-index" in blob, blob[-500:]
    assert _kb_ids(db) == [1, 4, 11], "拒绝路径必须零写入"

    ok = _run_tool("--restore", str(image), "--dsn", _dsn(db), "--drop-index", "--json")
    assert ok.returncode == 0, (ok.stdout or "")[-500:] + (ok.stderr or "")[-800:]
    result = _json_of(ok.stdout)
    assert sorted(int(x) for x in result["restored_ids"]) == [7, 9, 12], str(result["restored_ids"])
    assert _kb_ids(db) == before_ids, "恢复后 KB id 集合与改前不一致：" + str(_kb_ids(db))
    assert _ref_counts(db) == before_refs, "恢复后引用计数与改前不一致"


def _merger():
    spec = importlib.util.spec_from_file_location("s6_186_fix_merger", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_restore_target_identity_distinguishes_same_basename(tmp_path):
    import sqlalchemy as sa
    import pytest

    merger = _merger()
    a, b = tmp_path / "a" / "same.sqlite", tmp_path / "b" / "same.sqlite"
    a.parent.mkdir()
    b.parent.mkdir()
    _alembic_0006(a)
    _alembic_0006(b)
    _seed_compatible_duplicates(a)
    engine_a, engine_b = sa.create_engine(_dsn(a)), sa.create_engine(_dsn(b))
    try:
        bundle = merger.plan_merge(engine_a)
        bundle["report"].update(merger._target_label(engine_a))
        files = merger._write_materials(tmp_path / "materials", bundle)
        image = next(p for p in files if p.name.startswith("before-image-"))
        assert merger._target_label(engine_a) != merger._target_label(engine_b)
        with pytest.raises(SystemExit, match="目标|另一个库"):
            merger.run_restore(engine_b, image, False)
        with pytest.raises(SystemExit, match="目标|另一个库"):
            merger.run_restore(engine_b, image, False, force_target=True)
        assert _kb_ids(b) == [1]
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_transaction_aborts_if_material_plan_metadata_or_references_drift(tmp_path):
    import sqlalchemy as sa
    import pytest

    merger = _merger()
    db = tmp_path / "drift.sqlite"
    _alembic_0006(db)
    _seed_compatible_duplicates(db)
    engine = sa.create_engine(_dsn(db))
    try:
        plan = merger.plan_merge(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE knowledge_bases SET name='latest-name' WHERE id=7"))
            conn.execute(sa.text(
                "INSERT INTO notes (id,title,content,category,kb_id) VALUES(111,'new','x','',7)"
            ))
        before_ids, before_refs = _kb_ids(db), _ref_counts(db)
        with pytest.raises(RuntimeError, match="漂移|变化"):
            merger.apply_merge(engine, plan)
        assert _kb_ids(db) == before_ids and _ref_counts(db) == before_refs
    finally:
        engine.dispose()


def test_same_stamp_materials_are_never_overwritten(tmp_path, monkeypatch):
    import sqlalchemy as sa

    merger = _merger()
    db = tmp_path / "collision.sqlite"
    _alembic_0006(db)
    _seed_compatible_duplicates(db)
    engine = sa.create_engine(_dsn(db))
    monkeypatch.setattr(merger, "_now_stamp", lambda: "20261002T000000Z")
    try:
        first = merger.plan_merge(engine)
        first["report"].update(merger._target_label(engine))
        files = merger._write_materials(tmp_path / "materials", first)
        old_bytes = {p: p.read_bytes() for p in files}
        merger.apply_merge(engine, first)
        second = merger.plan_merge(engine)
        second["report"].update(merger._target_label(engine))
        merger._write_materials(tmp_path / "materials", second)
        assert all(p.read_bytes() == old_bytes[p] for p in files)
    finally:
        engine.dispose()


def test_restore_refuses_unbound_before_image_copy_before_any_write(tmp_path, capsys):
    import sqlalchemy as sa

    merger = _merger()
    db = tmp_path / "unbound.sqlite"
    _alembic_0006(db)
    _seed_compatible_duplicates(db)
    assert merger.main(["--dsn", _dsn(db), "--apply", "--out", str(tmp_path / "materials"),
                        "--json"]) == 0
    report = _json_of(capsys.readouterr().out)
    original = next(Path(p) for p in report["materials"] if Path(p).name.startswith("before-image-"))
    substitute = original.with_name("unbound-image.json")
    substitute.write_bytes(original.read_bytes())
    before_ids, before_refs = _kb_ids(db), _ref_counts(db)
    engine = sa.create_engine(_dsn(db))
    try:
        with pytest.raises(SystemExit, match="绑定"):
            merger.run_restore(engine, substitute, False)
        assert _kb_ids(db) == before_ids and _ref_counts(db) == before_refs
    finally:
        engine.dispose()


def test_restore_refuses_drop_index_at_head_0007_before_any_write(tmp_path, capsys):
    import sqlalchemy as sa

    merger = _merger()
    db = tmp_path / "restore-head.sqlite"
    _alembic_0006(db)
    _seed_compatible_duplicates(db)
    assert merger.main(["--dsn", _dsn(db), "--apply", "--out", str(tmp_path / "materials"),
                        "--json"]) == 0
    report = _json_of(capsys.readouterr().out)
    image = next(Path(p) for p in report["materials"] if Path(p).name.startswith("before-image-"))
    connection = sqlite3.connect(db)
    connection.executescript(
        "ALTER TABLE knowledge_bases ADD COLUMN path_or_url_key VARCHAR(64);"
        "CREATE UNIQUE INDEX uk_knowledge_bases_path_or_url_key ON knowledge_bases(path_or_url_key);"
        "UPDATE alembic_version SET version_num='0007';"
    )
    connection.close()
    before_ids, before_refs = _kb_ids(db), _ref_counts(db)
    engine = sa.create_engine(_dsn(db))
    try:
        with pytest.raises(SystemExit, match="downgrade 0006"):
            merger.run_restore(engine, image, True)
        assert _kb_ids(db) == before_ids and _ref_counts(db) == before_refs
        with engine.connect() as conn:
            assert conn.execute(sa.text(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='uk_knowledge_bases_path_or_url_key'"
            )).scalar() == 1
    finally:
        engine.dispose()


def test_runtime_rollback_closes_abort_without_deleting_pending_and_allows_retry(tmp_path, monkeypatch, capsys):
    import sqlalchemy as sa
    from app.core.kb_identity import ensure_kb_maintenance_complete

    merger = _merger()
    db = tmp_path / "abort-retry.sqlite"
    _alembic_0006(db)
    _seed_compatible_duplicates(db)
    connection = sqlite3.connect(db)
    connection.execute("ALTER TABLE knowledge_bases ADD COLUMN path_or_url_key VARCHAR(64)")
    connection.commit()
    connection.close()
    before_ids, before_refs = _kb_ids(db), _ref_counts(db)
    write_keys = merger._write_keys
    monkeypatch.setattr(merger, "_write_keys", lambda *args: (_ for _ in ()).throw(RuntimeError("rollback probe")))
    assert merger.main(["--dsn", _dsn(db), "--apply", "--out", str(tmp_path / "materials"), "--json"]) == 2
    capsys.readouterr()
    assert _kb_ids(db) == before_ids and _ref_counts(db) == before_refs
    pendings = list((tmp_path / "maintenance-gates").glob("*.pending.json"))
    assert len(pendings) == 1, "中止留痕不能删除"
    completion = json.loads(pendings[0].with_name(pendings[0].name.replace(".pending.", ".complete.")).read_text("utf-8"))
    assert completion["outcome"] == "aborted" and completion["receipts"] == {}
    sql = json.loads(Path(completion["sql_receipt"]["path"]).read_text("utf-8"))
    state = json.loads(Path(sql["abort_state"]["path"]).read_text("utf-8"))
    assert state["before"] == state["after"]
    assert len(state["before"]["knowledge_bases"]) == len(before_ids)
    ensure_kb_maintenance_complete(_dsn(db))
    monkeypatch.setattr(merger, "_write_keys", write_keys)
    assert merger.main(["--dsn", _dsn(db), "--apply", "--out", str(tmp_path / "materials"), "--json"]) == 0
    assert _kb_ids(db) == [1, 4, 11]
    assert len(list((tmp_path / "maintenance-gates").glob("*.pending.json"))) == 2


def test_external_drift_zero_write_abort_uses_current_state_and_unknown_db_failure_stays_pending(tmp_path, monkeypatch, capsys):
    import sqlalchemy as sa
    from app.core.kb_identity import KnowledgeBaseMaintenanceRequired, ensure_kb_maintenance_complete

    merger = _merger()
    db = tmp_path / "abort-drift.sqlite"
    _alembic_0006(db)
    _seed_compatible_duplicates(db)
    original_begin = merger._begin_pending

    def drift(engine, bundle, files):
        original_begin(engine, bundle, files)
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE knowledge_bases SET name='latest-current-state' WHERE id=7"))

    monkeypatch.setattr(merger, "_begin_pending", drift)
    assert merger.main(["--dsn", _dsn(db), "--apply", "--out", str(tmp_path / "materials"), "--json"]) == 2
    capsys.readouterr()
    ensure_kb_maintenance_complete(_dsn(db))
    state_file = next((tmp_path / "materials").rglob("sql-abort-state.json"))
    state = json.loads(state_file.read_text("utf-8"))
    assert next(row["name"] for row in state["before"]["knowledge_bases"] if row["id"] == 7) == "latest-current-state"
    assert state["before"] == state["after"]
    monkeypatch.setattr(merger, "_begin_pending", original_begin)
    original_apply = merger.apply_merge

    def unknown(engine, bundle):
        raise sa.exc.OperationalError("probe", {}, RuntimeError("unknown transaction outcome"))

    monkeypatch.setattr(merger, "apply_merge", unknown)
    with pytest.raises(sa.exc.OperationalError):
        merger.main(["--dsn", _dsn(db), "--apply", "--out", str(tmp_path / "materials"), "--json"])
    with pytest.raises(KnowledgeBaseMaintenanceRequired):
        ensure_kb_maintenance_complete(_dsn(db))
    assert len(list((tmp_path / "materials").rglob("sql-aborted.json"))) == 1
    monkeypatch.setattr(merger, "apply_merge", original_apply)


@pytest.mark.parametrize("damage", ["deleted_reference", "changed_reference", "missing_table"])
def test_restore_refuses_incomplete_reference_scope_without_sql_receipt_or_partial_write(tmp_path, capsys, damage):
    import sqlalchemy as sa

    merger = _merger()
    db = tmp_path / "restore-reference.sqlite"
    _alembic_0006(db)
    _seed_compatible_duplicates(db)
    assert merger.main(["--dsn", _dsn(db), "--apply", "--out", str(tmp_path / "materials"), "--json"]) == 0
    report = _json_of(capsys.readouterr().out)
    image = next(Path(p) for p in report["materials"] if Path(p).name.startswith("before-image-"))
    engine = sa.create_engine(_dsn(db))
    try:
        with engine.begin() as conn:
            if damage == "deleted_reference":
                conn.execute(sa.text("DELETE FROM notes WHERE id=102"))
            elif damage == "changed_reference":
                conn.execute(sa.text("UPDATE notes SET kb_id=11 WHERE id=102"))
            else:
                conn.execute(sa.text("DROP TABLE notes"))
        with engine.connect() as conn:
            before = merger._sql_state(conn)
        with pytest.raises(RuntimeError, match="引用|缺失|漂移"):
            merger.run_restore(engine, image, False)
        with engine.connect() as conn:
            assert merger._sql_state(conn) == before
        assert not list(image.parent.glob("sql-restored-*.json"))
    finally:
        engine.dispose()
