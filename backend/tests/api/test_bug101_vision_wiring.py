"""回归修复（2026-09-27 业主真机反馈）HTTP 面载体：带图轮真的把图交给模型。

两条回答链路（``POST /api/chat`` / ``POST /api/chat/stream``）的带图轮：

- 模型装配走 ``resolve_vision_model``（``vision_config`` 角色）——**不得**回落
  ``resolve_chat_model``（那等于把纯文本发给模型猜，101 的缺陷本体）；
- 模型收到的最后一条 user 消息带 ``ImageContent``（``image_url`` part）+ 视觉系统提示
  （``VISION_SYSTEM_PROMPT``）；
- **无图轮逐字不变**：仍走 ``resolve_chat_model``、纯文本、非视觉提示（回归钉）；
- 视觉装配不到 ⇒ 流开始前 400 ``VISION_NOT_CONFIGURED_HINT``、零 DB 副作用
  （回归修复 后的端点语义；101 当时的流内 error 帧语义见该载体注释）；
- 用量：带图轮成功收口记 ``kind="vision"``，无图轮记 ``chat``（回归钉）。

生成层载体见 ``tests/generation/test_bug101_vision_messages.py``。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.api import chat_routes
from app.core.config import settings
from app.generation import answer as answer_mod
from app.models import Chat, UsageEvent
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401

DATA_URL = "data:image/png;base64,AAAA"


class _FakeStream:
    def __init__(self, parts: list[str]) -> None:
        self._parts = parts

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for part in self._parts:
            yield SimpleNamespace(type="text_delta", delta=part)


class _RecorderModel:
    """假 bound model：记录收到的消息与系统提示（流式 + 同步两用）。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def generate(self, messages, system_prompt=None, max_output_tokens=None):  # noqa: ANN001
        self.calls.append({"messages": messages, "system_prompt": system_prompt, "mode": "generate"})
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.reply)], stop_reason="end_turn"
        )

    async def stream(self, messages, system_prompt=None):  # noqa: ANN001
        self.calls.append({"messages": messages, "system_prompt": system_prompt, "mode": "stream"})
        return _FakeStream([self.reply])

    @property
    def last(self) -> dict:
        assert self.calls, "模型一次都没被调用"
        return self.calls[-1]

    def last_user_parts(self) -> list:
        return list(self.last["messages"][-1].content)

    @property
    def part_types(self) -> list[str]:
        return [getattr(block, "type", "") for block in self.last_user_parts()]


def _install_vision(monkeypatch, *, vision_reply: str | None, chat_reply: str = "纯文本回答。"):
    """装好视觉 / 对话两个装配缝与路由桩；``vision_reply=None`` ⇒ 视觉装配不到。"""
    chat_recorder = _RecorderModel(chat_reply)
    vision_recorder = _RecorderModel(vision_reply) if vision_reply is not None else None
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: chat_recorder)
    monkeypatch.setattr(
        chat_routes,
        "resolve_vision_model",
        lambda: vision_recorder,
    )
    monkeypatch.setattr(chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external"))
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    return chat_recorder, vision_recorder


# --- 腿①：流式带图轮——图进请求体 + 视觉系统提示 + 走视觉装配缝 -----------------


@pytest.mark.asyncio
async def test_stream_image_turn_sends_image_part_and_vision_prompt(client, db, monkeypatch):
    chat_recorder, vision_recorder = _install_vision(monkeypatch, vision_reply="图里是一只猫。")

    async with client.stream(
        "POST",
        "/api/chat/stream",
        json={"message": "图里有什么？", "images": [DATA_URL]},
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")

    events = parse_sse(raw)
    assert events[0]["type"] == "meta"
    assert events[-1]["type"] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "token") == "图里是一只猫。"

    assert vision_recorder.calls, "带图轮必须走视觉装配缝"
    assert chat_recorder.calls == [], "带图轮不得改调对话模型"
    assert vision_recorder.last["system_prompt"] == answer_mod.VISION_UNNUMBERED_SYSTEM_PROMPT + "\n\n" + answer_mod.FACTUAL_ANSWER_CONSTRAINTS
    assert vision_recorder.part_types == ["text", "image"]
    image_part = vision_recorder.last_user_parts()[1]
    assert (image_part.media_type, image_part.source) == ("image/png", "AAAA")

    # 落库回读：回答以正常轮收口（kind=direct、零引用——带图轮本就 agent off）
    chat_id = events[0]["chat_id"]
    await db.rollback()
    detail = (await client.get(f"/api/chats/{chat_id}")).json()
    roles = [(m["role"], m["content"]) for m in detail["messages"]]
    assert ("assistant", "图里是一只猫。") in roles


# --- 腿②：无图轮逐字不变（回归钉） --------------------------------------------


@pytest.mark.asyncio
async def test_stream_text_turn_keeps_chat_seam_and_plain_text(client, db, monkeypatch):
    chat_recorder, vision_recorder = _install_vision(monkeypatch, vision_reply="图里是一只猫。")

    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "向量库用什么存储？"}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")

    events = parse_sse(raw)
    assert events[-1]["type"] == "done"
    assert chat_recorder.calls, "无图轮必须走对话装配缝"
    assert vision_recorder.calls == [], "无图轮不得改调视觉装配缝"
    assert chat_recorder.last["system_prompt"] != answer_mod.VISION_SYSTEM_PROMPT
    assert chat_recorder.part_types == ["text"]


# --- 腿③：同步带图轮同口径 ------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_image_turn_sends_image_part_and_vision_prompt(client, db, monkeypatch):
    chat_recorder, vision_recorder = _install_vision(monkeypatch, vision_reply="图里是一只猫。")

    resp = await client.post(
        "/api/chat", json={"message": "图里有什么？", "images": [DATA_URL]}
    )

    assert resp.status_code == 200
    assert resp.json()["message"] == "图里是一只猫。"
    assert vision_recorder.calls, "同步带图轮也必须走视觉装配缝"
    assert chat_recorder.calls == []
    assert vision_recorder.last["system_prompt"] == answer_mod.VISION_UNNUMBERED_SYSTEM_PROMPT + "\n\n" + answer_mod.FACTUAL_ANSWER_CONSTRAINTS
    assert vision_recorder.part_types == ["text", "image"]


# --- 腿④：视觉装配不到 ⇒ 流开始前 400 固定中文提示（回归修复 后的语义）------
# ⚠️ 测试侧适配（有意差异，随 118 登记）：101 当时的语义是「进流、error 帧、用户消息
# 留档」；118 把带图轮的就绪校验改为查视觉角色 ⇒ 装配不到在**流开始前**即 400
# （``VISION_NOT_CONFIGURED_HINT``，零 DB 副作用）。流内 hint 收尾仍保留（兜底
# ``stream_tokens`` 直调方），但端点面到不了。


@pytest.mark.asyncio
async def test_stream_image_turn_without_vision_model_rejected_before_stream(client, db, monkeypatch):
    chat_recorder, _vision = _install_vision(monkeypatch, vision_reply=None)

    resp = await client.post(
        "/api/chat/stream",
        json={"message": "图里有什么？", "images": [DATA_URL]},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == answer_mod.VISION_NOT_CONFIGURED_HINT
    assert chat_recorder.calls == [], "装配不到视觉模型时不得改调对话模型"

    # 零 DB 副作用：就绪校验在任何写库之前（101 旧语义会落用户消息，118 后不再落）
    count = (await db.execute(select(func.count()).select_from(Chat))).scalar()
    assert count == 0


# --- 腿⑤：用量 kind——带图轮记 vision、无图轮记 chat ----------------------------


async def _latest_usage_kind(db) -> str | None:
    await db.rollback()
    row = (
        await db.execute(select(UsageEvent).order_by(UsageEvent.id.desc()))
    ).scalars().first()
    return row.kind if row is not None else None


@pytest.mark.asyncio
async def test_usage_kind_is_vision_for_image_turn_and_chat_for_text_turn(client, db, monkeypatch):
    import httpx
    from app.usage import recorder
    from tests.test_opt5_observation_usage import _provider
    from tests.api.t0_wire import wire_sse_text

    _install_vision(monkeypatch, vision_reply="图里是一只猫。")
    sends = []
    providers = []
    for kind in ("vision", "chat"):
        def handler(request, kind=kind):
            sends.append((kind, request))
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content=wire_sse_text("控制回答"),
            )
        provider = _provider(handler, kind=kind)
        providers.append(provider)
        model = provider.model("controlled")
        monkeypatch.setattr(chat_routes, f"resolve_{kind}_model", lambda model=model: model)
    try:
        await client.post("/api/chat", json={"message": "图里有什么？", "images": [DATA_URL]})
        await db.rollback()
        await recorder.drain_pending()
        assert await _latest_usage_kind(db) == "vision"
        await client.post("/api/chat", json={"message": "纯文本提问都没问题吧"})
        await db.rollback()
        await recorder.drain_pending()
        assert await _latest_usage_kind(db) == "chat"
        assert [kind for kind, _ in sends] == ["vision", "chat"]
    finally:
        for provider in providers:
            await provider._client.close()
