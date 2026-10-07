"""U4 same snapshot through real HTTP sync/stream and actual Faux Agent loop."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import chat_routes
from app.generation import answer, citation
from app.models import Message
from app.retrieval.types import number, render_materials
from tests.api.test_chat_agent import (
    _install_retrieval, _provider_with_tool_call, _seed_materials,
)
from tests.api.test_chat_contract import _StubClassifier, client, faux_model, parse_sse  # noqa: F401


@pytest.mark.asyncio
async def test_same_question_and_snapshot_across_three_paths(client, db, monkeypatch):
    question = "甲公司2026年付款是否批准？只答甲公司，不要换主体或省略否定。"
    materials = await _seed_materials(
        db, ["甲公司2026年付款未批准，金额20万元。", "乙公司批准30万元；不得据此推断甲公司。"],
        filename="付款记录.md",
    )
    number(materials)

    async def retrieve(_db, query, filters, *, stats=None):
        assert query == question
        assert filters["kb_ids"] == [1]
        return list(materials), render_materials(materials), None

    monkeypatch.setattr(chat_routes, "retrieve_materials", retrieve)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: None)
    monkeypatch.setattr(chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db"))
    _install_retrieval(monkeypatch, materials)
    captures = []
    original = answer.assemble_answer_prompt

    def capture(message, history=None, **kwargs):
        result = original(message, history, **kwargs)
        captures.append((message, result[0], result[1]))
        return result

    monkeypatch.setattr(answer, "assemble_answer_prompt", capture)
    results = []
    reply = "对照记录显示乙公司批准30万元。[2] 不能据此推断甲公司获批。"
    for path, mode in (("/api/chat", "off"), ("/api/chat/stream", "off"), ("/api/chat/stream", "react")):
        if mode == "react":
            provider = _provider_with_tool_call(question)
            from cubeloop.providers import faux_assistant_message
            provider.append_responses([faux_assistant_message(reply)])
            model = provider.model("faux-chat")
        else:
            model = faux_model(reply)
        monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
        response = await client.post(
            path, json={"message": question, "agent_mode": mode, "filters": {"kb_ids": [1]}},
        )
        assert response.status_code == 200, response.text
        body = response.json() if mode == "off" and path == "/api/chat" else parse_sse(response.text)[-1]
        assert body["kind"] == "normal" and body["answer_source"] == "kb"
        assert [ref["ordinal"] for ref in body["refs"]] == [2]
        assert citation.resolve_citations(body.get("message", reply), body["refs"])[1] == "cited"
        results.append(body["refs"][0]["chunk_id"])
    assert len(captures) == 3 and len(set(results)) == 1
    assert all(item == captures[0] for item in captures)
    assert captures[0][0] == question
    assert answer.CITED_ANSWER_CONSTRAINTS in captures[0][1]
    assert "未批准" in str(captures[0][2]) and "[2]" in str(captures[0][2])


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_budget_overflow_uses_existing_error_closeout_and_keeps_user(client, db, monkeypatch, stream):
    model = faux_model("这个回答不应被请求")
    model.spec.context_window = 4096
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: None)
    monkeypatch.setattr(chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external"))
    question = "完整保留本轮限制，不得删掉否定。" * 500
    response = await client.post("/api/chat/stream" if stream else "/api/chat", json={"message": question})
    assert response.status_code == 200, response.text
    if stream:
        events = parse_sse(response.text)
        assert events[-1] == {"type": "error", "detail": answer.CONTEXT_BUDGET_HINT}
        assert not any(event["type"] == "done" for event in events)
    else:
        assert response.json()["message"] == answer.CONTEXT_BUDGET_HINT
        assert response.json()["refs"] == []
    rows = (await db.execute(select(Message).order_by(Message.id))).scalars().all()
    assert [(row.role, row.content) for row in rows] == [("user", question)]


def test_external_context_omits_candidates_but_keeps_tool_channel():
    plan = SimpleNamespace(kind="direct", materials=[object()], materials_text="[1] 私有候选",
                           tool_context=[SimpleNamespace(tool="clock", text="工具结果", ok=True)])
    context = chat_routes.answer_context(plan)
    assert context["materials_text"] == "" and not context["has_material"]
    assert len(context["tool_context"]) == 1


def test_observation_uses_same_sparse_ordinals_and_safe_marker_parser():
    from app.observe.citation import classify_citations, USED, UNRESOLVED
    refs = [{"ordinal": 2, "chunk_id": "b"}, {"ordinal": 4, "chunk_id": "d"}]
    events = classify_citations("[4] [1] [" + "9" * 5000 + "]", refs)
    assert events[0][0] == USED and events[0][1]["chunk_id"] == "d"
    assert [event[0] for event in events[1:]] == [UNRESOLVED, UNRESOLVED]
