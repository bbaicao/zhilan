"""U1.7：两类来源同入口维护，业务提交不被派生失败反噬，失败重试不重复建边。"""

import pytest
from sqlalchemy import select

from app.core import derived_counters
from app.models import Document, Note, WikiLink
from app.pipeline import knowledge_graph, wikilink
from app.session import note_service, repository
from tests.api.test_chat_contract import client  # noqa: F401


async def _source(db, kind, title, body):
    row = (
        Note(title=title, content=body, kb_id=1)
        if kind == "note"
        else Document(
            filename=f"{title}.md", file_type="md", status="ready", index_status="ready",
            raw_content=body, kb_id=1,
        )
    )
    db.add(row)
    await db.commit()
    return row


async def _edges(db, kind, sid):
    return list(
        (
            await db.execute(
                select(WikiLink).where(
                    WikiLink.source_type == kind, WikiLink.source_id == sid,
                ).order_by(WikiLink.target_title)
            )
        ).scalars().all()
    )


def _state(rows):
    return [
        (row.target_title, row.target_note_id, row.target_document_id, row.resolved)
        for row in rows
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["note", "document"])
async def test_create_update_rename_soft_delete_restore_and_hard_delete(db, kind):
    watcher = await _source(db, "note", "Watcher", "[[Future]]")
    watcher_id = int(watcher.id)
    assert await wikilink.maintain_source_links(db, "note", watcher_id) is True
    assert _state(await _edges(db, "note", watcher_id)) == [("Future", None, None, False)]

    target = await _source(db, kind, "Future", "[[Watcher]]")
    target_id = int(target.id)
    assert await wikilink.maintain_source_links(db, kind, target_id, text="[[Watcher]]") is True
    target_identity = (target_id, None) if kind == "note" else (None, target_id)
    assert _state(await _edges(db, "note", watcher_id)) == [("Future", *target_identity, True)]
    assert _state(await _edges(db, kind, target_id)) == [("Watcher", watcher_id, None, True)]

    if kind == "note":
        await note_service.update_note(db, target_id, title="After", content="[[Alternative]]")
    else:
        target.filename = "After.md"
        target.raw_content = "[[Alternative]]"
        await db.commit()
        assert await wikilink.maintain_source_links(
            db, kind, target_id, text="[[Alternative]]", old_title="Future",
        ) is True
    assert _state(await _edges(db, "note", watcher_id)) == [("Future", None, None, False)]
    assert _state(await _edges(db, kind, target_id)) == [("Alternative", None, None, False)]

    await note_service.update_note(db, watcher_id, content="[[After]]")
    assert _state(await _edges(db, "note", watcher_id)) == [("After", *target_identity, True)]
    if kind == "note":
        await note_service.soft_delete_note(db, target_id)
    else:
        target.deleted_at = repository.utc_now()
        await db.commit()
        assert await wikilink.maintain_source_links(db, kind, target_id) is True
    assert _state(await _edges(db, "note", watcher_id)) == [("After", None, None, False)]
    assert _state(await _edges(db, kind, target_id)) == [("Alternative", None, None, False)]
    graph = await knowledge_graph.build_graph(db)
    assert not any(node["title"] == "After" for node in graph["nodes"]), "软删目标不转成同库幽灵"

    target.deleted_at = None
    await db.commit()
    assert await wikilink.maintain_source_links(db, kind, target_id) is True
    assert _state(await _edges(db, "note", watcher_id)) == [("After", *target_identity, True)]
    assert _state(await _edges(db, kind, target_id)) == [("Alternative", None, None, False)]

    alternative = await note_service.create_note(db, title="Alternative", content="正文")
    assert _state(await _edges(db, kind, target_id)) == [
        ("Alternative", alternative.id, None, True),
    ], "后创建的目标须反向补齐原有文档或笔记出链"

    await db.delete(target)
    await db.commit()
    assert await wikilink.remove_links_for_deleted(db, kind, target_id) is True
    assert await _edges(db, kind, target_id) == []
    assert _state(await _edges(db, "note", watcher_id)) == [("After", None, None, False)]
    assert await wikilink.remove_links_for_deleted(db, kind, target_id) is True
    assert await _edges(db, kind, target_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["note", "document"])
async def test_failed_derived_write_reports_false_preserves_save_and_retries(db, monkeypatch, kind):
    monkeypatch.setattr(derived_counters, "_COUNTERS", {})
    watcher = await note_service.create_note(db, title="Watcher", content="正文")
    source = await _source(db, kind, "Source", "[[Watcher]]")
    sid, watcher_id = int(source.id), int(watcher.id)
    assert await wikilink.maintain_source_links(db, kind, sid, text="[[Watcher]]") is True
    before = _state(await _edges(db, kind, sid))

    if kind == "note":
        source.content = "[[Missing]]"
    else:
        source.raw_content = "[[Missing]]"
    await db.commit()
    original = wikilink.rebuild_source_links

    async def fail_after_write(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("U1.7 derived write failure")

    monkeypatch.setattr(wikilink, "rebuild_source_links", fail_after_write)
    assert await wikilink.maintain_source_links(db, kind, sid, text="[[Missing]]") is False
    assert source.id == sid, "回滚派生写入后，已提交业务行仍能正常返回"
    assert (source.content if kind == "note" else source.raw_content) == "[[Missing]]"
    assert _state(await _edges(db, kind, sid)) == before == [("Watcher", watcher_id, None, True)]
    assert derived_counters.failure_counts() == {
        "note_links" if kind == "note" else "doc_links": 1,
    }

    monkeypatch.setattr(wikilink, "rebuild_source_links", original)
    assert await wikilink.maintain_source_links(db, kind, sid, text="[[Missing]]") is True
    after = _state(await _edges(db, kind, sid))
    assert after == [("Missing", None, None, False)]
    assert await wikilink.maintain_source_links(db, kind, sid, text="[[Missing]]") is True
    assert _state(await _edges(db, kind, sid)) == after


@pytest.mark.asyncio
async def test_note_save_response_survives_derived_rollback(db, monkeypatch):
    monkeypatch.setattr(derived_counters, "_COUNTERS", {})
    source = await note_service.create_note(db, title="Source", content="旧正文")
    sid = int(source.id)

    async def fail(*args, **kwargs):
        raise RuntimeError("U1.7 note link failure")

    monkeypatch.setattr(wikilink, "rebuild_source_links", fail)
    saved = await note_service.update_note(db, sid, content="新正文")
    assert saved is not None and saved.id == sid and saved.content == "新正文"
    assert derived_counters.failure_counts() == {"note_links": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["note", "document"])
async def test_actual_delete_restore_and_purge_endpoints_maintain_edges(db, client, kind):
    watcher = await _source(db, "note", "Watcher", "[[Target]]")
    target = await _source(db, kind, "Target", "[[Watcher]]")
    watcher_id, target_id = int(watcher.id), int(target.id)
    await wikilink.maintain_source_links(db, "note", watcher_id)
    await wikilink.maintain_source_links(db, kind, target_id, text="[[Watcher]]")
    identity = (target_id, None) if kind == "note" else (None, target_id)
    assert _state(await _edges(db, "note", watcher_id)) == [("Target", *identity, True)]
    await db.commit()

    response = await client.delete(f"/api/{'notes' if kind == 'note' else 'documents'}/{target_id}")
    assert response.status_code == 200, response.text
    await db.rollback()
    assert _state(await _edges(db, "note", watcher_id)) == [("Target", None, None, False)]
    await db.commit()

    response = await client.post("/api/trash/restore", json={"type": kind, "id": target_id})
    assert response.status_code == 200, response.text
    await db.rollback()
    assert _state(await _edges(db, "note", watcher_id)) == [("Target", *identity, True)]
    assert _state(await _edges(db, kind, target_id)) == [("Watcher", watcher_id, None, True)]
    await db.commit()

    response = await client.delete(f"/api/{'notes' if kind == 'note' else 'documents'}/{target_id}")
    assert response.status_code == 200, response.text
    response = await client.delete(f"/api/trash/{kind}/{target_id}")
    assert response.status_code == 200, response.text
    await db.rollback()
    assert await _edges(db, kind, target_id) == []
    assert _state(await _edges(db, "note", watcher_id)) == [("Target", None, None, False)]
