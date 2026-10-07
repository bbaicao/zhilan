"""SSE 全量事件与停止机制的端到端用例（阶段 3 · 条目 3.4）。

覆盖：``reasoning`` 帧与 ``done.reasoning`` 全文、``stopped`` 帧 + 部分留档、
**极早期停止零字不落消息**、``/api/chat/stop`` 的幂等语义、模型侧异常（超时/鉴权/未配置）
映射成固定中文 ``error`` 提示、以及停止登记在流结束时被注销。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import chat_stop, chat_routes
from app.core.errors import AUTH_HINT, TIMEOUT_HINT
from app.models import Message
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401
from tests.api.test_chat_retrieval_wiring import _install
from tests.api.test_bug153_stream_error_semantics import _FakeStream, _FailingModel, _final
from tests.retrieval.conftest import seed_document

CHUNK_TEXT = "向量库使用 Qdrant 内嵌模式，向量化由上游接口完成。"


class _StreamModel:
    """可注入延迟与事件类型的流式假模型（只实现 ``stream``，`stream_tokens` 只用它）。"""

    def __init__(self, parts, *, kinds=None, delay: float = 0.0, first_delay: float = 0.0):
        self.parts = list(parts)
        self.kinds = list(kinds or ["text_delta"] * len(self.parts))
        self.delay = delay
        self.first_delay = first_delay

    async def stream(self, messages, system_prompt=None):  # noqa: ANN001
        return self._iterate()

    async def _iterate(self):
        if self.first_delay:
            await asyncio.sleep(self.first_delay)
        for index, part in enumerate(self.parts):
            if index and self.delay:
                await asyncio.sleep(self.delay)
            yield SimpleNamespace(type=self.kinds[index], delta=part)


def _install_stream(monkeypatch, model) -> None:
    """装流式假模型 + 路由分类器（判「库内」：这样有资料的用例才走 `normal` 分路）。

    没有语料时「库内未命中」自然落到 `direct`（既有语义），所以异常/停止用例照样成立。
    """
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )
    from app.core.config import settings

    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)


async def _messages(db, chat_id: int) -> list[Message]:
    return list(
        (
            await db.execute(
                select(Message).where(Message.chat_id == chat_id).order_by(Message.id)
            )
        )
        .scalars()
        .all()
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    chat_stop.reset()
    yield
    chat_stop.reset()


@pytest.mark.asyncio
async def test_reasoning_frames_and_done_carry_thought_text(client, db, monkeypatch):
    await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    model = _StreamModel(
        ["先想一下。", "据资料 [1]，", "向量库用 Qdrant。"],
        kinds=["thinking_delta", "text_delta", "text_delta"],
    )
    _install_stream(monkeypatch, model)

    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "向量库用什么存储？"}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")

    events = parse_sse(raw)
    assert [event["type"] for event in events] == ["meta", "reasoning", "token", "token", "done"]
    assert events[1]["text"] == "先想一下。"
    done = events[-1]
    assert done["reasoning"] == "先想一下。"  # done 里是**思考全文**
    assert done["kind"] == "normal"
    assert done["answer_source"] == "kb"
    assert len(done["refs"]) == 1

    rows = await _messages(db, done["chat_id"])
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[1].reasoning == "先想一下。"
    assert rows[1].content == "据资料 [1]，向量库用 Qdrant。"


@pytest.mark.asyncio
async def test_stop_mid_stream_emits_stopped_and_keeps_partial_answer(client, db, monkeypatch):
    await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    # 长流 + 每个增量 50ms：停止点（120ms）落在**流的中段**，留足调度余量（不靠精确时序）
    model = _StreamModel([f"第{index}段。" for index in range(20)], delay=0.05)
    _install_stream(monkeypatch, model)
    request_id = "req-stop-mid"

    async def _stop_soon():
        await asyncio.sleep(0.12)
        return await client.post("/api/chat/stop", json={"request_id": request_id})

    stopper = asyncio.create_task(_stop_soon())
    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "向量库用什么存储？", "request_id": request_id}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")
    stop_response = await stopper

    assert stop_response.status_code == 200
    assert stop_response.json() == {"stopped": True}

    events = parse_sse(raw)
    kinds = [event["type"] for event in events]
    assert kinds[-1] == "stopped"
    assert "done" not in kinds and "error" not in kinds
    stopped = events[-1]
    assert stopped["chat_id"] == events[0]["chat_id"]

    chat_id = stopped["chat_id"]
    rows = await _messages(db, chat_id)
    assert [row.role for row in rows] == ["user", "assistant"]
    # 已生成的部分留档；被停的回答不挂引用、answer_source 保持 NULL
    assert rows[1].kind == "stopped"
    assert rows[1].refs == []
    assert rows[1].answer_source is None
    assert rows[1].content and "第0段。" in rows[1].content
    assert rows[1].content != "".join(model.parts)  # 确实是**部分**内容，不是完整回答

    # 事件：停止留痕（stream/interrupted + turn/end kind=stopped）
    from app.session import events as event_log

    interrupted = await event_log.list_events_by_type(db, chat_id, "stream/interrupted")
    assert len(interrupted) == 1
    assert interrupted[0].data["reason"] == "stopped"
    turn_end = await event_log.list_events_by_type(db, chat_id, "turn/end")
    assert turn_end[-1].data["kind"] == "stopped"


@pytest.mark.asyncio
async def test_stop_before_any_token_leaves_no_assistant_message(client, db, monkeypatch):
    """极早期停止：一个增量都没收到 ⇒ **零字不落消息**，只留用户消息。"""
    model = _StreamModel(["永远不会到达"], first_delay=0.5)
    _install_stream(monkeypatch, model)
    request_id = "req-stop-early"

    async def _stop_soon():
        await asyncio.sleep(0.05)
        return await client.post("/api/chat/stop", json={"request_id": request_id})

    stopper = asyncio.create_task(_stop_soon())
    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "向量库用什么存储？", "request_id": request_id}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")
    await stopper

    events = parse_sse(raw)
    assert events[-1]["type"] == "stopped"
    chat_id = events[-1]["chat_id"]

    rows = await _messages(db, chat_id)
    assert [row.role for row in rows] == ["user"]  # 没有 assistant 行
    assert rows[0].content == "向量库用什么存储？"


@pytest.mark.asyncio
async def test_stop_unknown_request_id_is_not_an_error(client, db, monkeypatch):
    _install(monkeypatch, reply="好的。")

    resp = await client.post("/api/chat/stop", json={"request_id": "从未存在"})

    assert resp.status_code == 200
    assert resp.json() == {"stopped": False}


@pytest.mark.asyncio
async def test_timeout_hint_becomes_error_frame_with_fixed_text(client, db, monkeypatch):
    """模型侧异常 → ``error`` 帧 + 契约里的**固定中文提示**；不落 assistant 消息，但用户消息要留。"""
    model = _FailingModel(_FakeStream([], _final(
        "error", error_type="ProviderTimeout", error_message=TIMEOUT_HINT,
    )))
    _install_stream(monkeypatch, model)

    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "向量库用什么存储？"}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")

    events = parse_sse(raw)
    assert events[-1]["type"] == "error"
    assert events[-1] == {"type": "error", "detail": "生成失败，请稍后重试。（ProviderTimeout）"}
    assert "done" not in [event["type"] for event in events]

    chat_id = events[0]["chat_id"]
    rows = await _messages(db, chat_id)
    assert [row.role for row in rows] == ["user"]  # 提问不丢，异常不当完整回答落库


@pytest.mark.asyncio
async def test_auth_hint_is_also_mapped(client, db, monkeypatch):
    _install_stream(monkeypatch, _FailingModel(_FakeStream([], _final(
        "error", error_type="ProviderAuthError", error_message=AUTH_HINT,
    ))))

    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "向量库用什么存储？"}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")

    frames = parse_sse(raw)
    assert frames[-1] == {"type": "error", "detail": "生成失败，请稍后重试。（ProviderAuthError）"}
    assert not [frame for frame in frames if frame["type"] in {"done", "token"}]
    assert [row.role for row in await _messages(db, frames[0]["chat_id"])] == ["user"]


@pytest.mark.asyncio
async def test_stop_registry_is_cleaned_up_after_normal_stream(client, db, monkeypatch):
    _install(monkeypatch, reply="好的。")

    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "今天天气怎么样", "request_id": "req-clean"}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")

    assert parse_sse(raw)[-1]["type"] == "done"
    assert chat_stop.is_registered("req-clean") is False
    assert chat_stop.active_count() == 0
