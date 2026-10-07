"""`回归修复` 判据载体：重排门槛把材料**删空** ⇒ **refuse**（不再静默转库外直答）。

🔴 口径（逐字，防误读）

· 2026-09-27 拍板④（「refuse」，与 076 拍板③同精神）：检索明明命中（candidates ≥ 1）、
  候选全被 ``RAG_MIN_RERANK_SCORE`` 过滤（``empty_reason="below_threshold"``）⇒ 走拒答行，
  不再把「模型自己的知识」冒充正常回答；留痕半（``empty_reason`` 进 ``retrieval/result`` 与
  ``turn/end``）此前已落地（029 载体），本条补的是**口径**。
· **三条边界**（实现与载体同钉）：
  ① 真·零命中（``no_recall``）**不走本条**——库外直答对「库里根本没有」仍是设计行为；
  ② 路由判成 ``external`` 的轮**不拦**——用户问的就是库外知识，库内弱匹配被删空不该
    反过来拒答（``ROUTING_MODE=off`` 视为 external，029 的既有 direct+NOTICE 腮因此不变）；
  ③ 只作用**非 Agent 轮**——Agent 轮的检索尝试在 trace 里可见（不属「静默」），且 refuse 的
    ``needs_model=False`` 会短路整个 Agent（连联网检索一起杀）⇒ Agent 轮空手收口归 076。
· 载体场景必须**显式钉 ``internal_db``**（``_pin_internal_db``）：``prepare``/conftest 的
  默认路由是 off ⇒ external，不钉就落不进 084 的口径面。
· 🟢 2026-09-27 审计处置（REAL-1）：补边界③的行为腿（``test_agent_round_not_blocked_by_below_threshold``）——
  Agent 轮 + 基线 below_threshold ⇒ Agent 照常跑完（done.kind != refuse）⇒「实现与载体同钉」声明成立。
"""

from __future__ import annotations

import pytest

from app.api import chat_routes
from app.core.config import settings
from app.generation.answer import REFUSE_TEXT
from app.session import events
from tests.api.test_chat_contract import (  # noqa: F401
    _StubClassifier,
    client,
    faux_model,
    parse_sse,
)
from tests.api.test_chat_stream_events import _messages
from tests.api.test_turn_prefix_040 import QUESTION, prepare
from tests.api.test_rerank_provenance_029 import install_recall, install_rerank
from tests.api.test_chat_agent import (  # noqa: F401 —— 边界③腿复用 agent 夹具
    _events,
    _install_model,
    _install_retrieval,
    _provider_with_tool_call,
    _seed_materials,
)

QUESTION_TEXT = QUESTION


def _pin_internal_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """钉成库内语义轮（084 的口径面；理由见文件头）。

    ⚠️ `prepare` 会把 ``ROUTING_MODE`` 钉成 ``off``（⇒ decision 恒 external、分类器根本
    不被咨询）⇒ 这里必须把路由模式**恢复成出厂默认 regex**，``_StubClassifier`` 才会被
    问到（regex 不命中 ⇒ 兜底问分类器）。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "ROUTING_MODE", "regex")
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )


async def _prepare_internal(db, monkeypatch, model):  # noqa: ANN001
    chat = await prepare(db, monkeypatch, model, assume=False)
    _pin_internal_db(monkeypatch)
    return chat


@pytest.mark.asyncio
async def test_below_threshold_round_refuses_instead_of_direct(client, db, monkeypatch):
    """主腿（internal_db）：12 条候选全被门槛（0.99）删空 ⇒ refuse / empty_scope / 标准拒答 / 零引用。"""
    chat = await _prepare_internal(db, monkeypatch, faux_model("库外直答不该出现。"))
    install_recall(monkeypatch)
    install_rerank(monkeypatch, [0.1] * 12, threshold=0.99)

    resp = await client.post("/api/chat", json={"chat_id": chat.id, "message": QUESTION_TEXT})

    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "refuse", f"门槛删空应落 refuse（084）：{body.get('kind')}"
    assert body["answer_source"] == "empty_scope"
    assert body["message"] == REFUSE_TEXT
    assert body["refs"] == []

    await db.commit()
    result = (await events.list_events_by_type(db, chat.id, "retrieval/result"))[-1].data
    assert result["empty_reason"] == "below_threshold"
    assert result["rerank"]["candidates"] == 12 and result["rerank"]["kept"] == 0
    end = (await events.list_events_by_type(db, chat.id, "turn/end"))[-1].data
    assert end["final_material_count"] == 0
    assert end["final_empty_reason"] == "below_threshold"


@pytest.mark.asyncio
async def test_no_recall_still_goes_direct(client, db, monkeypatch):
    """分界腿①：真·零命中（no_recall，召回 0 条）⇒ 库内语义轮也**照旧直答**（084 只管「命中被删空」）。"""
    chat = await _prepare_internal(db, monkeypatch, faux_model("按一般知识：可以直接回答。"))
    install_recall(monkeypatch, count=0)  # 召回 0 条 ⇒ status=no_candidates ⇒ empty_reason=no_recall
    from app.core.config import settings

    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    monkeypatch.setattr(settings, "RAG_MIN_RERANK_SCORE", 0.99)

    resp = await client.post("/api/chat", json={"chat_id": chat.id, "message": QUESTION_TEXT})

    body = resp.json()
    assert body["kind"] == "direct", f"真·零命中仍应直答（084 分界①）：{body.get('kind')}"
    assert body["answer_source"] == "general"


@pytest.mark.asyncio
async def test_external_routed_round_still_direct_with_notice(client, db, monkeypatch):
    """分界腿②：路由判 external（ROUTING_MODE=off 的既有语义）⇒ 门槛删空**不拦**（direct+NOTICE 不变）。"""
    chat = await prepare(db, monkeypatch, faux_model("按一般知识：先定主题再规划。"), assume=False)
    install_recall(monkeypatch)
    install_rerank(monkeypatch, [0.1] * 12, threshold=0.99)

    resp = await client.post("/api/chat", json={"chat_id": chat.id, "message": QUESTION_TEXT})

    body = resp.json()
    assert body["kind"] == "direct", f"external 轮不受 084 影响（分界②）：{body.get('kind')}"
    assert body["answer_source"] == "general"


@pytest.mark.asyncio
async def test_below_threshold_stream_round_refuses_too(client, db, monkeypatch):
    """流式同构腿（internal_db）：同一场景走 /api/chat/stream ⇒ refuse 文案逐字一致。"""
    chat = await _prepare_internal(db, monkeypatch, faux_model("不该被调到。"))
    install_recall(monkeypatch)
    install_rerank(monkeypatch, [0.1] * 12, threshold=0.99)

    resp = await client.post(
        "/api/chat/stream", json={"chat_id": chat.id, "message": QUESTION_TEXT}
    )

    frames = parse_sse(resp.text)
    assert frames[-1]["type"] == "done"
    assert frames[-1]["kind"] == "refuse"
    assert [f["text"] for f in frames if f["type"] == "token"] == [REFUSE_TEXT]
    assert (await _messages(db, chat.id))[-1].content == REFUSE_TEXT


@pytest.mark.asyncio
async def test_agent_round_not_blocked_by_below_threshold(client, db, monkeypatch):
    """边界③行为腿（审计 REAL-1 处置）：基线 below_threshold 的 **Agent 轮不拦**——Agent 照常跑。

    构造：种子文档（真关键词路召回）+ 重排门槛 0.99 删空（基线 below_threshold）+
    Agent 工具假件返回材料 ⇒ 轮子照常走 Agent（不落 refuse），且 Agent 有料 ⇒ normal。
    """
    monkeypatch.setattr(settings, "AGENT_MODE", "off")
    materials = await _seed_materials(db, ["蓝鲸是哺乳动物。", "鲸类生活在海里。"], filename="蓝鲸.md")
    install_recall(monkeypatch)
    install_rerank(monkeypatch, [0.1] * 12, threshold=0.99)
    _install_retrieval(monkeypatch, materials)
    provider = _provider_with_tool_call("蓝鲸")
    _install_model(monkeypatch, provider, answer_text="蓝鲸是哺乳动物。[1]")

    events = await _events(client, {"message": "蓝鲸是什么", "agent_mode": "react"})

    done = events[-1]
    assert done["type"] == "done"
    assert done["kind"] != "refuse", f"Agent 轮不得被 084 短路成 refuse（边界③）：{done.get('kind')}"
    assert done["kind"] == "normal"
    assert len(done["refs"]) >= 1
