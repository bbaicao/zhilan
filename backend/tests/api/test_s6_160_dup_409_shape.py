"""S6 · 条目 160 取证：重复上传 409 的 detail **精确形状**（02 §1.1 / §7.4）。

产品侧只读口径（本批不改 app/** 一个字节）：
- backend/app/api/document_routes.py:244-281：except DuplicateDocument ⇒
  HTTPException(409, detail={"code": "duplicate_content", "message": str(exc),
  "existing_id": exc.document_id, "filename": exc.filename})。
- backend/app/pipeline/ingest.py:832-886：命中既有行的三种条件 ——
  ① 既有行在回收站（deleted_at 非空）⇒ 提示恢复/彻删，overwrite=True **也先提示**；
  ② 既有行 index_status == "failed" ⇒ 认领重试（**不** 409；本文件未覆盖，见报告「未覆盖面」）；
  ③ 正常旧行：overwrite=True ⇒ 验证新版后原位发布，保留 id 与历史引用（**不是** 409）；
     overwrite=False ⇒ 409（message / filename 都报**既有行**）。

与 tests/api/test_documents_contract.py 的分工：那边登记 message / filename 文案逐字；
本文件登记**四键精确集合**、**逐键类型**、**overwrite=True 反向保护**与**回收站分支**。

判别力：detail 多一键/少一键、existing_id 被转成 str、overwrite=True 也走 409、
回收站分支被错放到 overwrite 判断之后、或该分支 rollback 后读 ORM 属性（回归修复）
把 409 变 500 —— 本文件对应腿必红。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Chunk, Document, IndexBuild
from app.pipeline import ingest
from app.vector.embed import FakeEmbedding
from app.vector.qdrant_store import QdrantVectorStore

from tests.api.test_chat_contract import client  # noqa: F401  复用 ASGI 客户端夹具
from tests.api.test_documents_contract import _upload_dir  # noqa: F401

pytestmark = pytest.mark.asyncio

DETAIL_KEYS = {"code", "message", "existing_id", "filename"}
TRASH_MESSAGE = "该内容在回收站中，可恢复或彻底删除后再上传"


@pytest.fixture(autouse=True)
def _isolate_vector(monkeypatch, tmp_path):
    """覆盖重建必须成功验证新版；注入合法模型身份与本地临时 Qdrant。"""
    embedding = FakeEmbedding(dim=4)
    store = QdrantVectorStore(path=str(tmp_path / "vectors"))
    monkeypatch.setattr(ingest, "get_embedding_client", lambda **kw: embedding)
    monkeypatch.setattr(ingest, "get_vector_store", lambda **kw: store)
    try:
        yield store
    finally:
        store.close()


async def _upload(client, filename: str, content: str, *, overwrite: bool | None = None):
    """上传一段内容；overwrite 走查询参数（前端 frontend/src/api.ts:442-445 同款 ?overwrite=true）。"""
    url = "/api/documents/upload"
    if overwrite is not None:
        url += f"?overwrite={'true' if overwrite else 'false'}"
    return await client.post(
        url,
        files={"file": (filename, content.encode("utf-8"), "text/markdown")},
    )


async def test_duplicate_409_detail_is_exactly_four_typed_keys(client):  # noqa: F811
    """四键精确集合相等 + 逐键类型 + 既有行口径（id / 文件名都指第一次上传那条）。"""
    content = "# S6-160\n\n同一内容重复上传时的 detail 形状。\n" * 3
    first = await _upload(client, "s6-160-a.md", content)
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]

    second = await _upload(client, "s6-160-b.md", content)
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]

    assert isinstance(detail, dict), f"409 的 detail 必须是对象，实得 {type(detail).__name__}"
    assert set(detail) == DETAIL_KEYS, (
        f"detail 键集必须精确等于 {sorted(DETAIL_KEYS)}；"
        f"多 {sorted(set(detail) - DETAIL_KEYS)} / 少 {sorted(DETAIL_KEYS - set(detail))}"
    )
    assert detail["code"] == "duplicate_content"
    assert isinstance(detail["message"], str)
    assert not detail["message"].isascii(), "message 必须是可读中文提示"
    assert "知识库里已有同内容文档" in detail["message"]
    assert isinstance(detail["existing_id"], int) and not isinstance(detail["existing_id"], bool)
    assert detail["existing_id"] == first_id
    assert isinstance(detail["filename"], str)
    assert detail["filename"] == "s6-160-a.md", (
        "filename 报的是既有行的文件名（第一次上传名），不是本次的 s6-160-b.md"
    )


async def test_overwrite_true_rebuilds_and_never_409(client, db):  # noqa: F811
    """反向保护：overwrite=True 命中正常旧行时发布新版（200），保留身份及旧块。"""
    content = "# S6-160-overwrite\n\n覆盖重建必须放行，不许回 409。\n" * 3
    first = await _upload(client, "s6-160-ow-a.md", content)
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]
    old = await db.get(Document, first_id, populate_existing=True)
    old_active, old_content = old.active_build_id, old.content_build_id
    old_chunk_ids = list((await db.scalars(
        select(Chunk.id).where(Chunk.document_id == first_id, Chunk.build_id == old_content)
    )).all())
    assert old_active and old_content and old_chunk_ids
    await db.rollback()

    rebuild = await _upload(client, "s6-160-ow-b.md", content, overwrite=True)
    assert rebuild.status_code == 200, rebuild.text
    body = rebuild.json()
    assert body["filename"] == "s6-160-ow-b.md"
    assert body["id"] == first_id, "覆盖重建保留文档身份，历史引用仍指向同一资料"
    current = await db.get(Document, first_id, populate_existing=True)
    assert current.active_build_id != old_active
    assert current.content_build_id != old_content
    assert current.active_build_id and current.content_build_id and current.vectorized is True
    assert (await db.get(IndexBuild, old_active, populate_existing=True)).state == "retired"
    assert all([(await db.get(Chunk, chunk_id)) is not None for chunk_id in old_chunk_ids])

    third = await _upload(client, "s6-160-ow-c.md", content)
    assert third.status_code == 409, third.text
    assert third.json()["detail"]["existing_id"] == body["id"], (
        "重建后 409 必须指向已启用新版的同一文档身份"
    )
    assert third.json()["detail"]["filename"] == "s6-160-ow-b.md"


async def test_trash_branch_hints_restore_and_beats_overwrite(client):  # noqa: F811
    """回收站里有同内容：overwrite=False / True **都** 409 提示恢复（不是 500、不是重建）。"""
    content = "# S6-160-trash\n\n回收站里有同内容时的提示分支。\n" * 3
    first = await _upload(client, "s6-160-trash-a.md", content)
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]

    soft = await client.delete(f"/api/documents/{first_id}")
    assert soft.status_code == 200, soft.text

    for overwrite in (False, True):
        resp = await _upload(client, "s6-160-trash-b.md", content, overwrite=overwrite)
        assert resp.status_code == 409, (overwrite, resp.text)  # ⛔ 不是 500（回归修复 回归位）
        detail = resp.json()["detail"]
        assert set(detail) == DETAIL_KEYS
        assert detail["code"] == "duplicate_content"
        assert detail["message"] == TRASH_MESSAGE
        assert isinstance(detail["existing_id"], int) and not isinstance(detail["existing_id"], bool)
        assert detail["existing_id"] == first_id
        assert detail["filename"] == "s6-160-trash-a.md"

    station = await client.get("/api/trash")
    assert station.status_code == 200, station.text
    assert [item["id"] for item in station.json()["documents"]] == [first_id], (
        "回收站分支下 overwrite=True 也不许顺带清掉既有行"
    )
