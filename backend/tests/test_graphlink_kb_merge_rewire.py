"""「导入资料自动连线」生命周期判据：kb 合并必须把新表一起改向（且迁移 head = 0011 单头）。

为什么单独钉这条：`document_similarity_edges.kb_id` 决定 similar 边画进哪个库。
`kb_merge_duplicates.py` 的 `REF_TABLES` 不带上它，合并后这些边会挂在**已被删掉**的库上
—— 图谱里不再出现（源/目标库不符），行却永久悬挂，且恢复材料也说不清它们原本属于谁。
本文件用**真 alembic head** 造库、**真 CLI** 走预检与合并，逐条核对：
① head 库确实只有 `0011` 这一个版本（单头）；② 预检报告把新表算进引用表与引用计数；
③ 合并后边行的 `kb_id` 指到保留行、旧行已删。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND_DIR / "scripts" / "kb_merge_duplicates.py"
PATH_A = "D:/gl/kb/dup"
KB_COLUMNS = "(id, name, kind, path_or_url, collection, read_only, status, last_sync_at)"
KB_VALUES = "VALUES (?, ?, ?, ?, ?, 0, ?, NULL)"
DOC_SQL = (
    "INSERT INTO documents (id, filename, file_type, status, index_status, chunk_count,"
    " vectorized, kb_id, category, deleted_at) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?)"
)
EDGE_SQL = (
    "INSERT INTO document_similarity_edges (id, kb_id, source_document_id, target_document_id,"
    " score, space_id, dim) VALUES (?, ?, ?, ?, ?, ?, ?)"
)


@pytest.fixture(autouse=True)
def isolated_maintenance(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHILAN_KB_MERGE_DIR", str(tmp_path / "maintenance-gates"))


def _dsn(db: Path) -> str:
    return "sqlite:///" + db.as_posix()


def _alembic_head(db: Path) -> None:
    env = dict(os.environ)
    env["DB_BACKEND"] = "sqlite"
    env["SQLITE_PATH"] = str(db)
    env["PYTHONPATH"] = str(BACKEND_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("ZHILAN_KB_MERGE_DIR", None)
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, "alembic upgrade head 失败：" + (proc.stderr or "")[-800:]


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


def _connect(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db))


def _seed_duplicate_kbs_with_similar_edge(db: Path) -> None:
    conn = _connect(db)
    try:
        conn.execute("INSERT INTO knowledge_bases " + KB_COLUMNS + " " + KB_VALUES,
                     (4, "A4", "imported", PATH_A, "c1", "ready"))
        conn.execute("INSERT INTO knowledge_bases " + KB_COLUMNS + " " + KB_VALUES,
                     (9, "A9", "imported", PATH_A, "c1", "ready"))
        conn.execute(DOC_SQL, (201, "d201", "txt", "ready", "done", 4, "c", None))
        conn.execute(DOC_SQL, (202, "d202", "txt", "ready", "done", 9, "c", None))
        conn.execute(EDGE_SQL, (301, 9, 201, 202, 0.91, "sp-a", 2))
        conn.commit()
    finally:
        conn.close()


def test_migration_head_is_0011_single_head(tmp_path: Path) -> None:
    db = tmp_path / "head.sqlite"
    _alembic_head(db)
    conn = _connect(db)
    try:
        versions = [str(row[0]) for row in conn.execute("SELECT version_num FROM alembic_version")]
        tables = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        index_names = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='document_similarity_edges'"
        )}
    finally:
        conn.close()
    assert versions == ["0011"], "迁移链必须只有一个 head（本次新增 0011）"
    assert "document_similarity_edges" in tables
    assert "ix_document_similarity_edges_source" in index_names
    assert "ix_document_similarity_edges_target" in index_names
    # 唯一约束在 SQLite 里是内联 autoindex（名字查不到）⇒ 用行为验证：(kb, source, target) 不许重复
    conn = _connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(EDGE_SQL, (302, 1, 7, 8, 0.5, "sp-a", 2))
            conn.execute(EDGE_SQL, (303, 1, 7, 8, 0.5, "sp-a", 2))
        conn.rollback()
    finally:
        conn.close()


def test_kb_merge_rewires_similar_edges(tmp_path: Path) -> None:
    db = tmp_path / "rewire.sqlite"
    out = tmp_path / "materials"
    _alembic_head(db)
    _seed_duplicate_kbs_with_similar_edge(db)

    preflight = _run_tool("--preflight", "--dsn", _dsn(db), "--out", str(out), "--json")
    assert preflight.returncode == 0, (preflight.stdout or "")[-500:] + (preflight.stderr or "")[-800:]
    report = _json_of(preflight.stdout)
    assert "document_similarity_edges" in report["ref_tables"], "预检必须把新表算作引用表"
    assert report["ref_totals"]["document_similarity_edges"] == 1
    groups = {int(group["kept_id"]): [int(x) for x in group["merged_ids"]] for group in report["groups"]}
    assert groups == {4: [9]}, str(report["groups"])

    applied = _run_tool("--apply", "--dsn", _dsn(db), "--out", str(out), "--json")
    assert applied.returncode == 0, (applied.stdout or "")[-800:] + (applied.stderr or "")[-1200:]

    conn = _connect(db)
    try:
        kb_ids = [int(row[0]) for row in conn.execute("SELECT id FROM knowledge_bases ORDER BY id")]
        edge_kbs = [int(row[0]) for row in conn.execute(
            "SELECT kb_id FROM document_similarity_edges ORDER BY id"
        )]
        edge_rows = [tuple(int(value) for value in row) for row in conn.execute(
            "SELECT id, source_document_id, target_document_id FROM document_similarity_edges"
        )]
    finally:
        conn.close()
    assert 9 not in kb_ids and 4 in kb_ids, "重复的 9 已被并进 4（1 是迁移自带的库）"
    assert edge_kbs == [4], "相似边必须跟着改向到保留库 —— 否则它们永远画不出来"
    assert edge_rows == [(301, 201, 202)], "改向不许改 id / 不许动端点"
