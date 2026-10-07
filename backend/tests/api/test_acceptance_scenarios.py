"""条目 1.6 验收场景：库外 / 库内指令 / 澄清 三类，端到端 + 落库校验。

阶段 1 设计说明 条目 1.6 的三个场景（原文）：

1. 「今天天气怎么样」→ external → general，**无假引用**；
2. 「知识库里关于项目周报的内容」→ internal（检索可空）→ 空结果策略**定死一种**
   （`CHAT_DIRECT_ANSWER_ENABLE=true` 时 direct，否则 refuse）；
3. 「那个怎么样」→ clarify 追问。

与 `tests/api/test_chat_contract.py` 的分工：那边验**契约面**（字段/SSE/错误码），
这边验**验收面**——三类问题的路由结论、§3.8 映射、落库值、以及 I7「无假引用」
在真实问答链路上成立。

模型一律 FauxProvider 注入（不连真模型）；DB 用独立测试库。
"""

from __future__ import annotations

import pytest

from app.api import chat_routes
from app.generation import answer as answer_mod
from app.generation import source_map
from tests.api.test_chat_contract import (  # noqa: F401 —— 复用同一套夹具与工具
    _StubClassifier,
    client,
    faux_model,
    parse_sse,
)


@pytest.fixture
def scenario_models(monkeypatch):
    """给三个场景各排一条 Faux 响应（按调用顺序消费）。"""

    def _install(*replies: str, classify_as: str = "external"):
        model = faux_model(replies[0], list(replies[1:]))
        monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
        monkeypatch.setattr(
            chat_routes, "resolve_routing_classifier", lambda: _StubClassifier(classify_as)
        )
        return model

    return _install


# --------------------------------------------------------------------------
# 场景 1：库外
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario1_external_question(client, scenario_models, db):
    """「今天天气怎么样」→ external → general，无假引用。"""
    scenario_models("今天晴，气温 20 度。", classify_as="external")

    resp = await client.post("/api/chat", json={"message": "今天天气怎么样"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["kind"] == "direct", "库外问题 → direct"
    assert body["answer_source"] == "general", "库外 → general"
    assert body["refs"] == [], "I7：库外回答**无假引用**"
    assert body["message"] == "今天晴，气温 20 度。"

    # 落库同源同值
    await _assert_persisted(db, body["chat_id"], kind="direct", answer_source="general",
                            source_type="external", answer="今天晴，气温 20 度。")


# --------------------------------------------------------------------------
# 场景 2：库内指令
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario2_internal_instruction_direct(client, scenario_models, db):
    """「知识库里关于项目周报的内容」→ internal（检索可空）→ 开关开 → direct。"""
    scenario_models("根据我自己的知识……", classify_as="external")

    resp = await client.post("/api/chat", json={"message": "知识库里关于项目周报的内容"})
    body = resp.json()

    # 路由判定为库内（正则命中 kb_named）
    assert body["kind"] == "direct", "库内未命中 + 允许直答 → direct"
    assert body["answer_source"] == "general"
    assert body["refs"] == [], "阶段 1 无检索 → refs 恒空"

    await _assert_persisted(
        db, body["chat_id"], kind="direct", answer_source="general",
        source_type="internal_db", answer="根据我自己的知识……",
    )


@pytest.mark.asyncio
async def test_scenario2_internal_instruction_refuse(client, scenario_models, db, monkeypatch):
    """同一问题 + 开关关 → refuse（**空结果策略定死一种**，两种都已定死）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", False)
    scenario_models("不该被调用", classify_as="external")

    resp = await client.post("/api/chat", json={"message": "知识库里关于项目周报的内容"})
    body = resp.json()

    assert body["kind"] == "refuse"
    assert body["answer_source"] == "empty_scope"
    assert body["message"] == answer_mod.REFUSE_TEXT
    assert body["refs"] == []

    await _assert_persisted(
        db, body["chat_id"], kind="refuse", answer_source="empty_scope",
        source_type="internal_db", answer=answer_mod.REFUSE_TEXT,
    )


# --------------------------------------------------------------------------
# 场景 3：澄清
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario3_clarify(client, scenario_models, db):
    """「那个怎么样」→ clarify 追问，refs 空。"""
    scenario_models("你指的是哪份文档？")

    resp = await client.post("/api/chat", json={"message": "那个怎么样"})
    body = resp.json()

    assert body["kind"] == "clarify"
    assert body["answer_source"] == "clarify"
    assert body["refs"] == []
    assert body["message"] == "你指的是哪份文档？", "正文就是追问原文"

    await _assert_persisted(
        db, body["chat_id"], kind="clarify", answer_source="clarify",
        source_type="external", answer="你指的是哪份文档？",
    )


# --------------------------------------------------------------------------
# 三类的相互区分（I7 核心 + 阶段验收标准 #1）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_scenarios_are_mutually_distinguishable(client, scenario_models, db):
    """三类同会话连问：kind / answer_source 两两不同，且各自落库正确。

    这是阶段 1 验收标准 #1「三类问题路由与 source_type 正确落库」的直译。
    """
    scenario_models(
        "今天晴。",              # 场景1 直答
        "你指的是哪份文档？",      # 场景3 追问
        "库内直答。",            # 场景2 直答
        classify_as="external",
    )

    questions = [
        "今天天气怎么样",
        "那个怎么样",
        "知识库里关于项目周报的内容",
    ]
    results = []
    chat_id = None
    for q in questions:
        payload = {"message": q}
        if chat_id:
            payload["chat_id"] = chat_id
        r = await client.post("/api/chat", json=payload)
        assert r.status_code == 200
        results.append(r.json())
        chat_id = r.json()["chat_id"]

    kinds = [r["kind"] for r in results]
    sources = [r["answer_source"] for r in results]

    assert kinds == ["direct", "clarify", "direct"], kinds
    assert sources == ["general", "clarify", "general"], sources
    # clarify 与 direct 必须能区分（I7：不把澄清误判成拒答/直答）
    assert results[1]["kind"] != results[0]["kind"]
    assert results[1]["answer_source"] != results[0]["answer_source"]
    # 三类都无假引用
    assert all(r["refs"] == [] for r in results)

    # 三条都落库，kind / source_type 各正确
    await db.rollback()
    from sqlalchemy import select

    from app.models import Message

    rows = list(
        (await db.execute(select(Message).where(Message.chat_id == chat_id).order_by(Message.id)))
        .scalars()
        .all()
    )
    assistant_rows = [r for r in rows if r.role == "assistant"]
    assert [(r.kind, r.answer_source, r.source_type) for r in assistant_rows] == [
        ("direct", "general", "external"),
        ("clarify", "clarify", "external"),
        ("direct", "general", "internal_db"),
    ]


# --------------------------------------------------------------------------
# I9：三类轮次都留决策（回归修复 的场景级复验）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_decisions_cover_all_three(client, scenario_models, db):
    """三类场景各留一条决策，来源分别可辨（I9 + 回归修复 回归）。"""
    from app.routing import decisions as routing_decisions

    scenario_models("今天晴。", "你指的是哪份文档？", "库内直答。", classify_as="external")

    chat_id = None
    for q in ["今天天气怎么样", "那个怎么样", "知识库里关于项目周报的内容"]:
        payload = {"message": q}
        if chat_id:
            payload["chat_id"] = chat_id
        r = await client.post("/api/chat", json=payload)
        chat_id = r.json()["chat_id"]

    await db.rollback()
    decisions = await routing_decisions.load_decisions(db, chat_id)
    assert len(decisions) == 3, f"三轮应三条决策，实际 {len(decisions)}"
    assert [d.source for d in decisions] == ["llm", "clarify", "regex"]
    assert [d.source_type for d in decisions] == ["external", "external", "internal_db"]


# --------------------------------------------------------------------------
# 流式版本：三类场景在 SSE 上同样成立
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenarios_over_sse(client, scenario_models):
    """三类场景走 /chat/stream：done 帧的 kind/answer_source 与同步一致。"""
    scenario_models("今天晴。", "你指的是哪份文档？", classify_as="external")

    expected = [("direct", "general"), ("clarify", "clarify")]
    chat_id = None
    for idx, q in enumerate(["今天天气怎么样", "那个怎么样"]):
        payload = {"message": q}
        if chat_id:
            payload["chat_id"] = chat_id
        resp = await client.post("/api/chat/stream", json=payload)
        events = parse_sse(resp.text)
        chat_id = events[0]["chat_id"]
        done = events[-1]
        assert done["type"] == "done"
        assert (done["kind"], done["answer_source"]) == expected[idx], q
        assert done["refs"] == []


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------


async def _assert_persisted(
    db, chat_id: int, *, kind: str, answer_source: str, source_type: str, answer: str
) -> None:
    """校验落库值（rollback 拿新快照，避开 REPEATABLE READ）。"""
    from sqlalchemy import select

    from app.models import Message

    await db.rollback()
    rows = list(
        (await db.execute(select(Message).where(Message.chat_id == chat_id).order_by(Message.id)))
        .scalars()
        .all()
    )
    assert [r.role for r in rows] == ["user", "assistant"], rows
    assistant = rows[1]
    assert assistant.kind == kind
    assert assistant.answer_source == answer_source
    assert assistant.source_type == source_type
    assert assistant.content == answer
    assert assistant.refs == []
    # 用户消息也带 source_type（路由结论落库）
    assert rows[0].source_type == source_type


def test_scenario_mapping_matches_section_3_8():
    """场景与 §3.8 映射表逐行对得上（防止实现与文档漂移）。"""
    # 场景1：external → direct / general
    assert source_map.map_round("external", has_materials=False, direct_enabled=True) == (
        "direct",
        "general",
        [],
    )
    # 场景2a：internal 未命中 + 允许直答 → direct / general
    assert source_map.map_round("internal_db", has_materials=False, direct_enabled=True) == (
        "direct",
        "general",
        [],
    )
    # 场景2b：internal 未命中 + 禁止直答 → refuse / empty_scope
    assert source_map.map_round("internal_db", has_materials=False, direct_enabled=False) == (
        "refuse",
        "empty_scope",
        [],
    )
    # 场景3：澄清 → clarify / clarify
    assert source_map.map_clarify_round() == ("clarify", "clarify", [])
