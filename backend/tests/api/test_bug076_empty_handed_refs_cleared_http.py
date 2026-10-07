"""`回归修复` 端到端判据载体（HTTP 全链路）：**Agent 空手收尾 ⇒ 引用清空**。

🔴 口径（逐字，防误读）

· 场景 = 账本 msg 958 的同型：**基线检索有候选**（关键词路真命中种子文档）+ **Agent 工具零命中**
  （`knowledge_search` 假件返回空）+ 模型收尾无协议块（A 路）⇒ 修复前 `done.refs` 会把
  基线候选拿 `citation_mode="fallback"` 全量挂出；修复后 = **零引用 + kind=direct +
  answer_source=general**（2026-09-27 拍板③「一致性优先」）。
· 判据锚 = `turn/end.final_material_count`（拍板单点名的现成判据）。
· ⚠️ 桩面：工具检索与回答链路打桩（Faux），**基线检索是真链路**（真关键词命中）——
  这正是 076「同一轮两条检索打架」里要保留真实的那条。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.api import chat_routes
from app.core.config import settings
from app.models import SessionEvent
from tests.api.test_chat_agent import (  # noqa: F401 —— 夹具复用（本仓既有做法）
    _events,
    _install_model,
    _install_retrieval,
    _provider_with_tool_call,
    _seed_materials,
)
from tests.api.test_chat_contract import client, parse_sse  # noqa: F401
from tests.retrieval.conftest import seed_document

CHUNK_TEXT = "向量库使用 Qdrant 内嵌模式，向量化由上游接口完成。"


@pytest.mark.asyncio
async def test_empty_handed_agent_round_clears_refs_and_goes_direct(client, db, monkeypatch):
    """076 主腿（HTTP）：基线有候选 + Agent 空手 ⇒ done.refs=[] / kind=direct / 计数 0。"""
    monkeypatch.setattr(settings, "AGENT_MODE", "off")
    await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    # Agent 工具**零命中**（076 的「Agent 空手」）：knowledge_search 返回空。
    calls = _install_retrieval(monkeypatch, [])
    provider = _provider_with_tool_call("向量库")
    _install_model(monkeypatch, provider, answer_text="我没有找到相关资料，无法回答这个问题。")
    # 决策走 aux；本文件自钉，避免跨文件 collect 时丢 API autouse 而变成无工具轮。
    monkeypatch.setattr(chat_routes, "resolve_aux_model", chat_routes.resolve_chat_model)

    events = await _events(client, {"message": "向量库用什么存储？", "agent_mode": "react"})

    done = events[-1]
    assert done["type"] == "done"
    assert len(calls) == 1 and calls[0][0] == "向量库", (calls, done["agent_trace"])
    tool_steps = [
        step for step in done["agent_trace"]["steps"]
        if step["kind"] in ("tool", "observation")
    ]
    assert [step["kind"] for step in tool_steps] == ["tool", "observation"], tool_steps
    assert all(step["tool"] == "knowledge_search" for step in tool_steps)
    assert tool_steps[0]["tool_call_id"] == tool_steps[1]["tool_call_id"]
    assert done["kind"] == "direct", f"空手收尾应落 direct：{done.get('kind')}"
    assert done["refs"] == [], f"空手收尾不得挂基线候选引用（076）：{done.get('refs')}"
    assert done["answer_source"] == "general"

    # 拍板单点名的现成判据：turn/end.final_material_count == 0（session_events 的落库形态）。
    await db.commit()
    end_rows = await db.execute(
        select(SessionEvent)
        .where(SessionEvent.chat_id == int(events[0]["chat_id"]), SessionEvent.type == "turn/end")
        .order_by(SessionEvent.id.desc())
        .limit(1)
    )
    end = end_rows.scalars().first()
    assert end is not None, "turn/end 事件应已落库"
    assert end.data["final_material_count"] == 0, end.data
    assert end.data["kind"] == "direct", end.data


@pytest.mark.asyncio
async def test_agent_with_materials_keeps_refs_control(client, db, monkeypatch):
    """对照腿（防过修）：Agent **有料** ⇒ 引用照常（076 只清「空手」态）。"""
    monkeypatch.setattr(settings, "AGENT_MODE", "off")
    materials = await _seed_materials(db, ["蓝鲸是哺乳动物。", "鲸类生活在海里。"], filename="蓝鲸.md")
    calls = _install_retrieval(monkeypatch, materials)
    provider = _provider_with_tool_call("蓝鲸")
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[1]")
    monkeypatch.setattr(chat_routes, "resolve_aux_model", chat_routes.resolve_chat_model)

    events = await _events(client, {"message": "蓝鲸是什么", "agent_mode": "react"})

    done = events[-1]
    assert done["type"] == "done"
    assert len(calls) == 1 and calls[0][0] == "蓝鲸", (calls, done["agent_trace"])
    assert done["kind"] == "normal"
    assert len(done["refs"]) >= 1, "Agent 有料的轮引用照常"
