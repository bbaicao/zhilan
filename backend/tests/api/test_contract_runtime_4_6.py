"""Runtime counterparts for the frozen note-generation stream."""

import json

import pytest
from sqlalchemy import func, select

from app.api import chat_routes
from app.models import Note
from tests.api.test_chat_contract import client, faux_model, parse_sse  # noqa: F401
from tests.retrieval.conftest import seed_document


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_note_stream_real_wiring(client, db, monkeypatch, failure):
    doc = await seed_document(db, ["项目于2024-03-15启动，预算120万元。"], filename="contract.md")
    raw = json.dumps({"title": "项目", "content": "项目于2024-03-15启动，预算120万元。",
                      "category": "合同", "tags": ["项目"]}, ensure_ascii=False)

    class BrokenModel:
        async def stream(self, *args, **kwargs):
            raise TimeoutError("injected note timeout")

    model = BrokenModel() if failure else faux_model(raw)
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    response = await client.post(f"/api/documents/{doc.id}/generate-note/stream")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    frames = parse_sse(response.text)
    assert frames[0]["type"] == "status" and frames[0]["detail"]
    terminal = [f for f in frames if f["type"] in {"done", "error"}]
    assert len(terminal) == 1
    if failure:
        assert terminal[0] == {"type": "error", "detail": "笔记生成失败，请稍后重试。（TimeoutError）"}
    else:
        done = terminal[0]
        assert done["type"] == "done"
        assert set(done) == {"type", "title", "content", "category", "tags",
                             "source_document_id", "source_filename", "completeness"}
        assert done["source_document_id"] == doc.id and done["source_filename"] == doc.filename
        assert done["title"] == "项目" and done["content"] == json.loads(raw)["content"]
        assert "".join(f["text"] for f in frames if f["type"] == "token") == raw
        assert {"complete", "checker", "checked", "missing"} == set(done["completeness"])
        assert done["completeness"]["checker"] == "regex"
        assert done["completeness"]["complete"] is True and done["completeness"]["missing"] == []
    assert await db.scalar(select(func.count()).select_from(Note)) == 0, "draft generation must not save a note"
