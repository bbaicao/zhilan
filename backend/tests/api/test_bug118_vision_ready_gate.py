"""回归修复 载体：带图轮就绪校验查**视觉角色**（原缺陷：无条件查对话角色）。

缺陷（修复前）：``validate_request`` 无条件 ``require_chat_ready()``（查
``resolve_chat_model()``），而带图轮实际装配 ``resolve_vision_model()``——
vision-only 绑定（只给视觉角色配了服务）被 400「尚未配置对话模型」误拒；
v1.0.13 登记的 ``VISION_NOT_CONFIGURED_HINT`` 提示族在该分支永远到不了。

修复：带图轮查视觉角色（装配不到 ⇒ 400 固定中文提示，**流开始前**、零 DB 副作用）；
无图轮逐字不变（回归钉）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.api import chat_routes
from app.core.config import settings
from app.core.errors import NOT_CONFIGURED_HINT
from app.generation import answer as answer_mod
from app.models import Chat
from tests.api.test_bug101_vision_wiring import DATA_URL, _RecorderModel
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401


def _install_seams(monkeypatch, *, chat_model, vision_model) -> None:
    """按角色的装配缝注入：``None`` = 该角色未绑定。"""
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: chat_model)
    monkeypatch.setattr(chat_routes, "resolve_vision_model", lambda: vision_model)
    monkeypatch.setattr(chat_routes, "resolve_aux_model", lambda: None)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external")
    )
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)


async def _chat_rows(db) -> int:
    return int((await db.execute(select(func.count()).select_from(Chat))).scalar() or 0)


# --- 腿①：vision-only 绑定（对话角色未绑定）的带图轮必须放行 -------------------


@pytest.mark.asyncio
async def test_image_turn_with_vision_only_binding_is_accepted(client, db, monkeypatch):
    vision = _RecorderModel("图里是一只猫。")
    _install_seams(monkeypatch, chat_model=None, vision_model=vision)
    resp = await client.post("/api/chat", json={"message": "图里有什么？", "images": [DATA_URL]})

    assert resp.status_code == 200
    assert resp.json()["message"] == "图里是一只猫。"
    assert vision.calls, "带图轮走视觉装配缝"
    assert vision.last["system_prompt"] == answer_mod.VISION_UNNUMBERED_SYSTEM_PROMPT + "\n\n" + answer_mod.FACTUAL_ANSWER_CONSTRAINTS


# --- 腿②：视觉装配不到 ⇒ 400 固定中文提示（两端点同一闸门、零 DB 副作用）-------


@pytest.mark.asyncio
async def test_image_turn_without_vision_model_is_rejected_before_stream(client, db, monkeypatch):
    """对话角色配得好好的，400 只能来自视觉缺失，且文案是 v1.0.13 提示族。"""
    chat_stub = object()  # 只为过「对话角色已绑定」——带图轮不该用到它
    _install_seams(monkeypatch, chat_model=chat_stub, vision_model=None)

    resp = await client.post("/api/chat", json={"message": "图里有什么？", "images": [DATA_URL]})
    assert resp.status_code == 400
    assert resp.json()["detail"] == answer_mod.VISION_NOT_CONFIGURED_HINT

    resp = await client.post(
        "/api/chat/stream", json={"message": "图里有什么？", "images": [DATA_URL]}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == answer_mod.VISION_NOT_CONFIGURED_HINT

    # 就绪校验在任何写库之前：不建会话、不落消息
    assert await _chat_rows(db) == 0


# --- 腿③（回归钉）：无图轮逐字不变——对话角色未绑定仍是原 400 文案 --------------


@pytest.mark.asyncio
async def test_text_turn_without_chat_model_keeps_original_hint(client, db, monkeypatch):
    _install_seams(monkeypatch, chat_model=None, vision_model=None)

    resp = await client.post("/api/chat", json={"message": "纯文本提问"})

    assert resp.status_code == 400
    assert resp.json()["detail"] == NOT_CONFIGURED_HINT
    assert await _chat_rows(db) == 0
