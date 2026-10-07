"""条目 4.5.4 · 回收站四端点：与 4.5.1 的软删**同一套语义**的自洽验收。

覆盖（对应契约 §1.2 的四行 + 冻结前端 ``TrashOut`` / ``TrashItem`` / ``TrashKind``）：

- ``GET /api/trash`` 的**字段表逐字**：三组、每组一条 ``{id,title,category?,deleted_at}``，
  文档 ``title`` = 文件名、会话不带 ``category``、按删除时间倒序；**只列软删的行**；
- 软删 → 恢复 → 彻底删除**全链**（会话的消息、文档的分块在软删/恢复期间一行不动）；
- ``kind`` 取值域封闭：非法 kind 走 422（路径参数 Literal），不是 500、不是静默空；
- 彻底删除才清子行：会话走 messages、文档走 purge_document、笔记走删行 + 链接边重解析；
- ``POST /api/trash/empty`` 对全站逐条做彻底删除并回三类条数（空站回全 0）。

夹具：``client`` 复用 4.5.1 的公共夹具（``from tests.api.test_chat_contract import client``），
``db`` 来自 ``tests/conftest.py``；公共夹具的清表清单没覆盖 notes / wiki_links，
故本文件自带 ``clean_extra`` 补清（夹具写在自己的测试文件里，不改公共 conftest）。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.models import Chat, Chunk, Document, Message, Note, WikiLink
from app.session import repository as repo
from tests.api.test_chat_contract import client  # noqa: F401 —— 公共夹具（4.5.1）

pytestmark = pytest.mark.asyncio

#: 公共夹具（``tests/conftest.py`` 的 ``_CLEAN_ORDER``）没清这几张表：本文件补清。
_EXTRA_TABLES = ("wiki_links", "session_groups", "notes", "usage_events")


@pytest_asyncio.fixture
async def clean_extra(db):
    """在 ``db`` 夹具之上补清 notes / wiki_links / session_groups / usage_events。"""
    for table in _EXTRA_TABLES:
        await db.execute(text(f"DELETE FROM {table}"))
    await db.commit()
    return db


async def _fresh(db) -> None:
    """结束 ``db`` 上的当前事务，让后续断言看到**别的连接刚提交**的行。

    MySQL 默认隔离级别是 REPEATABLE READ：``db`` 夹具的会话在第一次读时就定了快照，
    而接口调用跑在另一个连接上（自己提交）。不先 commit/rollback，断言会读到旧快照
    ——实测踩过（删除成功却依然查得到那条会话）。
    """
    await db.commit()


# --- 造数据 -----------------------------------------------------------------


async def _note(db, title: str, *, category: str = "", tags=None, deleted: bool = False) -> Note:
    row = Note(
        title=title,
        content=f"{title} 的正文",
        category=category,
        tags=tags,
        kb_id=1,
        deleted_at=repo.utc_now() if deleted else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _document(
    db,
    filename: str,
    *,
    category: str = "",
    tags=None,
    chunks: list[str] | None = None,
    deleted: bool = False,
) -> Document:
    row = Document(
        filename=filename,
        file_type="md",
        status="ready",
        index_status="ready",
        kb_id=1,
        category=category,
        tags=tags,
        raw_content="原文正文",
        deleted_at=repo.utc_now() if deleted else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    for index, content in enumerate(chunks or []):
        db.add(Chunk(document_id=row.id, content=content, ordinal=index))
    await db.commit()
    return row


async def _chat(db, title: str, *, deleted: bool = False, messages: int = 0) -> Chat:
    chat = await repo.create_chat(db, title=title)
    for index in range(messages):
        db.add(Message(chat_id=chat.id, role="user", content=f"第 {index} 问"))
    if deleted:
        chat.deleted_at = repo.utc_now()
    await db.commit()
    await db.refresh(chat)
    return chat


# --- GET /api/trash ---------------------------------------------------------


async def test_list_trash_contract_fields(client, clean_extra):  # noqa: F811
    """字段表逐字 + 只列软删的行 + 文档 title=文件名、会话不带 category。"""
    await _note(clean_extra, "活笔记", category="工作")
    trashed_note = await _note(clean_extra, "删掉的笔记", category="生活", deleted=True)
    await _document(clean_extra, "活文档.md")
    trashed_doc = await _document(clean_extra, "删掉的文档.md", category="资料", deleted=True)
    await _chat(clean_extra, "活会话")
    trashed_chat = await _chat(clean_extra, "删掉的会话", deleted=True)

    resp = await client.get("/api/trash")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"notes", "documents", "chats"}
    assert [item["id"] for item in data["notes"]] == [trashed_note.id]
    assert [item["id"] for item in data["documents"]] == [trashed_doc.id]
    assert [item["id"] for item in data["chats"]] == [trashed_chat.id]

    note_item = data["notes"][0]
    assert set(note_item) == {"id", "title", "category", "deleted_at"}
    assert note_item["title"] == "删掉的笔记"
    assert note_item["category"] == "生活"
    assert isinstance(note_item["deleted_at"], str)

    doc_item = data["documents"][0]
    assert set(doc_item) == {"id", "title", "category", "deleted_at"}
    assert doc_item["title"] == "删掉的文档.md", "文档进回收站时 title = 文件名（冻结前端口径）"

    chat_item = data["chats"][0]
    assert set(chat_item) == {"id", "title", "deleted_at"}, "会话没有分类维度，不发空 category 占位"
    assert chat_item["title"] == "删掉的会话"


async def test_list_trash_orders_by_deleted_at_desc(client, clean_extra):  # noqa: F811
    """按删除时间倒序：刚删的在上（deleted_at 相同则按 id 倒序兜底）。"""
    first = await _note(clean_extra, "先删的", deleted=True)
    second = await _note(clean_extra, "后删的", deleted=True)
    second.deleted_at = repo.utc_now()
    first.deleted_at = second.deleted_at.replace(year=2000)
    await clean_extra.commit()

    data = (await client.get("/api/trash")).json()
    assert [item["id"] for item in data["notes"]] == [second.id, first.id]


# --- 恢复：与 4.5.1 的软删自洽 --------------------------------------------


async def test_soft_delete_then_restore_brings_chat_and_messages_back(client, clean_extra):  # noqa: F811
    """全链自洽：``DELETE /api/chats/{id}``（4.5.1 软删）→ 站内可见 → 恢复 → 原样回来。"""
    chat = await _chat(clean_extra, "要删的会话", messages=2)
    chat_id = chat.id

    deleted = await client.delete(f"/api/chats/{chat_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True, "message": "已移入回收站，可在回收站恢复或彻底删除"}

    await _fresh(clean_extra)  # 让下面的断言看到接口那条连接刚提交的行
    # 软删期间：默认列表看不到、详情按 404、回收站里在
    assert chat_id not in {item["id"] for item in (await client.get("/api/chats")).json()}
    assert (await client.get(f"/api/chats/{chat_id}")).status_code == 404
    assert chat_id in {item["id"] for item in (await client.get("/api/trash")).json()["chats"]}
    # 消息行一行没动（软删只置时间戳）
    rows = (await clean_extra.execute(select(Message).where(Message.chat_id == chat_id))).scalars().all()
    assert len(rows) == 2

    restored = await client.post("/api/trash/restore", json={"type": "chat", "id": chat_id})
    assert restored.status_code == 200
    assert restored.json() == {"ok": True, "message": "已恢复"}

    await _fresh(clean_extra)
    assert chat_id in {item["id"] for item in (await client.get("/api/chats")).json()}
    detail = await client.get(f"/api/chats/{chat_id}")
    assert detail.status_code == 200
    assert [msg["content"] for msg in detail.json()["messages"]] == ["第 0 问", "第 1 问"]


async def test_restore_notes_and_documents(client, clean_extra):  # noqa: F811
    """笔记 / 文档同链：恢复 = 清 ``deleted_at``，分块仍在。"""
    note = await _note(clean_extra, "待恢复笔记", deleted=True)
    doc = await _document(clean_extra, "待恢复文档.md", chunks=["块一"], deleted=True)

    for kind, item_id in (("note", note.id), ("document", doc.id)):
        resp = await client.post("/api/trash/restore", json={"type": kind, "id": item_id})
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True

    await _fresh(clean_extra)
    await clean_extra.refresh(note)
    await clean_extra.refresh(doc)
    assert note.deleted_at is None
    assert doc.deleted_at is None
    chunks = (
        await clean_extra.execute(select(Chunk).where(Chunk.document_id == doc.id))
    ).scalars().all()
    assert len(chunks) == 1, "软删/恢复期间分块不动（恢复零成本）"
    assert (await client.get("/api/trash")).json()["notes"] == []


@pytest.mark.parametrize("kind", ["note", "document", "chat"])
async def test_restore_404_when_not_in_trash(client, clean_extra, kind):  # noqa: F811
    """不在站里（在库）的条目恢复 → 404 可读中文；不存在的 id 同样 404。"""
    made = {
        "note": await _note(clean_extra, "在库笔记"),
        "document": await _document(clean_extra, "在库文档.md"),
        "chat": await _chat(clean_extra, "在库会话"),
    }[kind]
    label = {"note": "笔记", "document": "文档", "chat": "会话"}[kind]

    live = await client.post("/api/trash/restore", json={"type": kind, "id": made.id})
    assert live.status_code == 404
    assert live.json()["detail"] == f"回收站中没有这条{label}"

    missing = await client.post("/api/trash/restore", json={"type": kind, "id": 999999})
    assert missing.status_code == 404
    assert missing.json()["detail"] == f"回收站中没有这条{label}"


async def test_restore_invalid_body_422(client):  # noqa: F811
    """非法 ``type`` / 非正 ``id``：422（pydantic 校验），不是 500。"""
    bad_kind = await client.post("/api/trash/restore", json={"type": "bogus", "id": 1})
    assert bad_kind.status_code == 422
    bad_id = await client.post("/api/trash/restore", json={"type": "note", "id": 0})
    assert bad_id.status_code == 422


# --- 彻底删除 ---------------------------------------------------------------


async def test_purge_chat_deletes_messages_and_chat(client, clean_extra):  # noqa: F811
    """会话彻底删除：连 messages 一起清（软删不调用这条路径）。"""
    chat = await _chat(clean_extra, "彻底删的会话", deleted=True, messages=3)
    resp = await client.delete(f"/api/trash/chat/{chat.id}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "message": "已彻底删除"}

    await _fresh(clean_extra)
    assert (await clean_extra.execute(select(Chat).where(Chat.id == chat.id))).scalars().all() == []
    assert (
        await clean_extra.execute(select(Message).where(Message.chat_id == chat.id))
    ).scalars().all() == []
    assert (await client.get("/api/trash")).json()["chats"] == []


async def test_purge_document_deletes_chunks(client, clean_extra):  # noqa: F811
    """文档彻底删除：分块一起清（向量由既有 purge_document 负责，单测里被隔离成假件）。"""
    doc = await _document(clean_extra, "彻底删的文档.md", chunks=["块一", "块二"], deleted=True)
    resp = await client.delete(f"/api/trash/document/{doc.id}")
    assert resp.status_code == 200

    await _fresh(clean_extra)
    assert (await clean_extra.execute(select(Document).where(Document.id == doc.id))).scalars().all() == []
    assert (
        await clean_extra.execute(select(Chunk).where(Chunk.document_id == doc.id))
    ).scalars().all() == []


async def test_purge_note_cleans_wiki_links(client, clean_extra):  # noqa: F811
    """笔记彻底删除：删行 + 出链清空 + 指向它的入边按事实重解析（转「未创建」）。"""
    note = await _note(clean_extra, "被删的笔记")
    other = await _note(clean_extra, "引用者")
    clean_extra.add(
        WikiLink(
            source_type="note",
            source_id=note.id,
            kb_id=1,
            target_title="引用者",
            target_note_id=other.id,
            resolved=True,
        )
    )
    clean_extra.add(
        WikiLink(
            source_type="note",
            source_id=other.id,
            kb_id=1,
            target_title="被删的笔记",
            target_note_id=note.id,
            resolved=True,
        )
    )
    await clean_extra.commit()
    # 进回收站（本域只认「站内条目」，故直接软删）
    note.deleted_at = repo.utc_now()
    await clean_extra.commit()

    resp = await client.delete(f"/api/trash/note/{note.id}")
    assert resp.status_code == 200

    await _fresh(clean_extra)
    links = (await clean_extra.execute(select(WikiLink))).scalars().all()
    assert [link.source_id for link in links] == [other.id], "出链必须随笔记一起消失"
    assert links[0].target_note_id is None and links[0].resolved is False, (
        "指向已删笔记的入边要按当前事实重解析成「未创建」，而不是留着悬空 id"
    )


async def test_purge_404_for_live_or_missing_row(client, clean_extra):  # noqa: F811
    """彻底删除只对**站内**条目生效：在库的行 / 不存在的 id 都 404，绝不静默删库。"""
    note = await _note(clean_extra, "在库笔记")
    live = await client.delete(f"/api/trash/note/{note.id}")
    assert live.status_code == 404
    assert live.json()["detail"] == "回收站中没有这条笔记"
    await _fresh(clean_extra)
    assert (await clean_extra.execute(select(Note).where(Note.id == note.id))).scalars().all(), (
        "在库的行必须原封不动"
    )

    missing = await client.delete("/api/trash/chat/999999")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "回收站中没有这条会话"


async def test_purge_invalid_kind_422(client):
    """非法 ``kind`` → 422（路径参数是封闭 Literal；契约 §4 的全局中文文案）。"""
    resp = await client.delete("/api/trash/invalid_kind/1")
    assert resp.status_code == 422
    detail = resp.json().get("detail", "")
    assert "请求参数不正确" in detail or isinstance(detail, (list, dict))


# --- 清空 -------------------------------------------------------------------


async def test_empty_trash_purges_everything_and_reports_counts(client, clean_extra):  # noqa: F811
    """清空：三类一起彻底删除，回 ``{ok, notes, documents, chats}``；子行全清。"""
    note = await _note(clean_extra, "站内笔记", deleted=True)
    doc = await _document(clean_extra, "站内文档.md", chunks=["块"], deleted=True)
    chat = await _chat(clean_extra, "站内会话", deleted=True, messages=1)
    await _note(clean_extra, "在库笔记")  # 不在站里：绝不能被清掉
    kept = await _chat(clean_extra, "在库会话")

    resp = await client.post("/api/trash/empty")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "notes": 1, "documents": 1, "chats": 1}

    assert (await client.get("/api/trash")).json() == {"notes": [], "documents": [], "chats": []}
    await _fresh(clean_extra)
    for model, item_id in ((Note, note.id), (Document, doc.id), (Chat, chat.id)):
        assert (await clean_extra.execute(select(model).where(model.id == item_id))).scalars().all() == []
    assert (
        await clean_extra.execute(select(Chunk).where(Chunk.document_id == doc.id))
    ).scalars().all() == []
    assert (
        await clean_extra.execute(select(Message).where(Message.chat_id == chat.id))
    ).scalars().all() == []
    assert (await clean_extra.execute(select(Chat).where(Chat.id == kept.id))).scalars().all(), (
        "在库会话不受影响"
    )


async def test_empty_trash_on_empty_station_is_zero(client, clean_extra):  # noqa: F811
    """空站清空：200 + 全 0（不报错、不 404）。"""
    resp = await client.post("/api/trash/empty")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "notes": 0, "documents": 0, "chats": 0}


# --- S6 · 187 补腿：二次调用（幂等边界）--------------------------------------


async def test_restore_twice_second_call_is_404(client, clean_extra):  # noqa: F811
    """S6 · 187 补腿：恢复成功后再恢复 ⇒ 404（row 已出站），文案按三类分别核对。

    既有 test_restore_404_when_not_in_trash 只覆盖「一开始就不在站里」；
    「第一次恢复成功 ⇒ 第二次必须 404」此前没有判据 —— 第二次若返回 200（幂等假成功）
    或文案串了 label，本腿必红。
    """
    made = {
        "note": await _note(clean_extra, "S6-二次恢复笔记", deleted=True),
        "document": await _document(clean_extra, "S6-二次恢复.md", deleted=True),
        "chat": await _chat(clean_extra, "S6-二次恢复会话", deleted=True),
    }
    labels = {"note": "笔记", "document": "文档", "chat": "会话"}

    for kind, row in made.items():
        first = await client.post("/api/trash/restore", json={"type": kind, "id": row.id})
        assert first.status_code == 200, (kind, first.text)
        assert first.json() == {"ok": True, "message": "已恢复"}

        await _fresh(clean_extra)
        second = await client.post("/api/trash/restore", json={"type": kind, "id": row.id})
        assert second.status_code == 404, (kind, second.text)
        assert second.json()["detail"] == f"回收站中没有这条{labels[kind]}"


async def test_purge_twice_second_call_is_404(client, clean_extra):  # noqa: F811
    """S6 · 187 补腿：彻底删除成功后再删 ⇒ 404；顺带补上 document 的 404 文案缺口。

    既有 test_purge_404_for_live_or_missing_row 只覆盖在库 note + 缺失 chat，
    document 的 purge 404 文案此前没有判据。
    """
    made = {
        "note": await _note(clean_extra, "S6-二次彻删笔记", deleted=True),
        "document": await _document(clean_extra, "S6-二次彻删.md", chunks=["块"], deleted=True),
        "chat": await _chat(clean_extra, "S6-二次彻删会话", deleted=True, messages=1),
    }
    labels = {"note": "笔记", "document": "文档", "chat": "会话"}

    for kind, row in made.items():
        first = await client.delete(f"/api/trash/{kind}/{row.id}")
        assert first.status_code == 200, (kind, first.text)
        assert first.json() == {"ok": True, "message": "已彻底删除"}

        await _fresh(clean_extra)
        second = await client.delete(f"/api/trash/{kind}/{row.id}")
        assert second.status_code == 404, (kind, second.text)
        assert second.json()["detail"] == f"回收站中没有这条{labels[kind]}"


async def test_empty_trash_twice_second_call_reports_all_zero(client, clean_extra):  # noqa: F811
    """S6 · 187 补腿：连续清空两次 —— 第二次 200 且三类计数**全 0**（ok 仍为真）。

    「全 0」只指 notes / documents / chats 三个计数；响应键集也要精确
    （多一个键 / 少一个键即红）。在库的行两次都不受影响。
    """
    station = {
        "note": await _note(clean_extra, "S6-清空一次笔记", deleted=True),
        "document": await _document(clean_extra, "S6-清空一次.md", chunks=["块"], deleted=True),
        "chat": await _chat(clean_extra, "S6-清空一次会话", deleted=True, messages=1),
    }
    kept = await _note(clean_extra, "S6-清空后仍在库笔记")

    first = await client.post("/api/trash/empty")
    assert first.status_code == 200, first.text
    assert first.json() == {"ok": True, "notes": 1, "documents": 1, "chats": 1}

    await _fresh(clean_extra)
    second = await client.post("/api/trash/empty")
    assert second.status_code == 200, second.text
    body = second.json()
    assert set(body) == {"ok", "notes", "documents", "chats"}
    assert body["ok"] is True
    assert body["notes"] == 0 and body["documents"] == 0 and body["chats"] == 0

    assert (await client.get("/api/trash")).json() == {"notes": [], "documents": [], "chats": []}
    await _fresh(clean_extra)
    await clean_extra.refresh(kept)
    assert kept.deleted_at is None, "在库的行两次清空都不许被顺手清掉"
    for model, row in ((Note, station["note"]), (Document, station["document"]), (Chat, station["chat"])):
        assert (await clean_extra.execute(select(model).where(model.id == row.id))).scalars().all() == []
