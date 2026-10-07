"""条目 153（P0）载体：流式链路的**上游失败语义**——失败必须显式收尾，绝不落成「成功空回答」。

缺陷（修复前 · 实测形态）：上游整轮失败时，cubeloop 适配器先发一条 ``type="error"`` 的
**无 delta** 事件、再 ``set_result(content=[], stop_reason="error", error_message=…)``
（``cubeloop/providers/openai.py:523-555``）；而 ``stream_tokens`` 的消费循环
``if not delta: continue`` 把那条事件整条吃掉、收尾只把 ``usage`` 读走
（``app/api/chat_routes.py:1104-1112`` + ``:1130-1145``）⇒ 该轮以「零 token、raw_text=""」
走**正常完成**分支：HTTP 200 + ``done`` 帧、库里落一条**空正文** assistant 消息、
审计面无失败记录。

修复后（本文件的判据）：

- 腿①：错误事件（无 delta）⇒ 末帧 ``error``（不是 ``done``），本轮只落用户消息，
  ``turn/end`` 带 kind="error"、``assistant/attempt`` 以 aborted 收尾；
- 腿②：没有错误事件、但 ``final.stop_reason == "error"``（中途失败 ⇒ 已收到半截正文）
  ⇒ 同款 error 收尾，半截正文**不落库**；
- 腿③（反向钉）：正常轮（``stop_reason="stop"``）仍 ``done`` + 正文落库；
- 腿④（反向钉）：「长度截断」（``stop_reason="length"``）**不是**失败，不得被误判成 error。

判据只认「末帧类型 + 落库行 + 事件日志 + error 帧 detail」，不认时间、不认日志文本。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import chat_routes, chat_stop
from app.api.chat_schemas import ChatRequest
from app.models import Message
from app.session import events as event_log
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401


FAILURE_PREFIX = "生成失败，请稍后重试。"


class _FakeStream:
    """既可按事件迭代、又可 ``await result()`` 的流替身（cubeloop 适配器的双出口同款）。"""

    def __init__(self, events: list, final) -> None:
        self._events = list(events)
        self._final = final

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def result(self):
        return self._final


class _FailingModel:
    """``stream()`` 回一条「事件 + 收尾结果」的假模型；同步 ``generate()`` 不该被流式载体调用。"""

    def __init__(self, stream: _FakeStream) -> None:
        self._stream = stream

    async def stream(self, messages, system_prompt=None):  # noqa: ANN001
        return self._stream

    async def generate(self, messages, system_prompt=None, max_output_tokens=None):  # noqa: ANN001
        raise AssertionError("流式载体不应走同步生成")


def _final(stop_reason: str, *, error_message=None, error_type=None):
    return SimpleNamespace(
        stop_reason=stop_reason,
        error_message=error_message,
        error_type=error_type,
        usage=None,
        content=[],
    )


def _error_event(*, error_type: str | None = "ProviderUnavailable", error_message: str | None = None):
    return SimpleNamespace(
        type="error",
        delta=None,
        error_type=error_type,
        error_message=error_message
        if error_message is not None
        else "ProviderUnavailable: Connection error. <- APIConnectionError: Connection error.",
    )


def _install(monkeypatch, model) -> None:
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: None)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external")
    )
    from app.core.config import settings

    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)


@pytest.fixture(autouse=True)
def _clean_registry():
    chat_stop.reset()
    yield
    chat_stop.reset()


def _frame(frame: bytes) -> dict:
    return json.loads(frame.decode("utf-8").removeprefix("data: "))


async def _messages(db, chat_id: int) -> list[Message]:
    return list(
        (await db.execute(select(Message).where(Message.chat_id == chat_id).order_by(Message.id)))
        .scalars()
        .all()
    )


async def _drive(db, monkeypatch, stream: _FakeStream, request_id: str) -> list[dict]:
    """铺钉子 → 真驱动 ``chat_stream`` 的 body_iterator → 收全帧。"""
    _install(monkeypatch, _FailingModel(stream))
    response = await chat_routes.chat_stream(
        ChatRequest(message="向量库用什么存储？", request_id=request_id), db
    )
    frames: list[dict] = []
    async for raw in response.body_iterator:
        frames.append(_frame(raw))
    return frames


@pytest.mark.asyncio
async def test_error_event_without_delta_ends_stream_with_error(db, monkeypatch):
    """腿①：无 delta 的 error 事件 ⇒ 末帧 error、只落用户消息、事件日志留失败痕迹。"""
    frames = await _drive(
        db,
        monkeypatch,
        _FakeStream(
            [_error_event()],
            _final("error", error_message="ProviderUnavailable: Connection error."),
        ),
        "req-bug153a",
    )
    kinds = [f["type"] for f in frames]
    assert kinds[-1] == "error", f"上游失败必须以 error 收尾，实得帧序 {kinds}"
    assert "done" not in kinds, f"失败轮不得下发 done，实得帧序 {kinds}"
    assert frames[-1]["detail"] == f"{FAILURE_PREFIX}（ProviderUnavailable）", frames[-1]["detail"]

    chat_id = frames[0]["chat_id"]
    rows = await _messages(db, chat_id)
    assert [row.role for row in rows] == ["user"], (
        f"失败轮不得落 assistant 消息（空回答不得当成功留档），实得 {[r.role for r in rows]}"
    )
    assert rows[0].content.strip(), "失败轮必须留下用户提问"

    events = await event_log.list_events(db, chat_id)
    ends = [e for e in events if e.type == "turn/end"]
    assert ends, f"失败轮必须留 turn/end，实得事件 {[e.type for e in events]}"
    assert ends[-1].data.get("kind") == "error", ends[-1].data
    assert ends[-1].data.get("detail") == frames[-1]["detail"], ends[-1].data
    attempts = [e for e in events if e.type == "assistant/attempt"]
    assert attempts, f"失败轮必须留 assistant/attempt，实得事件 {[e.type for e in events]}"
    assert attempts[-1].data.get("outcome") == "aborted", attempts[-1].data
    assert not [e for e in events if e.type == "assistant/message"], (
        f"失败轮不得写 assistant/message，实得事件 {[e.type for e in events]}"
    )


@pytest.mark.asyncio
async def test_final_stop_reason_error_mid_stream_keeps_no_partial_answer(db, monkeypatch):
    """腿②：仅 final.stop_reason="error"（无错误事件、已收到半截正文）⇒ 仍 error，半截不落库。"""
    frames = await _drive(
        db,
        monkeypatch,
        _FakeStream(
            [SimpleNamespace(type="text_delta", delta="半截正文。")],
            _final("error", error_message="RateLimited: 429 too many requests"),
        ),
        "req-bug153b",
    )
    kinds = [f["type"] for f in frames]
    assert kinds[-1] == "error", f"stop_reason=error 必须以 error 收尾，实得帧序 {kinds}"
    assert "done" not in kinds, f"失败轮不得下发 done，实得帧序 {kinds}"
    # 类名兜底路径：error_type 缺席时从 "类名: 说明" 形态的 error_message 里取类名，
    # 但**绝不透传厂商原文**。
    assert frames[-1]["detail"] == f"{FAILURE_PREFIX}（RateLimited）", frames[-1]["detail"]

    rows = await _messages(db, frames[0]["chat_id"])
    assert [row.role for row in rows] == ["user"], (
        f"失败轮的半截正文不得当完整回答落库，实得 {[r.role for r in rows]}"
    )


@pytest.mark.asyncio
async def test_normal_stream_still_finishes_with_done(db, monkeypatch):
    """腿③（反向钉）：stop_reason="stop" 的正常轮不受影响 ⇒ done + 正文落库。"""
    frames = await _drive(
        db,
        monkeypatch,
        _FakeStream([SimpleNamespace(type="text_delta", delta="唯一一段正文。")], _final("stop")),
        "req-bug153c",
    )
    kinds = [f["type"] for f in frames]
    assert kinds[-1] == "done", f"正常轮必须以 done 收尾，实得帧序 {kinds}"
    assert "error" not in kinds, f"正常轮不得被误判成失败，实得帧序 {kinds}"
    rows = await _messages(db, frames[-1]["chat_id"])
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[1].content == "唯一一段正文。", rows[1].content


@pytest.mark.asyncio
async def test_length_stop_reason_is_not_treated_as_failure(db, monkeypatch):
    """腿④（反向钉）：长度截断（stop_reason="length"）不是失败 ⇒ 仍 done + 正文落库。"""
    frames = await _drive(
        db,
        monkeypatch,
        _FakeStream([SimpleNamespace(type="text_delta", delta="被截断的正文。")], _final("length")),
        "req-bug153d",
    )
    kinds = [f["type"] for f in frames]
    assert kinds[-1] == "done", f"截断轮必须以 done 收尾（不是 error），实得帧序 {kinds}"
    assert "error" not in kinds, f"非 error 的 stop_reason 不得被误判成失败，实得帧序 {kinds}"
    rows = await _messages(db, frames[-1]["chat_id"])
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[1].content == "被截断的正文。"
