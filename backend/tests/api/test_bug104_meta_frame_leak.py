"""回归修复 载体：``meta`` 帧的 yield 挂在 try/finally 之外 ⇒ 停止登记条目永久泄漏。

缺陷结构（修复前）：``_events()`` 先 ``yield sse(meta)`` 再 ``try:``——客户端在 meta 帧后、
下一帧前断开（「等首字时关页」）时，Starlette 1.6 的 ``StreamingResponse`` 断开**从不
``aclose()`` body_iterator**，异步生成器悬挂在 meta yield 上等 GC；GC 终结器注入的
``GeneratorExit`` 落点在 try 之外 ⇒ ``finally`` 里 ``stop_registry.unregister`` 不执行
⇒ 注册表条目永久泄漏（``active_count()`` 只增不减，同一 ``request_id`` 的
``/chat/stop`` 从此永远回 ``{"stopped": true}``）。

载体判别力（进程内、零时序依赖）：直接驱动端点返回的 ``StreamingResponse.body_iterator``，
只取 meta 帧**然后丢弃全部引用**（显式不 ``aclose()``——照 Starlette 1.6 的真实行为），
让 GC 终结器把 ``GeneratorExit`` 注进悬挂点。修复前注入点在 try 外 ⇒ 登记仍在；
修复后注入点在 try 内 ⇒ finally 执行、登记注销。
"""

from __future__ import annotations

import asyncio
import gc
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import chat_routes, chat_stop
from app.api.chat_schemas import ChatRequest
from app.models import Chat
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401


class _OneTokenModel:
    """一个增量后自然到头的假模型（``stream()`` 返回异步迭代器——与 ``stream_tokens`` 同型）。"""

    async def stream(self, messages, system_prompt=None):  # noqa: ANN001
        return self._iterate()

    async def _iterate(self):
        yield SimpleNamespace(type="text_delta", delta="唯一一段正文。")

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


def _payload(request_id: str) -> ChatRequest:
    return ChatRequest(message="向量库用什么存储？", request_id=request_id)


async def _drain_frames_until_generator_finalized() -> None:
    """丢弃生成器的全部引用并等 GC 终结器把 GeneratorExit 注进悬挂点。

    Starlette 1.6 断开时从不 aclose body_iterator ⇒ 生成器留在悬挂的 yield 上；
    引用清零后 CPython/asyncio 的 asyncgen 终结钩子会在事件循环上调度 ``aclose()``。
    这里用几次 ``sleep(0)`` 让那个回调确定跑完（零时序依赖：钩子是确定性的调度）。
    """
    gc.collect()
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_abandon_at_meta_frame_releases_stop_registration(db, monkeypatch):
    """meta 帧后丢弃生成器（不 aclose）⇒ 注销必须仍发生（修复前：永久泄漏）。"""
    _install(monkeypatch, _OneTokenModel())

    response = await chat_routes.chat_stream(_payload("req-bug104"), db)
    gen = response.body_iterator
    meta = await gen.__anext__()
    assert json.loads(meta.decode("utf-8").removeprefix("data: "))["type"] == "meta"
    assert chat_stop.is_registered("req-bug104") is True

    # 照 Starlette 1.6 的真实断开行为：**从不 aclose**，直接丢弃全部引用
    # （StreamingResponse 本体也攥着 body_iterator，必须一并丢弃）。
    del response
    del gen
    await _drain_frames_until_generator_finalized()

    assert chat_stop.is_registered("req-bug104") is False
    assert chat_stop.active_count() == 0


@pytest.mark.asyncio
async def test_meta_frame_moving_into_try_keeps_normal_closeout(db, monkeypatch):
    """回归钉：meta 帧挪进 try 不得改变正常收尾（done + 注销 + 完整落库）。"""
    _install(monkeypatch, _OneTokenModel())

    response = await chat_routes.chat_stream(_payload("req-bug104b"), db)
    gen = response.body_iterator
    kinds = []
    while True:
        try:
            frame = await gen.__anext__()
        except StopAsyncIteration:
            break
        kinds.append(json.loads(frame.decode("utf-8").removeprefix("data: "))["type"])
    await gen.aclose()

    assert kinds == ["meta", "token", "done"]
    assert chat_stop.is_registered("req-bug104b") is False

    from app.models import Message

    messages = list((await db.execute(select(Message).order_by(Message.id))).scalars().all())
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content == "唯一一段正文。"
    assert messages[1].kind != "stopped"
