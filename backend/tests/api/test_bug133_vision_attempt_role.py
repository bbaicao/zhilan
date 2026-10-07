"""回归修复 载体：vision 轮 ``assistant/attempt`` 记**视觉角色**的厂商与型号。

缺陷（修复前）：``log_attempt``（全仓唯一 ``assistant/attempt`` 写入点）不透传本轮是否
带图，恒按 ``role="chat"`` 读 ``chat_config``——chat 绑 A 服务模型 X、vision 绑 B 服务
模型 Y 时，带图轮的 attempt 事件记 X，而同轮用量行（``record_answer_usage`` 记
``kind="vision"`` + vision 角色）记 Y：``attempt_id`` 串三表的对账链 provider/model
字段自相矛盾。

修复：``log_attempt`` 按本轮带图与否选角色（与 ``record_answer_usage`` 同一判据）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api import chat_routes
from app.core.config import Settings, settings
from app.session import attempt as attempt_mod
from app.session import events as event_log
from tests.api.test_bug101_vision_wiring import DATA_URL, _RecorderModel
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401

CHAT_CFG = SimpleNamespace(provider="chat-prov", model="chat-model-x", name="chat-model-x")
VISION_CFG = SimpleNamespace(provider="vision-prov", model="vision-model-y", name="vision-model-y")


def _install(monkeypatch, *, vision_reply: str | None) -> tuple[_RecorderModel, _RecorderModel | None]:
    """双角色分装 + 配置面双假件（provider/model 两角色刻意取不同值）。"""
    chat = _RecorderModel("纯文本回答。")
    vision = _RecorderModel(vision_reply) if vision_reply is not None else None
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: chat)
    monkeypatch.setattr(chat_routes, "resolve_vision_model", lambda: vision)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: None)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external")
    )
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    # chat_config / vision_config 是 Settings 的只读 property ⇒ 在**类上**换属性替身，
    # monkeypatch 结束自动还原（进程内其他读者随之读到假件，正是本载体要的口径）。
    monkeypatch.setattr(Settings, "chat_config", property(lambda self: CHAT_CFG))
    monkeypatch.setattr(Settings, "vision_config", property(lambda self: VISION_CFG))
    return chat, vision


async def _last_attempt(db, chat_id: int) -> dict:
    rows = await event_log.list_events_by_type(db, chat_id, attempt_mod.ATTEMPT_EVENT)
    assert rows, "本轮没有 assistant/attempt 事件"
    return rows[-1].data


@pytest.mark.asyncio
async def test_image_turn_attempt_records_vision_role(client, db, monkeypatch):
    _install(monkeypatch, vision_reply="图里是一只猫。")

    resp = await client.post("/api/chat", json={"message": "图里有什么？", "images": [DATA_URL]})

    assert resp.status_code == 200
    data = await _last_attempt(db, resp.json()["chat_id"])
    assert data["outcome"] == attempt_mod.ATTEMPT_COMMITTED
    # 修复前：这里记的是 chat-config 的 X（与同轮用量行 kind=vision 的 Y 自相矛盾）
    assert (data["provider"], data["model"]) == ("vision-prov", "vision-model-y")


@pytest.mark.asyncio
async def test_text_turn_attempt_still_records_chat_role(client, db, monkeypatch):
    """回归钉：无图轮恒记对话角色（逐字不变）。"""
    _install(monkeypatch, vision_reply="图里是一只猫。")

    resp = await client.post("/api/chat", json={"message": "纯文本提问没问题吧"})

    assert resp.status_code == 200
    data = await _last_attempt(db, resp.json()["chat_id"])
    assert (data["provider"], data["model"]) == ("chat-prov", "chat-model-x")


@pytest.mark.asyncio
async def test_stream_image_turn_attempt_records_vision_role(client, db, monkeypatch):
    """流式链路同一判据（``log_attempt`` 是两条链路共用的唯一写入点）。"""
    _install(monkeypatch, vision_reply="图里是一只猫。")

    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "图里有什么？", "images": [DATA_URL]}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")

    done = [e for e in parse_sse(raw) if e["type"] == "done"][-1]
    data = await _last_attempt(db, done["chat_id"])
    assert data["outcome"] == attempt_mod.ATTEMPT_COMMITTED
    assert (data["provider"], data["model"]) == ("vision-prov", "vision-model-y")
