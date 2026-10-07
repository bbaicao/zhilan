"""Unavailable selected skills fail before either answer path starts."""

import pytest
from sqlalchemy import func, select

from app.api import skills_spec
from app.models import Chat
from tests.api.test_chat_contract import client, use_faux  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
@pytest.mark.parametrize("enabled,available", [(False, True), (True, False), (False, False)])
async def test_unavailable_skill_rejected_before_writes(client, db, use_faux, monkeypatch, path, enabled, available):
    use_faux("must not answer")
    monkeypatch.setattr(skills_spec, "list_skills", lambda: [{
        "id": "local:unavailable", "name": "测试技能", "enabled": enabled,
        "available": available, "status_note": "配置缺失",
    }])
    response = await client.post(path, json={"message": "你好", "skill_ids": ["local:unavailable"]})
    assert response.status_code == 400
    assert response.json()["detail"] == "「测试技能」不可用：配置缺失。可在设置 → 技能 / MCP 里启用或补全配置。"
    assert "text/event-stream" not in response.headers["content-type"]
    assert await db.scalar(select(func.count()).select_from(Chat)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
async def test_available_skill_does_not_fail_preflight(client, use_faux, monkeypatch, path):
    """已知且可用的 skill id 不拦。

    ⚠️ **F4（第 0 笔）改判**：本用例原先还断言「未知 id 也放行」——那正是 F4 要修的
    静默吞意图行为。未知 id 现在 **400**（见 ``test_skill_preflight_f4_t2.py``）；
    这里只保留合法 id 这条成对腿。
    """
    use_faux("你好")
    monkeypatch.setattr(skills_spec, "list_skills", lambda: [{
        "id": "builtin:knowledge", "name": "知识库", "enabled": True, "available": True,
    }])
    response = await client.post(path, json={
        "message": "你好", "skill_ids": ["builtin:knowledge"],
    })
    assert response.status_code == 200
