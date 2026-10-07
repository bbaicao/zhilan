"""`回归修复`：上传 / 清洗预览**先按 size 预判 413，再分块读**。

缺陷形态（改前）：`data = await file.read()` 之后才判 `len(data)` ⇒ 远超 20MB 的请求体先被整份读
进内存再拒（uvicorn/h11 默认不限请求体）；`import-file` 的本地文件同理（`p.read_bytes()` 整读）。

修法：三条路径统一「**先预判、再分块读、读后二次判定**」——`file.size` / `p.stat().st_size` 预判
（超限**不读**）⇒ `_read_upload_bytes` 按 1MiB 分块读并拼回 bytes（`ingest_document` /
`prepare_document` 签名不变）⇒ 读后的 `len(data) > MAX_UPLOAD_BYTES` 判定照旧保留（`file.size`
不是契约）。

腿构成与判别力：

- 腿①②③（**判别腿**，改前必红）：上传 / 清洗预览 / import-file 三条路径在**超限**时 413 文案逐字，
  且 `UploadFile.read` / `Path.read_bytes` 的计数桩**零调用**（改前至少调用一次）；每条腿内部再放一条
  「反恒真」对照请求（上限放回默认 ⇒ 同一个计数桩必须记到读取），证明桩真挂在被测路径上。
- 腿④⑤（**判别腿**）：函数面构造 `file.size = None`（size 不可信）⇒ 仍 413（读后二次判定保留），
  且**读取量 < 整份**（边读边判、超限即停）；正常文件走分块读并**拼回完整字节**再交给 `prepare_document`。
  改前 `file.read()` 无参一次读全量 ⇒ 两条都红。
- 腿⑥⑦（**正控**）：小文件上传 / 清洗预览仍 200（分块读不改变正常路径）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import document_routes
from app.core.config import settings
from tests.api.test_chat_contract import client  # noqa: F401  —— 复用 ASGI 直连客户端（已覆盖 get_db）

#: 单测里把上限压到 10 字节：等价于「请求体远超上限」，不必造 20MB 载荷
LIMIT = 10
#: 默认上限（`document_routes.MAX_UPLOAD_BYTES` 的出厂值）
FULL_LIMIT = 20 * 1024 * 1024
TOO_LARGE = "文件超过限制，请压缩后重试"


@pytest.fixture(autouse=True)
def _upload_dir(monkeypatch, tmp_path):
    """上传落盘根钉到 tmp_path（同 `test_documents_endpoints.py` 的口径，不碰仓库 data/）。"""
    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path))
    yield


def _install_upload_read_counter(monkeypatch) -> list:
    """给 **Starlette 的** `UploadFile.read` 挂计数桩。

    ⚠️ 端点拿到的是 **Starlette** 的 `UploadFile`（multipart 解析器建出来的，`size` 也由它填）；
    `fastapi.datastructures.UploadFile` 只是类型注解与 `isinstance` 检查用，桩挂在它身上**记不到东西**
    ⇒ 本函数在腿①②里各配一条「反恒真」小文件请求（同一个桩必须记到读取），挂错类会当场变红。

    返回「每次调用的 size 参数」列表：改前端点会以 `read()`（size=-1）整读一次。
    """
    from starlette.datastructures import UploadFile as StarletteUploadFile

    calls: list = []
    orig = StarletteUploadFile.read

    async def counting_read(self, size: int = -1) -> bytes:  # noqa: ANN001
        calls.append(size)
        return await orig(self, size)

    monkeypatch.setattr(StarletteUploadFile, "read", counting_read)
    return calls


def _install_read_bytes_counter(monkeypatch) -> list:
    """给 `Path.read_bytes` 挂计数桩（`import-file` 那条本地读取路径）。"""
    calls: list = []
    orig = Path.read_bytes

    def counting_read_bytes(self) -> bytes:
        calls.append(str(self))
        return orig(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    return calls


class _StubUpload:
    """最小 `UploadFile` 替身：`size` 可控；`read(n)` 记录参数，并强制 `max_chunk` 上限。

    `max_chunk` 让「分块读」在几 KB 的载荷上就能被观测到（不必造 MB 级数据）。
    """

    def __init__(self, payload: bytes, *, size: int | None, max_chunk: int = 8, filename: str = "stub.md"):
        self._buf = payload
        self._pos = 0
        self.size = size
        self.filename = filename
        self.max_chunk = max_chunk
        self.read_sizes: list = []

    @property
    def consumed(self) -> int:
        return self._pos

    async def read(self, n: int = -1) -> bytes:
        self.read_sizes.append(n)
        remaining = self._buf[self._pos :]
        if not remaining:
            return b""
        if n is None or n < 0:
            take = remaining
        else:
            take = remaining[: min(n, self.max_chunk)]
        self._pos += len(take)
        return take


SAMPLE_MD = "# 标题\n\n第一段正文。\n\n第二段正文。\n"


# --- 判别腿：三条 HTTP 路径 ----------------------------------------------------


@pytest.mark.asyncio
async def test_a1_oversize_upload_413_without_reading_the_body(client, monkeypatch):
    """① 上传超限 ⇒ 413 文案逐字 + `UploadFile.read` **零调用**（改前会整读一次）。"""
    calls = _install_upload_read_counter(monkeypatch)
    monkeypatch.setattr(document_routes, "MAX_UPLOAD_BYTES", LIMIT)

    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("big.md", b"x" * 64, "text/markdown")},
    )
    assert resp.status_code == 413, resp.text
    assert resp.json()["detail"] == TOO_LARGE
    assert calls == [], f"超限请求体不得被读取，实测 read 参数={calls}"

    # 反恒真：上限放回默认值，同一个计数桩必须记到读取（否则本腿是恒真断言）
    monkeypatch.setattr(document_routes, "MAX_UPLOAD_BYTES", FULL_LIMIT)
    ok = await client.post(
        "/api/documents/upload",
        files={"file": ("small.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    assert ok.status_code == 200, ok.text
    assert calls, "计数桩没挂在被测读取路径上 ⇒ 本腿的「零调用」是恒真断言"
    assert ok.json()["chunk_count"] > 0


@pytest.mark.asyncio
async def test_a2_oversize_clean_preview_413_without_reading_the_body(client, monkeypatch):
    """② 清洗预览同一把尺：413 文案逐字 + 零读取（改前在 `file.read()` 之后才判）。"""
    calls = _install_upload_read_counter(monkeypatch)
    monkeypatch.setattr(document_routes, "MAX_UPLOAD_BYTES", LIMIT)

    resp = await client.post(
        "/api/documents/clean-preview",
        files={"file": ("big.md", b"x" * 64, "text/markdown")},
    )
    assert resp.status_code == 413, resp.text
    assert resp.json()["detail"] == TOO_LARGE
    assert calls == [], f"超限请求体不得被读取，实测 read 参数={calls}"

    monkeypatch.setattr(document_routes, "MAX_UPLOAD_BYTES", FULL_LIMIT)
    ok = await client.post(
        "/api/documents/clean-preview",
        files={"file": ("small.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    assert ok.status_code == 200, ok.text
    assert calls, "计数桩没挂在被测读取路径上 ⇒ 本腿的「零调用」是恒真断言"


@pytest.mark.asyncio
async def test_a3_oversize_import_file_413_without_reading_the_file(client, monkeypatch, tmp_path):
    """③ import-file 是本地路径：先 `st_size` 预判 ⇒ 超限时 `read_bytes` **零调用**。"""
    calls = _install_read_bytes_counter(monkeypatch)
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * 64)
    monkeypatch.setattr(document_routes, "MAX_UPLOAD_BYTES", LIMIT)

    resp = await client.post("/api/documents/import-file", json={"path": str(big)})
    assert resp.status_code == 413, resp.text
    assert resp.json()["detail"] == TOO_LARGE
    assert calls == [], f"超限本地文件不得被整读，实测 read_bytes={calls}"

    monkeypatch.setattr(document_routes, "MAX_UPLOAD_BYTES", FULL_LIMIT)
    small = tmp_path / "small.md"
    small.write_bytes(SAMPLE_MD.encode("utf-8"))
    ok = await client.post("/api/documents/import-file", json={"path": str(small)})
    assert ok.status_code == 200, ok.text
    assert calls == [str(small)], f"计数桩没挂在被测读取路径上 ⇒ 本腿的「零调用」是恒真断言（{calls}）"


# --- 判别腿：函数面（size 不可信 / 分块拼装）----------------------------------


@pytest.mark.asyncio
async def test_a4_unknown_size_still_413_and_reads_less_than_the_whole_body(monkeypatch):
    """④ `file.size = None`（size 不是契约）⇒ 读后二次判定仍 413，且**没有把整份读完**。

    改前：`file.read()` 无参一次读全量 ⇒ 读取量 == 整份（红）。
    """
    payload = b"x" * 64
    stub = _StubUpload(payload, size=None, max_chunk=8)
    monkeypatch.setattr(document_routes, "MAX_UPLOAD_BYTES", 16)

    with pytest.raises(HTTPException) as ei:
        await document_routes.upload_document_endpoint(file=stub, overwrite=False, db=None)
    assert ei.value.status_code == 413
    assert ei.value.detail == TOO_LARGE
    assert stub.consumed < len(payload), f"超限后必须停止读取，实测已读 {stub.consumed}/{len(payload)}"
    assert stub.consumed <= 16 + stub.max_chunk, f"读取量应受「上限 + 一个分块」约束：{stub.consumed}"
    assert all(n == document_routes._UPLOAD_CHUNK_BYTES for n in stub.read_sizes), stub.read_sizes


@pytest.mark.asyncio
async def test_a5_chunked_reads_are_reassembled_for_the_parser(monkeypatch):
    """⑤ 正常文件：**多次**按 `_UPLOAD_CHUNK_BYTES` 读、拼回**完整字节**再交给 `prepare_document`。

    改前：`file.read()` 无参一次读全量（`read_sizes == [-1]`）⇒ 红。
    """
    payload = SAMPLE_MD.encode("utf-8")
    stub = _StubUpload(payload, size=None, max_chunk=7, filename="chunked.md")
    monkeypatch.setattr(document_routes, "MAX_UPLOAD_BYTES", FULL_LIMIT)

    captured: dict = {}
    real_prepare = document_routes.prepare_document

    def spy_prepare(filename, data):  # noqa: ANN001
        captured["filename"] = filename
        captured["data"] = data
        return real_prepare(filename, data)

    monkeypatch.setattr(document_routes, "prepare_document", spy_prepare)

    out = await document_routes.clean_preview_endpoint(file=stub, db=None)
    assert captured["data"] == payload, "分块读必须拼回完整字节（不得丢块 / 截断）"
    assert captured["filename"] == "chunked.md"
    assert len(stub.read_sizes) >= 3, f"应发生多次分块读：{stub.read_sizes}"
    assert all(n == document_routes._UPLOAD_CHUNK_BYTES for n in stub.read_sizes), stub.read_sizes
    assert stub.consumed == len(payload), "正常文件必须读到 EOF"
    assert out.filename == "chunked.md" and out.chunk_count > 0


# --- 正控 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a6_small_upload_still_200(client):
    """⑥ 正控：小文件上传仍 200（分块读不改变正常路径）。"""
    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("ok.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["chunk_count"] > 0
    assert resp.json()["filename"] == "ok.md"


@pytest.mark.asyncio
async def test_a7_small_clean_preview_still_200(client):
    """⑦ 正控：小文件清洗预览仍 200（既有字段表断言在 `test_documents_endpoints.py`）。"""
    resp = await client.post(
        "/api/documents/clean-preview",
        files={"file": ("ok.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["chunk_count"] > 0
