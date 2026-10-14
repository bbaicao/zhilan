"""重建脚本 `scripts/rebuild_similarity_edges.py` 的入口判据。

只覆盖下游函数覆盖不到的三件事：①DSN 翻译与 --kb 解析；②关态拒绝（退出码 2）与
「有库没写成」的退出码 1；③真实端到端（真 SQLite 库 + 真本地 Qdrant）——试算一行不写、
真写落一行。边算法本身由 tests/pipeline/test_graphlink_similar_edges.py 覆盖，这里不重复。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models import Document, IndexBuild
from app.pipeline import graphlink
from app.vector import VectorPoint, get_vector_store
from app.vector.identity import point_id

BACKEND_DIR = Path(__file__).resolve().parents[1]
SCRIPT = BACKEND_DIR / "scripts" / "rebuild_similarity_edges.py"
#: 本地 Qdrant 的 for_space() 只接受 64 位小写 hex（见 QdrantVectorStore.for_space）。
SPACE_A = "a" * 64


def _load_script_module():
    spec = importlib.util.spec_from_file_location("graphlink_rebuild_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script_module():
    return _load_script_module()


def _script_env(db_path: Path, qdrant_dir: Path, *, enabled: bool = True) -> dict[str, str]:
    return {
        **os.environ,
        "DB_BACKEND": "sqlite",
        "SQLITE_PATH": str(db_path),
        "QDRANT_PATH": str(qdrant_dir),
        "GRAPHLINK_ENABLE": "true" if enabled else "false",
        "GRAPHLINK_TOP_K": "3",
        "GRAPHLINK_MIN_SCORE": "0.65",
        "GRAPHLINK_MAX_DEGREE": "3",
        "PYTHONPATH": str(BACKEND_DIR),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _run_script(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _edge_rows(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT kb_id, source_document_id, target_document_id, score, space_id, dim "
            "FROM document_similarity_edges ORDER BY source_document_id, target_document_id"
        ).fetchall()
    finally:
        conn.close()


async def _seed_two_documents(db_path: Path) -> tuple[list[int], list[str]]:
    engine = create_async_engine("sqlite+aiosqlite:///" + db_path.as_posix(), future=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    doc_ids: list[int] = []
    point_ids: list[str] = []
    try:
        async with maker() as session:
            for index, name in enumerate(("left.txt", "right.txt"), start=1):
                build_id = "build-%d" % index
                pid = point_id(build_id, 1)
                doc = Document(
                    filename=name,
                    file_type="txt",
                    status="ready",
                    index_status="ready",
                    kb_id=1,
                )
                session.add(doc)
                await session.flush()
                session.add(
                    IndexBuild(
                        id=build_id,
                        document_id=doc.id,
                        state="published",
                        fingerprint={},
                        space_id=SPACE_A,
                        point_ids=[pid],
                        vectorized=True,
                    )
                )
                await session.flush()
                doc.active_build_id = build_id
                doc.content_build_id = build_id
                doc_ids.append(doc.id)
                point_ids.append(pid)
            await session.commit()
    finally:
        await engine.dispose()
    return doc_ids, point_ids


def _seed_qdrant(qdrant_dir: Path, point_ids: list[str]) -> None:
    store = get_vector_store(path=str(qdrant_dir))
    try:
        scoped = store.for_space(SPACE_A)
        scoped.ensure_collection(2)
        scoped.upsert(
            [
                VectorPoint(id=pid, vector=[1.0, 0.0], payload={})
                for pid in point_ids
            ]
        )
    finally:
        store.close()


def test_to_async_dsn_translates_sync_drivers_and_keeps_async(script_module):
    assert script_module.to_async_dsn("sqlite:///E:/tmp/gl.db") == "sqlite+aiosqlite:///E:/tmp/gl.db"
    assert script_module.to_async_dsn("sqlite+aiosqlite:///E:/tmp/gl.db") == (
        "sqlite+aiosqlite:///E:/tmp/gl.db"
    )
    assert script_module.to_async_dsn("mysql+pymysql://u:changeme@h:3306/zhilan") == (
        "mysql+aiomysql://u:changeme@h:3306/zhilan"
    )
    assert script_module.to_async_dsn("mysql://u@h/zhilan") == "mysql+aiomysql://u@h/zhilan"
    assert script_module.to_async_dsn("  sqlite:///x.db  ") == "sqlite+aiosqlite:///x.db"


def test_parse_kbs_splits_dedupes_and_sorts(script_module):
    assert script_module.parse_kbs(["1,2", "5", "2"]) == [1, 2, 5]
    assert script_module.parse_kbs([" 3 , 1 "]) == [1, 3]
    assert script_module.parse_kbs([]) == []


def test_main_refuses_when_switch_disabled(script_module, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(settings, "GRAPHLINK_ENABLE", False, raising=False)
    db_path = tmp_path / "refused.db"
    code = script_module.main(["--dsn", "sqlite:///" + db_path.as_posix()])

    assert code == script_module.EXIT_REFUSED == 2
    assert "REFUSED" in capsys.readouterr().err
    assert not db_path.exists(), "关态必须在建库/建引擎之前就拒绝"


def test_main_rejects_non_integer_kb_before_touching_anything(
    script_module, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(settings, "GRAPHLINK_ENABLE", True, raising=False)
    db_path = tmp_path / "bad-kb.db"
    code = script_module.main(["--dsn", "sqlite:///" + db_path.as_posix(), "--kb", "x1"])

    assert code == script_module.EXIT_REFUSED
    assert "--kb 只接受整数" in capsys.readouterr().err
    assert not db_path.exists()


def test_main_dry_run_asks_graphlink_for_dry_run_and_returns_zero(
    script_module, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(settings, "GRAPHLINK_ENABLE", True, raising=False)
    seen: dict[str, object] = {}

    async def fake_known_kb_ids(session):
        seen["known_called"] = True
        return [1]

    async def fake_recompute(session, kb_id, *, store=None, dry_run=False):
        seen["kb_id"] = kb_id
        seen["dry_run"] = dry_run
        return {"status": "dry", "kb_id": kb_id, "edges": 4, "documents": 3, "missing": 0}

    monkeypatch.setattr(graphlink, "known_kb_ids", fake_known_kb_ids, raising=False)
    monkeypatch.setattr(graphlink, "recompute_kb_edges", fake_recompute, raising=False)
    db_path = tmp_path / "dry.db"
    code = script_module.main(
        ["--dsn", "sqlite:///" + db_path.as_posix(), "--dry-run", "--json"]
    )

    assert code == script_module.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["results"][0]["edges"] == 4
    assert seen == {"known_called": True, "kb_id": 1, "dry_run": True}


def test_main_explicit_kb_list_skips_discovery_and_flags_stale_as_failure(
    script_module, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(settings, "GRAPHLINK_ENABLE", True, raising=False)
    seen: dict[str, object] = {"kbs": []}

    async def fake_known_kb_ids(session):
        raise AssertionError("给了 --kb 就不该再列举库")

    async def fake_recompute(session, kb_id, *, store=None, dry_run=False):
        seen["kbs"].append(kb_id)
        if kb_id == 3:
            return {"status": "stale", "kb_id": kb_id, "edges": 0, "documents": 0, "missing": 0}
        return {"status": "written", "kb_id": kb_id, "edges": 2, "documents": 2, "missing": 0}

    monkeypatch.setattr(graphlink, "known_kb_ids", fake_known_kb_ids, raising=False)
    monkeypatch.setattr(graphlink, "recompute_kb_edges", fake_recompute, raising=False)
    db_path = tmp_path / "explicit.db"
    code = script_module.main(
        ["--dsn", "sqlite:///" + db_path.as_posix(), "--kb", "2,3"]
    )
    captured = capsys.readouterr()

    assert code == script_module.EXIT_FAIL == 1
    assert seen["kbs"] == [2, 3]
    assert "kb=3:stale" in captured.err
    assert "kb=2 status=written" in captured.out


def test_end_to_end_dry_run_then_write_and_then_refused(tmp_path):
    db_path = tmp_path / "graphlink.db"
    qdrant_dir = tmp_path / "qdrant"
    qdrant_dir.mkdir()
    env = _script_env(db_path, qdrant_dir)

    upgraded = subprocess.run(
        [sys.executable, "-B", "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert upgraded.returncode == 0, upgraded.stderr

    import asyncio

    doc_ids, point_ids = asyncio.run(_seed_two_documents(db_path))
    _seed_qdrant(qdrant_dir, point_ids)

    dry = _run_script(["--dsn", "sqlite:///" + db_path.as_posix(), "--kb", "1", "--dry-run", "--json"], env)
    assert dry.returncode == 0, dry.stderr
    dry_payload = json.loads(dry.stdout)
    assert dry_payload["results"][0]["status"] == "dry"
    assert dry_payload["results"][0]["edges"] == 1
    assert dry_payload["results"][0]["documents"] == 2
    assert _edge_rows(db_path) == [], "试算必须一行不写"

    written = _run_script(["--dsn", "sqlite:///" + db_path.as_posix(), "--kb", "1", "--json"], env)
    assert written.returncode == 0, written.stderr
    assert json.loads(written.stdout)["results"][0]["status"] == "written"
    rows = _edge_rows(db_path)
    assert [(row[0], row[1], row[2], row[4], row[5]) for row in rows] == [
        (1, doc_ids[0], doc_ids[1], SPACE_A, 2)
    ]
    assert rows[0][3] == pytest.approx(1.0, abs=1e-6)

    refused = _run_script(
        ["--dsn", "sqlite:///" + db_path.as_posix(), "--kb", "1"],
        _script_env(db_path, qdrant_dir, enabled=False),
    )
    assert refused.returncode == 2
    assert "REFUSED" in refused.stderr
    assert len(_edge_rows(db_path)) == 1, "关态拒绝不得改动已有边"
