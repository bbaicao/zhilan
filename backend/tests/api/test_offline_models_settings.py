"""设置导入：小型可信模型夹具验证真实 ZIP/哈希/不覆盖及本机入口。"""

import hashlib
import stat
import threading
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api import offline_model_routes as routes
from app.system import offline_models as models


@pytest.fixture
def bundle(tmp_path):
    contents = {"OCR/one.onnx": b"one", "Table/two.onnx": b"two"}
    manifest = {
        name: (hashlib.sha256(content).hexdigest(), len(content))
        for name, content in contents.items()
    }
    path = tmp_path / "models.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in contents.items():
            archive.writestr(models.MODEL_REPO + "/" + name, content)
    root = tmp_path / "home" / "models" / models.MODEL_REPO
    return models.OfflineModelImporter(manifest=manifest, root=root), path, root, contents


def test_import_and_repeat_preserve_files(bundle):
    service, path, root, contents = bundle
    service._run(path, root)
    assert service.snapshot()["state"] == "succeeded"
    assert service.snapshot()["imported_files"] == 2
    before = {name: (root / name).stat().st_mtime_ns for name in contents}
    service._run(path, root)
    status = service.snapshot()
    assert status["state"] == "succeeded"
    assert status["skipped_files"] == 2 and status["imported_files"] == 0
    assert status["present_files"] == 2
    assert before == {name: (root / name).stat().st_mtime_ns for name in contents}
    assert all((root / name).read_bytes() == value for name, value in contents.items())


def test_last_existing_conflict_stops_before_any_write(bundle):
    service, path, root, _ = bundle
    existing = root / "Table/two.onnx"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"bad")
    service._run(path, root)
    assert service.snapshot()["state"] == "failed"
    assert not (root / "OCR/one.onnx").exists()
    assert existing.read_bytes() == b"bad"


@pytest.mark.parametrize("name", [
    "../escape.onnx",
    models.MODEL_REPO + "/../escape.onnx",
    models.MODEL_REPO + "/OCR\\escape.onnx",
    models.MODEL_REPO + "/extra.onnx",
    models.MODEL_REPO + "/OCR/one.onnx",
])
def test_bad_zip_structure_never_writes(bundle, name):
    service, path, root, _ = bundle
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(name, b"bad")
    service._run(path, root)
    assert service.snapshot()["state"] == "failed"
    assert not root.exists()


def test_zip_symlink_rejected(bundle):
    service, path, root, _ = bundle
    with zipfile.ZipFile(path, "a") as archive:
        link = zipfile.ZipInfo(models.MODEL_REPO + "/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../escape")
    service._run(path, root)
    assert service.snapshot()["state"] == "failed"
    assert not root.exists()


def test_wrong_model_content_fails_and_preserves_scene(bundle):
    service, path, root, contents = bundle
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in contents.items():
            archive.writestr(models.MODEL_REPO + "/" + name, b"bad" if "one" in name else content)
    service._run(path, root)
    assert service.snapshot()["state"] == "failed"
    assert "校验失败" in service.snapshot()["detail"]
    assert (root / "OCR/one.onnx").read_bytes() == b"bad"
    assert not (root / "Table/two.onnx").exists()


def test_destination_junction_is_rejected(bundle, monkeypatch):
    service, path, root, _ = bundle
    root.parent.mkdir(parents=True)
    original = models.Path.lstat
    junction = root.parent

    class JunctionInfo:
        st_mode = stat.S_IFDIR
        st_file_attributes = 0x400

    def lstat(target, *args, **kwargs):
        return JunctionInfo() if target == junction else original(target, *args, **kwargs)

    monkeypatch.setattr(models.Path, "lstat", lstat)
    service._run(path, root)
    assert service._status["state"] == "failed"
    assert not root.exists()


def _client(service, monkeypatch, host="127.0.0.1"):
    monkeypatch.setattr(routes, "importer", service)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    return TestClient(app, base_url="http://127.0.0.1:8123", client=(host, 40000))


def test_async_start_and_busy_409(bundle, monkeypatch):
    service, path, _, _ = bundle
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = service._run

    def worker(*args):
        entered.set()
        release.wait(timeout=5)
        try:
            original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(service, "_run", worker)
    client = _client(service, monkeypatch)
    try:
        first = client.post("/api/settings/offline-models/import", json={"path": str(path)})
        assert first.status_code == 202 and first.json()["state"] == "running"
        assert entered.wait(timeout=2)
        second = client.post("/api/settings/offline-models/import", json={"path": str(path)})
        assert second.status_code == 409
    finally:
        release.set()
        assert finished.wait(timeout=5)
    assert service.snapshot()["state"] == "succeeded"


@pytest.mark.parametrize("headers,remote", [
    ({"Origin": "https://attacker.example"}, "127.0.0.1"),
    ({"Origin": "http://127.0.0.1:8124"}, "127.0.0.1"),
    ({"Origin": "null"}, "127.0.0.1"),
    ({"Host": "attacker.example:8123"}, "127.0.0.1"),
    ({}, "192.0.2.1"),
])
def test_nonlocal_request_cannot_read_pick_or_import(bundle, monkeypatch, headers, remote):
    service, path, _, _ = bundle
    client = _client(service, monkeypatch, remote)
    assert client.get("/api/settings/offline-models", headers=headers).status_code == 403
    assert client.post("/api/system/pick-model-archive", json={}, headers=headers).status_code == 403
    assert client.post(
        "/api/settings/offline-models/import", json={"path": str(path)}, headers=headers
    ).status_code == 403


def test_local_origin_and_picker_cancel(bundle, monkeypatch):
    service, _, _, _ = bundle
    client = _client(service, monkeypatch)
    monkeypatch.setattr(routes, "_ask_model_archive", lambda _: "")
    headers = {"Origin": "http://127.0.0.1:8123"}
    assert client.get("/api/settings/offline-models", headers=headers).status_code == 200
    assert client.post("/api/system/pick-model-archive", json={}, headers=headers).json() == {
        "available": True, "path": "", "detail": "",
    }


def test_invalid_archive_path_is_400(bundle, monkeypatch):
    service, _, _, _ = bundle
    client = _client(service, monkeypatch)
    assert client.post("/api/settings/offline-models/import", json={"path": "../models.zip"}).status_code == 400
    assert service.snapshot()["state"] == "idle"


def test_unavailable_picker_returns_manual_path_hint(bundle, monkeypatch):
    service, _, _, _ = bundle
    client = _client(service, monkeypatch)

    def unavailable(_):
        raise routes.PickerUnavailable("本机没有文件选择窗口，请手动填写路径。")

    monkeypatch.setattr(routes, "_ask_model_archive", unavailable)
    result = client.post("/api/system/pick-model-archive", json={})
    assert result.status_code == 200
    assert result.json()["available"] is False and result.json()["path"] == ""
    assert "手动" in result.json()["detail"]


def test_model_root_follows_ocr_home(bundle, monkeypatch):
    _, _, root, _ = bundle
    data_root = root.parents[2]
    monkeypatch.chdir(data_root)
    monkeypatch.setattr(models.settings, "MINERU_MODEL_DIR", "relative-home")
    monkeypatch.delenv("MINERU_HOME", raising=False)
    assert models.model_root() == data_root / "relative-home" / "models" / models.MODEL_REPO
    monkeypatch.setenv("MINERU_HOME", str(data_root / "env-home"))
    assert models.model_root() == data_root / "env-home" / "models" / models.MODEL_REPO


def test_packaged_manifest_has_exact_official_identity():
    manifest = models.load_manifest()
    assert len(manifest) == 13
    assert sum(size for _, size in manifest.values()) == 858204914


def test_relative_environment_home_matches_ocr_working_directory(monkeypatch, tmp_path):
    working = tmp_path / "backend"
    working.mkdir()
    monkeypatch.chdir(working)
    monkeypatch.setenv("MINERU_HOME", "custom-home")
    assert models.model_root() == working / "custom-home" / "models" / models.MODEL_REPO
