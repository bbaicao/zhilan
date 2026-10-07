"""OPT2: final model state closes out the same way through sync, stream and Agent."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import chat_cache, chat_routes, chat_stop
from app.core.config import settings
from app.generation import answer
from app.models import Message
from app.retrieval.rerank import RerankStats
from app.retrieval.types import number, render_materials
from app.session import events
from tests.api.test_bug153_stream_error_semantics import _FakeStream, _final, _install
from tests.api.test_chat_agent import _seed_materials
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401


class ReplyModel:
    def __init__(self, text, reason="stop"):
        self.text, self.reason = text, reason
        self.spec = SimpleNamespace(context_window=65536)
        self.calls = []

    def final(self):
        final = _final(self.reason)
        final.content = [SimpleNamespace(type="text", text=self.text)]
        return final

    async def generate(self, messages, system_prompt=None, max_output_tokens=None):
        self.calls.append((system_prompt, messages))
        return self.final()

    async def stream(self, messages, system_prompt=None):
        self.calls.append((system_prompt, messages))
        delta = [SimpleNamespace(type="text_delta", delta=self.text)] if self.text else []
        return _FakeStream(delta, self.final())


@pytest.fixture(autouse=True)
def clear_stop_registry():
    chat_stop.reset()
    yield
    chat_stop.reset()


async def install_materials(db, monkeypatch):
    materials = await _seed_materials(db, ["字段示例，不是系统错误。"], filename="OPT2.md")
    number(materials)

    async def retrieve(_db, query, filters, *, stats=None):
        return list(materials), render_materials(materials), None

    monkeypatch.setattr(chat_routes, "retrieve_materials", retrieve)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db"),
    )
    return materials


def install_agent_observation(monkeypatch, materials):
    """Use the real route Agent closeout, replacing only the decision/tool source."""
    async def source(turn, **kwargs):
        turn.materials = list(materials)
        turn.tool_context = [SimpleNamespace(
            tool="mcp:fixture:read", tool_call_id="opt2-read", ok=True,
            text="工具已返回字段说明。", collected_at="2026-10-07 00:00:00",
        )]
        yield "agent", turn.add_step("tool", "已读取工具资料", tool="mcp:fixture:read", ok=True)

    monkeypatch.setattr(chat_routes.agent_runner, "stream_agent_turn", source)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["sync", "stream", "agent"])
@pytest.mark.parametrize(
    "text,reason,status",
    [
        ("", "stop", "failed"),
        (" \n\t ", "stop", "failed"),
        ("生成失败，请稍后重试。是资料里的示例；尚未配置和 error 只是字段。[1]",
         "stop", "completed"),
        ("上游错误前的部分正文。[1]", "error", "failed"),
        ("被长度限制截断的部分正文。[1]", "length", "incomplete"),
        ("用户停止前的部分正文。[1]", "aborted", "stopped"),
        ("", "aborted", "stopped"),
    ],
    ids=["empty", "whitespace", "error-words", "upstream-error", "length", "abort-partial", "abort-empty"],
)
async def test_final_state_controls_http_persistence_and_side_effects(
    client, db, monkeypatch, path, text, reason, status,
):
    model = ReplyModel(text, reason)
    _install(monkeypatch, model)
    materials = await install_materials(db, monkeypatch)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    scheduled = {"cache": [], "memory": []}
    monkeypatch.setattr(
        chat_cache, "schedule_cache_write",
        lambda *args, **kwargs: scheduled["cache"].append((args, kwargs)),
    )
    monkeypatch.setattr(
        chat_routes.memory_mod, "schedule_extract",
        lambda **kwargs: scheduled["memory"].append(kwargs),
    )
    if path == "agent":
        install_agent_observation(monkeypatch, materials)
    question = "说明这份记录中的字段含义。"
    response = await client.post(
        "/api/chat" if path == "sync" else "/api/chat/stream",
        json={"message": question, "agent_mode": "react" if path == "agent" else "off"},
    )
    assert response.status_code == 200, response.text
    if path == "sync":
        body = response.json()
        chat_id = body["chat_id"]
        if status == "stopped":
            assert body["kind"] == "stopped"
            assert body["answer_source"] is None
        if status == "failed":
            assert body["message"].strip() and body["message"] != text
        else:
            assert body["message"] == text
        refs = body["refs"]
    else:
        frames = parse_sse(response.text)
        chat_id = frames[0]["chat_id"]
        assert frames[-1]["type"] == {
            "failed": "error", "stopped": "stopped",
        }.get(status, "done"), frames
        assert len([f for f in frames if f["type"] in {"done", "error", "stopped"}]) == 1
        if status == "failed":
            assert frames[-1]["detail"].strip()
        refs = frames[-1].get("refs", [])
        if path == "agent":
            assert any(f["type"] == "agent" for f in frames)
            assert answer.TOOL_CONTEXT_HEADER in str(model.calls[-1][1])

    await db.rollback()
    rows = list((await db.execute(
        select(Message).where(Message.chat_id == chat_id).order_by(Message.id),
    )).scalars().all())
    keep_assistant = status != "failed" and bool(text.strip())
    assert [r.role for r in rows] == ["user"] + (["assistant"] if keep_assistant else [])
    assert rows[0].content == question
    if keep_assistant:
        assert rows[-1].content == text
    if status in {"failed", "stopped"}:
        assert refs == []
    else:
        assert [ref["ordinal"] for ref in refs] == [1]
    history = await events.list_events(db, chat_id)
    attempt = [e.data for e in history if e.type == "assistant/attempt"][-1]
    assert attempt["generation_status"] == status
    assert attempt["finish_reason"] == reason
    assert attempt["outcome"] == {
        "failed": "aborted", "stopped": "interrupted", "incomplete": "interrupted",
        "completed": "committed",
    }[status]
    if status == "completed":
        assert len(scheduled["memory"]) == 1
        assert len(scheduled["cache"]) == (0 if path == "agent" else 1)
    else:
        assert scheduled == {"cache": [], "memory": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_image_question_survives_internal_threshold_and_direct_disabled(
    client, db, monkeypatch, stream,
):
    model = ReplyModel("图中有一张字段表。")
    _install(monkeypatch, model)
    monkeypatch.setattr(chat_routes, "resolve_vision_model", lambda: model)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db"),
    )
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", False)

    async def empty_retrieve(_db, query, filters, *, stats=None):
        return [], "", RerankStats(status="applied", candidates=1, kept=0, removed=1)

    monkeypatch.setattr(chat_routes, "retrieve_materials", empty_retrieve)
    response = await client.post(
        "/api/chat/stream" if stream else "/api/chat",
        json={"message": "解释图中的字段表。", "images": ["data:image/png;base64,AAAA"]},
    )
    assert response.status_code == 200, response.text
    body = parse_sse(response.text)[-1] if stream else response.json()
    assert body["kind"] == "direct" and body["refs"] == []
    if stream:
        assert body["type"] == "done"
        assert "图中有一张字段表。" in "".join(
            f["text"] for f in parse_sse(response.text) if f["type"] == "token"
        )
    else:
        assert "图中有一张字段表。" in body["message"]
    assert len(model.calls) == 1
    assert model.calls[0][0] == answer.VISION_UNNUMBERED_SYSTEM_PROMPT
    assert [p.type for p in model.calls[0][1][-1].content] == ["text", "image"]
    await db.rollback()
    rows = list((await db.execute(
        select(Message).where(Message.chat_id == body["chat_id"]).order_by(Message.id),
    )).scalars().all())
    assert [r.role for r in rows] == ["user", "assistant"]
    assert rows[0].images == ["data:image/png;base64,AAAA"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_blank_stopped_answer_cannot_persist_only_a_display_prefix(
    client, db, monkeypatch, stream,
):
    _install(monkeypatch, ReplyModel(" \t\n\u3000", "aborted"))
    original = chat_routes.build_turn_plan

    async def with_prefix(*args, **kwargs):
        plan = await original(*args, **kwargs)
        plan.assume = True
        assert chat_routes.get_turn_prefix(plan, "有效部分正文")
        return plan

    monkeypatch.setattr(chat_routes, "build_turn_plan", with_prefix)
    response = await client.post(
        "/api/chat/stream" if stream else "/api/chat",
        json={"message": "解释字段含义。"},
    )
    assert response.status_code == 200, response.text
    if stream:
        frames = parse_sse(response.text)
        assert frames[-1]["type"] == "stopped"
        chat_id = frames[0]["chat_id"]
    else:
        body = response.json()
        assert body["kind"] == "stopped" and body["answer_source"] is None
        assert not body["message"].strip()
        chat_id = body["chat_id"]
    await db.rollback()
    rows = list((await db.execute(
        select(Message).where(Message.chat_id == chat_id).order_by(Message.id),
    )).scalars().all())
    assert [r.role for r in rows] == ["user"]
