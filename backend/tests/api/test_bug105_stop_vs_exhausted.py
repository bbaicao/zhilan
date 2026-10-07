"""回归修复 载体：停止信号与流耗尽/末增量**同批**就绪 ⇒ 完整回答被误标 ``stopped``。

缺陷（修复前）：``if stop_wait in done_set: stopped = True`` **无条件**成立——包括
``getter`` 恰在同批抛 ``StopAsyncIteration``（流已自然到头）的情形。后果：完整正文以
``kind="stopped"``、``refs=[]``、``answer_source=NULL`` 落库，不发 ``done`` 发 ``stopped``，
用量 / 记忆抽取 / 缓存写回全部跳过，``clarify_rounds`` 不清零。

判别力（进程内、零时序依赖）：直接驱动 ``StreamingResponse.body_iterator`` 逐帧取帧。

- 腿①（缺陷本体）：模型恰好一个增量后自然到头；在 producer 悬挂在**最后一个 token 的
  yield 上**时置位停止信号 ⇒ 下一轮 ``asyncio.wait`` 里 ``stop_wait``（已置位）与
  ``getter``（对已耗尽源的 ``__anext__``，首个执行步即 ``StopAsyncIteration``）必然
  **同批**就绪 ⇒ 修复后必须按正常完成收尾（``done`` + 完整落库）；
- 腿②（反向钉）：流还有未取增量（第二个 token 带 50ms 延迟）时停止 ⇒ 修复后**仍走**
  ``stopped``（中途停止语义不变，不许过度矫正）。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import chat_routes, chat_stop
from app.api.chat_schemas import ChatRequest
from app.models import Message
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401


class _ScriptedModel:
    """按脚本产 ``text_delta`` 的假模型；``delay`` 控制相邻增量间隔（腿②用）。"""

    def __init__(self, parts: list[str], delay: float = 0.0) -> None:
        self.parts = list(parts)
        self.delay = delay

    async def stream(self, messages, system_prompt=None):  # noqa: ANN001
        return self._iterate()

    async def _iterate(self):
        for index, part in enumerate(self.parts):
            if index and self.delay:
                await asyncio.sleep(self.delay)
            yield SimpleNamespace(type="text_delta", delta=part)

    async def generate(self, messages, system_prompt=None, max_output_tokens=None):  # noqa: ANN001
        raise AssertionError("流式载体不应走同步生成")


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
        (
            await db.execute(select(Message).where(Message.chat_id == chat_id).order_by(Message.id))
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_stop_signal_racing_stream_exhaustion_finishes_normally(db, monkeypatch):
    """腿①：停止与流耗尽同批就绪 ⇒ 正常完成收尾（done + 完整落库），不误标 stopped。"""
    _install(monkeypatch, _ScriptedModel(["唯一一段正文。"]))

    response = await chat_routes.chat_stream(
        ChatRequest(message="向量库用什么存储？", request_id="req-bug105"), db
    )
    gen = response.body_iterator

    assert _frame(await gen.__anext__())["type"] == "meta"
    token = _frame(await gen.__anext__())
    assert token["type"] == "token" and token["text"] == "唯一一段正文。"

    # producer 此刻悬挂在最后一个 token 的 yield 上（源已耗尽但尚未被 wait 观察到）——
    # 这正是缺陷描述的窗口。此刻置位停止信号 ⇒ 下一轮 wait 两者同批就绪。
    assert chat_stop.request_stop("req-bug105") is True

    last = _frame(await gen.__anext__())
    assert last["type"] == "done", f"流已自然到头，停止不得把它误标成 {last['type']}"
    await gen.aclose()
    assert chat_stop.is_registered("req-bug105") is False

    rows = await _messages(db, last["chat_id"])
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[1].kind != "stopped"
    assert rows[1].content == "唯一一段正文。"


@pytest.mark.asyncio
async def test_stop_while_stream_still_has_pending_chunks_stays_stopped(db, monkeypatch):
    """腿②（反向钉）：流还有未取增量时停止 ⇒ 仍走 stopped（中途停止语义不变）。"""
    _install(monkeypatch, _ScriptedModel(["第一段。", "第二段。"], delay=0.05))

    response = await chat_routes.chat_stream(
        ChatRequest(message="向量库用什么存储？", request_id="req-bug105b"), db
    )
    gen = response.body_iterator

    assert _frame(await gen.__anext__())["type"] == "meta"
    token = _frame(await gen.__anext__())
    assert token["type"] == "token" and token["text"] == "第一段。"

    # producer 悬挂在第一个 token 的 yield 上，第二个 token 还在 50ms 延迟之后
    # ⇒ 下一轮 wait 里 getter 必然未就绪，只有 stop_wait 就绪 ⇒ 中途停止。
    assert chat_stop.request_stop("req-bug105b") is True

    last = _frame(await gen.__anext__())
    assert last["type"] == "stopped"
    await gen.aclose()
    assert chat_stop.is_registered("req-bug105b") is False

    rows = await _messages(db, last["chat_id"])
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[1].kind == "stopped"
    assert rows[1].content == "第一段。"  # 已生成的部分留档，第二个增量未交付
