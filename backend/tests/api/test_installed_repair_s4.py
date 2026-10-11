"""S4 response clocks and runtime identity; isolated, no external services."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.api as api
from app.api.document_routes import _to_out
from app.api.note_routes import NoteOut
from app.api.settings_schemas import data_payload
from app.api.trash_routes import _chat_item, _document_item, _note_item
from app.core.config import settings
import app.core.config as config_mod
from app.core import env_store


def note_at(value):
    return NoteOut(id=1, title="clock", content="body", category="",
                   created_at=value, updated_at=value)


def document_at(value):
    return SimpleNamespace(id=1, filename="clock.txt", file_type="txt",
                           status="ready", chunk_count=0, vectorized=False,
                           vector_error=None, category="", tags=None,
                           created_at=value, kb_id=1)


@pytest.mark.parametrize("backend", ["sqlite", "mysql"])
@pytest.mark.parametrize("kind", ["note", "document"])
def test_database_response_clock(monkeypatch, backend, kind):
    monkeypatch.setattr(settings, "DB_BACKEND", backend)
    value = datetime(2026, 10, 8, 23, 30)
    result = (note_at(value) if kind == "note" else _to_out(document_at(value), ""))
    encoded = result.model_dump(mode="json")["created_at"]
    actual = datetime.fromisoformat(encoded.replace("Z", "+00:00"))
    expected = value.replace(tzinfo=timezone.utc) if backend == "sqlite" else value.astimezone()
    assert actual.tzinfo is not None
    assert actual.timestamp() == expected.timestamp()
    if kind == "note":
        assert result.model_dump(mode="json")["updated_at"] == encoded
    assert value.tzinfo is None  # output conversion must not change stored values


@pytest.mark.parametrize("backend", ["sqlite", "mysql"])
@pytest.mark.parametrize("offset", [0, 8, -5])
def test_explicit_offset_is_preserved(monkeypatch, backend, offset):
    monkeypatch.setattr(settings, "DB_BACKEND", backend)
    value = datetime(2026, 10, 9, 0, 30, tzinfo=timezone(timedelta(hours=offset)))
    for result in (note_at(value), _to_out(document_at(value), "")):
        encoded = result.model_dump(mode="json")["created_at"]
        assert datetime.fromisoformat(encoded.replace("Z", "+00:00")) == value


@pytest.mark.parametrize("backend", ["sqlite", "mysql"])
@pytest.mark.parametrize("project", [_note_item, _document_item, _chat_item])
def test_soft_delete_response_is_utc(monkeypatch, backend, project):
    monkeypatch.setattr(settings, "DB_BACKEND", backend)
    value = datetime(2026, 10, 8, 23, 30)
    row = SimpleNamespace(id=1, title="clock", filename="clock", category="", deleted_at=value)
    assert project(row)["deleted_at"] == value.replace(tzinfo=timezone.utc)
    assert row.deleted_at is value


@pytest.mark.parametrize("backend", ["sqlite", "mysql"])
@pytest.mark.asyncio
async def test_health_implementation_identity(monkeypatch, backend):
    monkeypatch.setattr(settings, "DB_BACKEND", backend)
    result = await api.health()
    assert result["database_backend"] == backend
    assert result["redis_backend"] == "in_process"
    assert result["redis"] == "ok"


@pytest.mark.parametrize("configured,expected", [
    ("vectors/current", "relative"), ('"vectors/quoted"', "quoted"),
    ("", "remote"), ("absolute", "absolute"),
])
def test_data_uses_actual_root_without_creating(monkeypatch, tmp_path, configured, expected):
    root = tmp_path / "non-default installation"
    monkeypatch.setattr(config_mod, "ROOT_DIR", root)
    absolute = tmp_path / "absolute-vectors"
    raw = str(absolute) if expected == "absolute" else configured
    monkeypatch.setattr(settings, "QDRANT_PATH", raw)
    monkeypatch.setattr(settings, "QDRANT_URL", "http://vector.invalid:6333")
    result = data_payload()
    wanted = {"relative": root / "vectors/current", "quoted": root / "vectors/quoted",
              "absolute": absolute}.get(expected)
    assert result["chroma_dir"] == (raw.strip() or "http://vector.invalid:6333")
    assert result["chroma_path"] == (str(wanted) if wanted else "http://vector.invalid:6333")
    assert result["exists"] is False
    assert set(result) == {"chroma_dir", "chroma_path", "exists", "files", "size_bytes", "vectors", "notice"}
    assert not root.exists()
    assert not absolute.exists()


def test_data_exists_checks_same_resolved_path(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "ROOT_DIR", tmp_path)
    (tmp_path / "vectors").mkdir()
    monkeypatch.setattr(settings, "QDRANT_PATH", "vectors")
    assert data_payload()["exists"] is True


def test_non_default_frozen_installation_root(monkeypatch, tmp_path):
    import sys
    root = tmp_path / "Custom Install"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(root / "zhilan.exe"))
    monkeypatch.setattr(config_mod, "ROOT_DIR", env_store.resolve_data_root())
    monkeypatch.setattr(settings, "QDRANT_PATH", "data/qdrant")
    assert data_payload()["chroma_path"] == str(root / "data/qdrant")
    assert not root.exists()


@pytest.mark.parametrize("offset", [0, 8, -5])
def test_trash_keeps_existing_offsets_and_empty(offset):
    from app.api.response_time import response_time
    value = datetime(2026, 10, 9, 0, 30, tzinfo=timezone(timedelta(hours=offset)))
    row = SimpleNamespace(id=1, title="clock", filename="clock", category="", deleted_at=value)
    for project in (_note_item, _document_item, _chat_item):
        assert project(row)["deleted_at"] is value
    assert response_time(None) is None


@pytest.mark.asyncio
async def test_sqlite_current_timestamp_and_api_response(db, monkeypatch):
    from sqlalchemy import text
    monkeypatch.setattr(settings, "DB_BACKEND", "sqlite")
    value = (await db.execute(text("SELECT CURRENT_TIMESTAMP"))).scalar_one()
    naive = datetime.fromisoformat(value)
    actual = datetime.fromisoformat(note_at(naive).model_dump(mode="json")["created_at"].replace("Z", "+00:00"))
    assert abs((actual - datetime.now(timezone.utc)).total_seconds()) < 5
