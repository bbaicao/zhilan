"""`回归修复` 第二批「补腿」载体：`persist_turn` 两个**仅 log 吞点**的行为钉（#5 / #6）。

🔴 口径（逐字，防误读）

· 本文件是**行为钉**，**不是接线验证**：#5（归因行）/ #6（审计 run↔message 绑定）在
  2026-09-27 第二批**未接进 `counters.derived`**（名单把新键预算给了 `pdf_ocr_fallback`
  与 `web_fetch_degrade` 两键，键集钉「五键 → 七键」不含它们）⇒ 这两个吞点的可见性**仍是
  「仅 log」**（叠加 `回归修复`：冻结态应用层日志不进控制台 ⇒ 等于不可见）；
· 本载体钉的是它们的**隔离语义**（「回答已生成，仍以正常结果返回」绝不炸穿，I5）——
  防的是「有人把吞点改成抛出 / 把 rollback 丢了」这类回归；
· 绿 = 现状（隔离 + 警告）被如实钉住；**绿 ≠ 083 全闭环**（两点的计数面仍未接，账本照实登记）。

## 覆盖的吞点（吞点地图 #5 / #6）

| # | 落点（chat_routes.persist_turn） | 行为 |
|---|---|---|
| #5 | `add_attributions` try/except（`chat_routes.persist_turn` 内，行号随 076/084 同批改动漂移，按符号定位） | 归因失败 ⇒ rollback + warning，refs 照常返回 |
| #6 | `agent_audit.bind_message` try/except（同上，persist_turn 收尾段） | 回填失败 ⇒ warning（无 rollback），本轮照常 |
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AgentRun, MessageAttribution
from app.session import repository as repo
from tests.api.test_chat_agent import (  # noqa: F401 —— 夹具复用（本仓既有做法）
    _events,
    _install_model,
    _install_retrieval,
    _provider_with_tool_call,
)
from tests.api.test_chat_citation import _assistant_message
from tests.api.test_chat_contract import client, parse_sse  # noqa: F401 —— client 是夹具
from tests.api.test_chat_retrieval_wiring import _install
from tests.retrieval.conftest import seed_document

CHUNK_TEXT = "向量库使用 Qdrant 内嵌模式，向量化由上游接口完成。"


# --------------------------------------------------------------------------
# #5：归因行落库失败 ⇒ 隔离，refs 照常
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_05_attribution_write_failure_is_isolated(client, db, monkeypatch):
    """#5：`add_attributions` 必抛 ⇒ 本轮照常 200、refs 照常返回，归因表**零行**。"""

    async def _boom(_db, _rows):
        raise RuntimeError("模拟归因落库失败")

    monkeypatch.setattr(repo, "add_attributions", _boom)
    await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    _install(monkeypatch, reply="据资料 [1]，向量库用 Qdrant。")

    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "normal"
    assert len(body["refs"]) == 1, "归因失败不该连带丢引用（隔离语义）"

    message = await _assistant_message(db, body["chat_id"])
    assert message.refs == body["refs"], "消息行里的 refs 照常落库"

    rows = (
        await db.execute(
            select(MessageAttribution).where(MessageAttribution.message_id == message.id)
        )
    ).scalars().all()
    assert rows == [], "归因写失败 ⇒ 归因表零行（rollback 生效）"


@pytest.mark.asyncio
async def test_05_control_attributions_written_without_failure(client, db, monkeypatch):
    """#5 对照腿（防空转）：不打桩时同一场景归因**真落行** ⇒ 证明上一腿的红点打在了真位置。"""
    await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    _install(monkeypatch, reply="据资料 [1]，向量库用 Qdrant。")

    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})

    assert resp.status_code == 200
    message = await _assistant_message(db, resp.json()["chat_id"])
    rows = (
        await db.execute(
            select(MessageAttribution).where(MessageAttribution.message_id == message.id)
        )
    ).scalars().all()
    assert len(rows) == 1


# --------------------------------------------------------------------------
# #6：审计 run↔message 绑定失败 ⇒ 隔离，本轮照常
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_06_audit_bind_failure_is_isolated(client, db, monkeypatch):
    """#6：agent 轮里 `bind_message` 必抛 ⇒ 本轮照常收尾，`agent_runs.message_id` 保持 NULL。"""

    async def _boom(_db, _run_id, _message_id):
        raise RuntimeError("模拟审计回填失败")

    from app.agent import audit as agent_audit

    monkeypatch.setattr(agent_audit, "bind_message", _boom)
    settings_off(monkeypatch)
    materials = await _seed_materials_safe(db)
    _install_retrieval(monkeypatch, materials)
    provider = _provider_with_tool_call("蓝鲸")
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[1]")

    events = await _events(client, {"message": "蓝鲸是什么", "agent_mode": "react"})

    kinds = [e["type"] for e in events]
    assert kinds[0] == "meta" and "done" in kinds, f"绑定失败不该打断本轮：{kinds}"
    chat_id = int(events[0]["chat_id"])

    await db.commit()
    run = (
        await db.execute(select(AgentRun).where(AgentRun.chat_id == chat_id).limit(1))
    ).scalars().first()
    assert run is not None, "审计 run 行应存在（AGENT_ENABLE_AUDIT 默认开）——防空转"
    assert run.message_id is None, "绑定失败 ⇒ message_id 保持 NULL"


@pytest.mark.asyncio
async def test_06_control_bind_message_id_written_without_failure(client, db, monkeypatch):
    """#6 对照腿（防空转）：不打桩时 `agent_runs.message_id` 真被回填。"""
    settings_off(monkeypatch)
    materials = await _seed_materials_safe(db)
    _install_retrieval(monkeypatch, materials)
    provider = _provider_with_tool_call("蓝鲸")
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[1]")

    events = await _events(client, {"message": "蓝鲸是什么", "agent_mode": "react"})
    chat_id = int(events[0]["chat_id"])

    await db.commit()
    run = (
        await db.execute(select(AgentRun).where(AgentRun.chat_id == chat_id).limit(1))
    ).scalars().first()
    assert run is not None and run.message_id is not None


# --------------------------------------------------------------------------
# 脚手架
# --------------------------------------------------------------------------


def settings_off(monkeypatch: pytest.MonkeyPatch):
    """ReAct 用例的标准姿势：AGENT_MODE=off（显式关），agent_mode 由请求体打开。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENT_MODE", "off")
    return settings


async def _seed_materials_safe(db):  # noqa: ANN001
    """转发 `test_chat_agent._seed_materials`（它要 filename 关键字）。"""
    from tests.api.test_chat_agent import _seed_materials

    return await _seed_materials(db, ["蓝鲸是哺乳动物。", "鲸类生活在海里。"], filename="蓝鲸.md")


# --------------------------------------------------------------------------
# MINOR-2 处置（2026-09-27 审计）：两个新键的**逐键 HTTP 腿**——透传到
# `GET /api/agent/audit` 的 `counters.derived`（原先只有进程层腿 + 既有键的间接透传证明）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_keys_transit_to_audit_endpoint(client):
    """`pdf_ocr_fallback` / `web_fetch_degrade` bump 后经 `GET /api/agent/audit` 可见。"""
    from app.core import derived_counters

    derived_counters.bump("pdf_ocr_fallback", 2)
    derived_counters.bump("web_fetch_degrade", 1)
    try:
        resp = await client.get("/api/agent/audit")
        assert resp.status_code == 200, resp.text
        derived = resp.json()["counters"]["derived"]
        assert derived["pdf_ocr_fallback"] == 2, derived
        assert derived["web_fetch_degrade"] == 1, derived
    finally:
        derived_counters.reset_failure_counts()
