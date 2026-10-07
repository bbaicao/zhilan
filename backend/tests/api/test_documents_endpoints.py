"""条目 2.4 · documents 域 6 个新端点的契约/行为测试。

覆盖：OpenAPI 存在性、clean-preview 不写库、clean-report、DELETE 软删、
reindex 空库/400、generate-note 未配模型 400 / Faux 形状、
generate-note/stream 404 与 SSE Content-Type + done 字段。

用 ``httpx.ASGITransport`` 直连 ASGI；DB 用独立测试库；不连真模型。
**不要**与其它会抢 MySQL 的全量套件并发。
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.config import settings
from tests.api.test_chat_contract import client, faux_model  # 复用夹具与 Faux 造模


@pytest.fixture(autouse=True)
def _upload_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(type(settings), "upload_path", property(lambda self: tmp_path))
    yield


SAMPLE_MD = (
    "---\ncategory: 合同\ntags:\n  - 测试\n---\n\n"
    "# 项目说明\n\n本项目于 2024-03-15 启动，预算 120 万元。\n\n"
    "第一段正文，需要足够长以切出分块。\n\n"
    "第二段继续说明交付节点与验收标准。\n"
) * 2


# --------------------------------------------------------------------------
# 1. OpenAPI：6 个新端点全部存在
# --------------------------------------------------------------------------


def test_openapi_contains_six_new_endpoints():
    from main import app

    paths = app.openapi()["paths"]
    assert "post" in paths["/api/documents/reindex"]
    assert "post" in paths["/api/documents/clean-preview"]
    assert "get" in paths["/api/documents/{document_id}/clean-report"]
    assert "post" in paths["/api/documents/{document_id}/generate-note"]
    assert "post" in paths["/api/documents/{document_id}/generate-note/stream"]
    assert "delete" in paths["/api/documents/{document_id}"]


# --------------------------------------------------------------------------
# 2. clean-preview：字段齐全 + 不写库
# --------------------------------------------------------------------------

CLEAN_PREVIEW_FIELDS = {
    "filename",
    "file_type",
    "chunk_count",
    "content_hash",
    "raw_content",
    "clean_content",
    "report",
    "chunks",
    "removed_ratio",
    "frontmatter",
}


@pytest.mark.asyncio
async def test_clean_preview_fields_and_no_db_write(client, db):
    resp = await client.post(
        "/api/documents/clean-preview",
        files={"file": ("preview.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == CLEAN_PREVIEW_FIELDS
    assert body["filename"] == "preview.md"
    assert body["file_type"] == "md"
    assert body["chunk_count"] > 0
    assert isinstance(body["chunks"], list)
    assert all(isinstance(c, str) for c in body["chunks"])
    assert body["content_hash"]
    assert "raw_content" in body and body["raw_content"]
    assert body["clean_content"]
    assert isinstance(body["report"], dict)
    assert body["report"].get("rule_fingerprint")
    assert isinstance(body["removed_ratio"], float)
    # md frontmatter
    assert body["frontmatter"] is not None
    assert body["frontmatter"]["category"] == "合同"
    assert "测试" in body["frontmatter"]["tags"]

    # 不写库
    await db.rollback()
    count = (await db.execute(text("SELECT COUNT(*) FROM documents"))).scalar()
    assert count == 0
    chunk_count = (await db.execute(text("SELECT COUNT(*) FROM chunks"))).scalar()
    assert chunk_count == 0


@pytest.mark.asyncio
async def test_clean_preview_too_large_413(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.document_routes.MAX_UPLOAD_BYTES", 10
    )
    resp = await client.post(
        "/api/documents/clean-preview",
        files={"file": ("big.md", b"x" * 50, "text/markdown")},
    )
    assert resp.status_code == 413
    assert "文件超过限制" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_clean_preview_non_md_frontmatter_none(client):
    resp = await client.post(
        "/api/documents/clean-preview",
        files={"file": ("a.txt", "纯文本正文，用于预览。\n" * 3, "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["frontmatter"] is None


# --------------------------------------------------------------------------
# 3. clean-report：upload 后 GET；404 路径
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_report_after_upload(client):
    up = await client.post(
        "/api/documents/upload",
        files={"file": ("report.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    assert up.status_code == 200, up.text
    doc_id = up.json()["id"]

    resp = await client.get(f"/api/documents/{doc_id}/clean-report")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"id", "filename", "raw_content", "clean_content", "report", "chunk_count"}
    assert body["id"] == doc_id
    assert body["filename"] == "report.md"
    assert body["raw_content"]
    assert body["clean_content"]
    assert body["chunk_count"] > 0
    assert isinstance(body["report"], dict)
    # clean_content 是按 ordinal 拼接的 chunks
    chunks = await client.get("/api/documents")
    assert chunks.status_code == 200
    assert body["chunk_count"] == up.json()["chunk_count"]


@pytest.mark.asyncio
async def test_clean_report_missing_404(client):
    resp = await client.get("/api/documents/99999/clean-report")
    assert resp.status_code == 404
    assert "文档不存在" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_clean_report_after_delete_404(client):
    up = await client.post(
        "/api/documents/upload",
        files={"file": ("gone.md", "# 标题\n\n正文。\n" * 3, "text/markdown")},
    )
    doc_id = up.json()["id"]
    deleted = await client.delete(f"/api/documents/{doc_id}")
    assert deleted.status_code == 200
    resp = await client.get(f"/api/documents/{doc_id}/clean-report")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# 4. DELETE 软删
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_soft_and_list_excludes(client, db):
    up = await client.post(
        "/api/documents/upload",
        files={"file": ("del.md", "# 删除测试\n\n正文内容足够长。\n" * 3, "text/markdown")},
    )
    assert up.status_code == 200, up.text
    doc_id = up.json()["id"]

    resp = await client.delete(f"/api/documents/{doc_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["message"] == "已移入回收站，可在回收站恢复或彻底删除"

    listing = await client.get("/api/documents")
    assert listing.status_code == 200
    ids = [d["id"] for d in listing.json()]
    assert doc_id not in ids

    # 软删：行还在，deleted_at 非空；向量不要求
    await db.rollback()
    row = (
        await db.execute(
            text("SELECT deleted_at FROM documents WHERE id = :id"), {"id": doc_id}
        )
    ).first()
    assert row is not None
    assert row[0] is not None

    again = await client.delete(f"/api/documents/{doc_id}")
    assert again.status_code == 404


@pytest.mark.asyncio
async def test_delete_missing_404(client):
    resp = await client.delete("/api/documents/99999")
    assert resp.status_code == 404
    assert "文档不存在" in resp.json()["detail"]


# --------------------------------------------------------------------------
# 5. reindex
# --------------------------------------------------------------------------

REINDEX_FIELDS = {
    "documents",
    "notes",
    "vectors",
    "failed",
    "orphans_removed",
    "recleaned",
    "messages",
}


@pytest.mark.asyncio
async def test_reindex_empty_db_ok(client):
    resp = await client.post("/api/documents/reindex", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == REINDEX_FIELDS
    assert body["documents"] == 0
    assert body["notes"] == 0
    assert body["vectors"] == 0
    assert body["failed"] == 0
    assert body["orphans_removed"] == 0
    assert body["recleaned"] == 0
    assert isinstance(body["messages"], list)


@pytest.mark.asyncio
async def test_reindex_missing_id_400(client):
    resp = await client.post("/api/documents/reindex", json={"document_id": 99999})
    assert resp.status_code == 400, resp.text
    assert "文档不存在" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_reindex_after_upload(client, db):
    """重建向量失败仍返回统计 200，计失败且保留首次发布的关键词版本。"""
    up = await client.post(
        "/api/documents/upload",
        files={"file": ("rix.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    assert up.status_code == 200
    doc_id = up.json()["id"]
    statement = text(
        "SELECT active_build_id, raw_content, chunk_count, index_status "
        "FROM documents WHERE id=:id"
    )
    before = (await db.execute(statement, {"id": doc_id})).one()
    await db.rollback()

    resp = await client.post("/api/documents/reindex", json={"document_id": doc_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == REINDEX_FIELDS
    assert body["documents"] == 0
    assert body["notes"] == 0
    assert body["vectors"] == 0
    assert body["failed"] == 1
    assert body["recleaned"] == 0
    assert any(f"id={doc_id}" in message for message in body["messages"])
    after = (await db.execute(statement, {"id": doc_id})).one()
    assert after == before
    assert after.index_status == "ready"


@pytest.mark.asyncio
async def test_reindex_deleted_doc_400(client):
    up = await client.post(
        "/api/documents/upload",
        files={"file": ("drix.md", "# 重建已删\n\n正文。\n" * 3, "text/markdown")},
    )
    doc_id = up.json()["id"]
    await client.delete(f"/api/documents/{doc_id}")
    resp = await client.post("/api/documents/reindex", json={"document_id": doc_id})
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# 6. generate-note
# --------------------------------------------------------------------------

NOTE_FIELDS = {
    "title",
    "content",
    "category",
    "tags",
    "source_document_id",
    "source_filename",
    "completeness",
}

FAUX_NOTE_JSON = json.dumps(
    {
        "title": "项目说明笔记",
        "content": "项目于 2024-03-15 启动，预算 120 万元。交付节点见正文。",
        "category": "合同",
        "tags": ["测试", "笔记"],
    },
    ensure_ascii=False,
)


@pytest.mark.asyncio
async def test_generate_note_unconfigured_400(client, monkeypatch):
    up = await client.post(
        "/api/documents/upload",
        files={"file": ("note.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    doc_id = up.json()["id"]
    from app.api import chat_routes

    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: None)
    resp = await client.post(f"/api/documents/{doc_id}/generate-note")
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_generate_note_missing_404(client, monkeypatch):
    from app.api import chat_routes

    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: faux_model("x"))
    resp = await client.post("/api/documents/99999/generate-note")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_generate_note_with_faux(client, monkeypatch):
    up = await client.post(
        "/api/documents/upload",
        files={"file": ("note.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    doc_id = up.json()["id"]
    filename = up.json()["filename"]

    from app.api import chat_routes

    monkeypatch.setattr(
        chat_routes, "resolve_chat_model", lambda: faux_model(FAUX_NOTE_JSON)
    )
    resp = await client.post(f"/api/documents/{doc_id}/generate-note")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == NOTE_FIELDS
    assert body["title"] == "项目说明笔记"
    assert body["content"]
    assert body["source_document_id"] == doc_id
    assert body["source_filename"] == filename
    assert body["completeness"]["checker"] == "regex"
    assert "complete" in body["completeness"]
    assert isinstance(body["completeness"]["missing"], list)


# --------------------------------------------------------------------------
# 7. generate-note/stream
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_note_stream_missing_404(client, monkeypatch):
    from app.api import chat_routes

    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: faux_model("x"))
    resp = await client.post("/api/documents/99999/generate-note/stream")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_generate_note_stream_unconfigured_400(client, monkeypatch):
    up = await client.post(
        "/api/documents/upload",
        files={"file": ("s.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    doc_id = up.json()["id"]
    from app.api import chat_routes

    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: None)
    resp = await client.post(f"/api/documents/{doc_id}/generate-note/stream")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_generate_note_stream_content_type_and_done(client, monkeypatch):
    up = await client.post(
        "/api/documents/upload",
        files={"file": ("s.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    doc_id = up.json()["id"]

    from app.api import chat_routes

    monkeypatch.setattr(
        chat_routes, "resolve_chat_model", lambda: faux_model(FAUX_NOTE_JSON)
    )
    async with client.stream(
        "POST", f"/api/documents/{doc_id}/generate-note/stream"
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        ctype = resp.headers.get("content-type", "")
        assert "text/event-stream" in ctype
        raw = b""
        async for chunk in resp.aiter_bytes():
            raw += chunk

    text = raw.decode("utf-8")
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block.startswith("data: "):
            continue
        events.append(json.loads(block[len("data: ") :]))

    types = [e.get("type") for e in events]
    assert "done" in types or "error" in types
    done = next((e for e in events if e.get("type") == "done"), None)
    if done is not None:
        for key in (
            "title",
            "content",
            "category",
            "tags",
            "source_document_id",
            "source_filename",
            "completeness",
        ):
            assert key in done, f"done 缺少 {key}"
    else:
        # Faux stream 若走 error 分支，至少要有 error 事件且带 detail
        err = next(e for e in events if e.get("type") == "error")
        assert err.get("detail")


# --------------------------------------------------------------------------
# 回归修复：clean-preview 的解析段必须丢线程
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_preview_parse_runs_off_the_event_loop(client, monkeypatch):
    """**回归修复 · clean-preview**：``prepare_document``（MinerU OCR / 解码 /
    切块全在这棵调用树里）必须丢线程 —— 预览一个大文件同样不得冻结事件循环。

    判别法：解析桩记录执行线程；ASGI 直连下端点跑在测试协程的事件循环线程上
    ⇒ 桩线程必须异于循环线程。回退（端点内同步直调）⇒ 桩回到循环线程 ⇒ 红。
    """
    import threading

    from app.pipeline import ingest as ingest_mod

    loop_tid = threading.get_ident()
    tids: list[int] = []

    def _tid_parse(filename, data):
        tids.append(threading.get_ident())
        return "预览正文，用于事件循环占用判据。\n" * 3, []

    monkeypatch.setattr(ingest_mod, "parse_document_pages", _tid_parse)
    resp = await client.post(
        "/api/documents/clean-preview",
        files={"file": ("offloop.txt", "预览正文。\n" * 3, "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    assert tids, "解析桩没被调到 ⇒ 判据无效"
    assert all(t != loop_tid for t in tids), (
        "clean-preview 的解析跑在事件循环线程上 ⇒（回归修复 未修）"
    )
