"""U3.5：同步／SSE从原始历史消歧，只把改写用于检索。"""

import pytest

from app.api import chat_routes
from app.core.config import settings
from app.generation import answer as answer_mod
from app.retrieval.query import QUERY_REWRITE_VERSION
from app.session import service as session_service
from tests.api.test_chat_contract import client, parse_sse, use_faux


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
async def test_sync_and_stream_rewrite_search_but_answer_and_store_original(
    path, client, db, monkeypatch, use_faux,
):
    known = "查看AX-02设备，2026年10月之后，预算不超过3000元，不要翻新机。"
    question = "它的保修期限是什么？"
    chat, _, _ = await session_service.save_round(
        db, chat_id=None, question=known, answer="可能是AX-03，预计9999元。",
    )
    chat_id = chat.id
    seen = {}
    model = use_faux("保修期限以产品记录为准。")
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: model)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", False)

    async def fake_model_history(*_args, **_kwargs):
        return [{"role": "user", "content": "压缩猜测：应该买AX-03，售价9999元。"}]

    async def fake_retrieve(_db, query, filters, *, stats=None):
        seen["query"], seen["filters"], seen["stats"] = query, filters, stats
        return [], "", None

    build_messages = answer_mod.build_messages

    def capture_messages(message, *args, **kwargs):
        seen["answer_question"] = message
        return build_messages(message, *args, **kwargs)

    monkeypatch.setattr(session_service, "build_model_history", fake_model_history)
    monkeypatch.setattr(chat_routes, "retrieve_materials", fake_retrieve)
    monkeypatch.setattr(answer_mod, "build_messages", capture_messages)
    response = await client.post(path, json={
        "message": question, "chat_id": chat_id, "filters": {"kb_ids": [4], "tags": ["保修"]},
    })
    assert response.status_code == 200
    if path == "/api/chat":
        result = response.json()
    else:
        events = parse_sse(response.text)
        assert events[-1]["type"] == "done"
        result = events[-1]
    assert result["kind"] == "direct" and result["answer_source"] == "general"
    assert known in seen["query"] and question in seen["query"]
    assert "AX-03" not in seen["query"] and "9999" not in seen["query"]
    assert seen["filters"] == {"kb_ids": [4], "tags": ["保修"]}
    assert seen["answer_question"] == question
    stats = seen["stats"].payload()["query_rewrite"]
    assert stats["status"] == "applied" and stats["calls"] == 0
    assert stats["strategy_version"] == QUERY_REWRITE_VERSION
    await db.rollback()
    rows = await session_service.model_history_rows(db, chat_id)
    assert [row.content for row in rows if row.role == "user"][-1] == question


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
async def test_sync_and_stream_unresolved_long_reference_uses_existing_clarify(
    path, client, monkeypatch, use_faux,
):
    model = use_faux("你指的是哪一个设备？")
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: model)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", False)

    async def must_not_retrieve(*_args, **_kwargs):
        raise AssertionError("没有明确对象应先澄清，不能检索猜测对象")

    monkeypatch.setattr(chat_routes, "retrieve_materials", must_not_retrieve)
    response = await client.post(path, json={"message": "它是否符合预算3000元并且不要订阅服务的要求？"})
    assert response.status_code == 200
    if path == "/api/chat":
        result = response.json()
        message = result["message"]
    else:
        events = parse_sse(response.text)
        assert events[-1]["type"] == "done"
        result = events[-1]
        message = "".join(event["text"] for event in events if event["type"] == "token")
    assert result["kind"] == "clarify" and result["answer_source"] == "clarify"
    assert message == "你指的是哪一个设备？" and result["refs"] == []
