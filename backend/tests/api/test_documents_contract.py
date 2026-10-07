"""条目 2.1 · documents 域契约测试（02 §1.1 / §7.4 / §4）。

用 ``httpx.ASGITransport`` 直连 ASGI（不起真端口、不抢端口）；``get_db`` 已由
``client`` 夹具覆盖到 function 级测试引擎（照 test_chat_contract 先例）。
上传落档目录重定向到 tmp_path，避免污染仓库。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import settings
from tests.api.test_chat_contract import client  # 复用 ASGI 客户端夹具

# DocumentOut 严格 12 字段（02 §7.4）
DOC_FIELDS = {
    "id",
    "filename",
    "file_type",
    "status",
    "chunk_count",
    "vectorized",
    "vector_error",
    "category",
    "tags",
    "created_at",
    "message",
    "kb_id",
}


@pytest.fixture(autouse=True)
def _upload_dir(monkeypatch, tmp_path):
    """上传落档目录重定向到 tmp_path（property 不能实例属性覆盖，改类属性）。"""
    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path))
    yield


@pytest.fixture(autouse=True)
def _isolate_vector(monkeypatch):
    """隔离真 embedding / 真 Qdrant（条目 2.3 起上传会走 index_chunks）。

    本文件只断言 ``vectorized is False`` 与字段形状：embed 快速失败即可，
    避免本机已配置 embed API 时被打真请求、或成功后断言翻红。
    """

    class _NoEmbed:
        dim = 4

        async def embed_texts(self, texts):
            raise RuntimeError("测试隔离：不调用真实 embedding")

    class _NoStore:
        def ensure_collection(self, dim):
            raise RuntimeError("测试隔离：不连 Qdrant")

        def upsert(self, points):
            raise RuntimeError("测试隔离：不连 Qdrant")

        def delete(self, ids):
            raise RuntimeError("测试隔离：不连 Qdrant")

    monkeypatch.setattr(
        "app.pipeline.ingest.get_embedding_client", lambda **kwargs: _NoEmbed()
    )
    monkeypatch.setattr(
        "app.pipeline.ingest.get_vector_store", lambda **kwargs: _NoStore()
    )


# --------------------------------------------------------------------------
# OpenAPI：4 个端点存在、方法正确
# --------------------------------------------------------------------------


def test_openapi_endpoints_present():
    from main import app

    paths = app.openapi()["paths"]
    # 主路由以 /api 为前缀挂载（见 main.py::app.include_router(router, prefix="/api")）
    assert "/api/documents" in paths
    assert "get" in paths["/api/documents"]
    assert "post" in paths["/api/documents/upload"]
    assert "post" in paths["/api/documents/import-folder/scan"]
    assert "post" in paths["/api/documents/import-file"]


# --------------------------------------------------------------------------
# a. GET /documents 裸数组，字段集恰好 12
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_documents_array_and_fields(client):
    resp = await client.get("/api/documents")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)
    # 空库也应返回数组（不包装成对象）
    assert resp.json() == []


@pytest.mark.asyncio
async def test_upload_fields_exactly_twelve(client):
    """上传 MD → 200，响应字段集恰好等于 12 字段（不多不少）。"""
    content = "# 合同\n\n这是上传契约测试的正文，需要足够长以切出分块。\n" * 3
    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("contract.md", content.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == DOC_FIELDS, f"多 {set(body)-DOC_FIELDS} / 少 {DOC_FIELDS-set(body)}"
    assert body["status"] == "ready"
    assert body["chunk_count"] > 0
    assert body["vectorized"] is False
    # 内部列绝不出现
    assert "index_status" not in body
    assert "file_path" not in body
    assert "index_error" not in body


# --------------------------------------------------------------------------
# c. 重复上传 → 409，detail 是对象
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_upload_409(client):
    content = "# 重复\n\n同一内容重复上传应返回 409。\n" * 3
    first = await client.post(
        "/api/documents/upload",
        files={"file": ("a.md", content.encode("utf-8"), "text/markdown")},
    )
    assert first.status_code == 200
    second = await client.post(
        "/api/documents/upload",
        files={"file": ("b.md", content.encode("utf-8"), "text/markdown")},
    )
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert detail["code"] == "duplicate_content"
    assert "existing_id" in detail and "filename" in detail
    assert detail["existing_id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_duplicate_upload_409_detail_names_existing_row(client):
    """S6 · 160 补腿：detail 的 message / filename 指向**既有行**（第一次上传那条）。

    既有 test_duplicate_upload_409 只钉了 code / existing_id：文案与 filename
    此前没有判据 —— 把 filename 换成本次上传名（detail-b.md）或把 message 换成英文，
    本腿必红。（四键精确集合 / 逐键类型 / overwrite 反向保护见
    tests/api/test_s6_160_dup_409_shape.py，两处不重复。）
    """
    content = "# 重复文案\n\n同一内容重复上传时，detail 必须报既有行。\n" * 3
    first = await client.post(
        "/api/documents/upload",
        files={"file": ("detail-a.md", content.encode("utf-8"), "text/markdown")},
    )
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]

    second = await client.post(
        "/api/documents/upload",
        files={"file": ("detail-b.md", content.encode("utf-8"), "text/markdown")},
    )
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert detail["filename"] == "detail-a.md", "filename 是既有行的名字，不是本次的 detail-b.md"
    assert detail["message"] == f"知识库里已有同内容文档（id={first_id}，detail-a.md）。"


# --------------------------------------------------------------------------
# d. 不支持后缀 → 400，detail 中文
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_suffix_400(client):
    resp = await client.post(
        "/api/documents/upload",
        files={"file": ("malware.exe", b"MZ\x90\x00fake", "application/octet-stream")},
    )
    assert resp.status_code == 400, resp.text
    assert "暂不支持" in resp.json()["detail"]


# --------------------------------------------------------------------------
# e. import-folder/scan 分类 + 路径不存在 → 400
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_folder_scan_classifies(client, tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "a.md").write_text("# A\n\n正文。\n")
    (d / "b.exe").write_bytes(b"MZ fake")
    (d / ".hidden.md").write_text("# hidden\n")
    sub = d / "sub"
    sub.mkdir()
    (sub / "c.md").write_text("# C\n\n子目录正文。\n")

    resp = await client.post("/api/documents/import-folder/scan", json={"path": str(d), "recursive": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {f["name"] for f in body["files"]}
    skipped_names = {s["name"] for s in body["skipped"]}
    assert "a.md" in names and "c.md" in names  # 递归收录 .md
    assert "b.exe" in skipped_names  # 不支持后缀跳过
    assert ".hidden.md" in skipped_names  # 隐藏文件跳过
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_import_folder_scan_missing_path_400(client, tmp_path):
    resp = await client.post(
        "/api/documents/import-folder/scan",
        json={"path": str(tmp_path / "nope"), "recursive": True},
    )
    assert resp.status_code == 400, resp.text


# --------------------------------------------------------------------------
# f. import-file：category 回退文件夹名；路径不存在 → 404
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_file_fallback_category_is_folder_name(client, tmp_path):
    folder = tmp_path / "我的笔记"
    folder.mkdir()
    f = folder / "x.md"
    f.write_text("# X\n\n导入单文件测试正文。\n" * 2)

    resp = await client.post("/api/documents/import-file", json={"path": str(f)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == DOC_FIELDS
    assert body["status"] == "ready"
    # fallback_category = 文件所在文件夹名
    assert body["category"] == "我的笔记"


@pytest.mark.asyncio
async def test_import_file_missing_404(client, tmp_path):
    resp = await client.post("/api/documents/import-file", json={"path": str(tmp_path / "gone.md")})
    assert resp.status_code == 404, resp.text
    assert "文件不存在" in resp.json()["detail"]
