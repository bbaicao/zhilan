"""Code uploads preserve bytes, encoding, and the early request-body release."""
import hashlib
import inspect

import pytest

from app.core.config import settings
from app.pipeline import ingest
from app.pipeline.parsing import DocumentParseError
from tests.api.test_chat_contract import client  # noqa: F401

SUFFIXES = (
    "py", "js", "ts", "java", "go", "rs", "c", "h", "cpp", "cs", "rb", "php",
    "sh", "sql", "kt", "swift", "json", "yaml", "yml", "toml", "ini", "cfg",
    "xml", "jsx", "tsx", "vue", "css", "scss", "html",
)
TEXT = "def sample():\n    return 42\n"


@pytest.mark.parametrize("suffix", SUFFIXES)
def test_prepare_preserves_every_code_suffix(suffix):
    payload = TEXT.encode("utf-8")
    prepared = ingest.prepare_document("control." + suffix, payload)
    assert prepared.raw == prepared.text == TEXT
    assert prepared.source_encoding == "utf-8"
    assert prepared.source_sha256 == hashlib.sha256(payload).hexdigest()
    assert prepared.pieces and prepared.pieces[0].line_start == 1


@pytest.mark.parametrize("payload,encoding", [
    (b"\xef\xbb\xbf" + TEXT.encode("utf-8"), "utf-8"),
    (b"\xff\xfe" + TEXT.encode("utf-16-le"), "utf-16-le"),
])
def test_prepare_preserves_bom_encoding_and_source_identity(payload, encoding):
    prepared = ingest.prepare_document("control.toml", payload)
    assert prepared.raw == prepared.text == TEXT
    assert prepared.source_encoding == encoding
    assert prepared.source_sha256 == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("suffix", ["py", "txt"])
def test_request_bytes_are_released_before_cleaning(monkeypatch, suffix):
    original = ingest.clean
    calls = []

    def check_release(*args, **kwargs):
        frame = inspect.currentframe().f_back
        try:
            calls.append("data" not in frame.f_locals)
        finally:
            del frame
        return original(*args, **kwargs)

    monkeypatch.setattr(ingest, "clean", check_release)
    ingest.prepare_document("control." + suffix, TEXT.encode("utf-8"))
    assert calls == [True]


def test_binary_code_remains_rejected():
    with pytest.raises(DocumentParseError):
        ingest.prepare_document("control.py", b"PK\x03\x04binary")


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["clean-preview", "upload"])
async def test_code_upload_and_preview_use_shared_preparation(client, monkeypatch, tmp_path, endpoint):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    response = await client.post(
        "/api/documents/" + endpoint,
        files={"file": ("control.py", TEXT.encode("utf-8"), "text/plain")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filename"] == "control.py" and body["chunk_count"] > 0
    if endpoint == "clean-preview":
        assert body["raw_content"] == body["clean_content"] == TEXT
    else:
        report = await client.get(f"/api/documents/{body['id']}/clean-report")
        assert report.status_code == 200
        assert report.json()["raw_content"] == TEXT
