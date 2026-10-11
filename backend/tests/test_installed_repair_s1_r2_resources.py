"""S1-R2: keep upload body lifetime bounded without changing content identity."""

import inspect

import pytest

from app.api import document_routes
from app.pipeline import ingest


class FakeUpload:
    size = None

    def __init__(self, chunks):
        self._chunks = iter(chunks)

    async def read(self, _size):
        return next(self._chunks, b"")


@pytest.mark.asyncio
async def test_upload_reader_does_not_keep_chunk_list(monkeypatch):
    monkeypatch.setattr(document_routes, "MAX_UPLOAD_BYTES", 5)
    upload = FakeUpload([b"ab", b"cde", b"f"])

    assert await document_routes._read_upload_bytes(upload) == b"abcdef"


def test_upload_reader_and_ingest_release_normal_upload_body():
    source = inspect.getsource(document_routes._read_upload_bytes)
    assert "buffer = bytearray()" in source
    assert 'return b"".join(chunks)' not in source

    pipeline = inspect.getsource(ingest.ingest_document)
    assert "source_data=data if stored_path is not None else None" in pipeline
    helper = inspect.getsource(ingest._ingest_into)
    assert "if source_data is None:" in helper
    assert "data = b\"\"" in helper
    assert "source_data=source_data" in helper
