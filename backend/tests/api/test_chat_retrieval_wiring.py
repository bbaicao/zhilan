"""接线四条用例（阶段 3 · 条目 3.1 §二「接线与验证要求」）。

- ① **范围传递**：``payload.filters`` 必须传进检索（:func:`test_filters_are_forwarded_to_retrieval`）；
- ② **检索正文进轮次数据**：``TurnPlan`` 带「快照 + 渲染正文」且同源
  （:func:`test_turn_plan_carries_material_snapshot_and_rendered_text`）；
- ③ **两路都消费资料**：同步走 ``answer_direct``，流式走 ``build_messages`` + 系统提示按
  ``prompt_has_material`` 选（下面四条「有/无资料」用例断言的是**模型真正收到的东西**）；
- ④ **两态各一例**：同步 / 流式 × 有资料 / 无资料 = 4 例。

模型用本地 :class:`_RecorderModel`（记录收到的消息与系统提示，返回固定回答）——既有
``faux_model`` 只回文本，看不出「资料区到底有没有进提示词」。向量路由 ``tests/api/conftest.py``
的 autouse 夹具钉成假件，所以「有资料」那一态走的是**关键词路**（真库、真 SQL）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api import chat_routes
from app.core.config import settings
from app.generation import answer as answer_mod
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401 —— client 是夹具
from tests.retrieval.conftest import seed_document


class _FakeStream:
    """最小流：按片段产 ``text_delta``（与 CubeLoop 的事件形状一致）。"""

    def __init__(self, parts: list[str]) -> None:
        self._parts = parts

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for part in self._parts:
            yield SimpleNamespace(type="text_delta", delta=part)


class _RecorderModel:
    """假 bound model：**记录模型收到的消息与系统提示**，并返回固定回答。

    接口与 ``chat_routes`` 用到的两处一致：``await generate(messages, system_prompt=..., ...)``
    与 ``await stream(messages, system_prompt=...)``。
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def generate(self, messages, system_prompt=None, max_output_tokens=None):  # noqa: ANN001
        self.calls.append(
            {"messages": messages, "system_prompt": system_prompt, "mode": "generate"}
        )
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

    @property
    def last_user_text(self) -> str:
        message = self.last["messages"][-1]
        return "".join(
            getattr(block, "text", "")
            for block in (getattr(message, "content", None) or [])
            if getattr(block, "text", None)
        )


def _install(monkeypatch, *, reply: str, label: str = "internal_db") -> _RecorderModel:
    """装好模型与路由分类器（不连真模型；澄清关掉，避免它抢占这一轮）。"""
    recorder = _RecorderModel(reply)
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: recorder)
    monkeypatch.setattr(chat_routes, "resolve_routing_classifier", lambda: _StubClassifier(label))
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    return recorder


MATERIAL_CHUNK = "向量库使用 Qdrant 内嵌模式，向量化由上游接口完成。"
MATERIAL_FILENAME = "检索手册.md"


# --- ④ 同步 × 有资料 --------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_with_materials_feeds_material_block(client, db, monkeypatch):
    document = await seed_document(db, [MATERIAL_CHUNK], filename=MATERIAL_FILENAME)
    recorder = _install(monkeypatch, reply="据资料 [1]，向量库用 Qdrant。")

    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "normal"
    assert body["answer_source"] == "kb"
    assert body["message"] == "据资料 [1]，向量库用 Qdrant。"

    assert recorder.last["mode"] == "generate"
    prompt_text = recorder.last_user_text
    assert "资料如下：" in prompt_text
    assert f"[1]（文档：{MATERIAL_FILENAME}）" in prompt_text
    assert MATERIAL_CHUNK in prompt_text
    # 有编号资料同时带 U4 的逐事实引用约束；来源身份仍与原文一致。
    assert recorder.last["system_prompt"] == (
        answer_mod.SYSTEM_PROMPT + "\n\n" + answer_mod.CITED_ANSWER_CONSTRAINTS
    )
    assert len(body["refs"]) == 1
    assert body["refs"][0]["source_id"] == document.id
    assert body["refs"][0]["ordinal"] == 1
    assert body["refs"][0]["citation_mode"] == "cited"


# --- ④ 同步 × 无资料 --------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_without_materials_keeps_no_material_wording(client, db, monkeypatch):
    recorder = _install(monkeypatch, reply="我按自己的知识回答。")

    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "direct"
    assert body["answer_source"] == "general"

    prompt_text = recorder.last_user_text
    assert "资料如下" not in prompt_text
    assert "知识库" not in prompt_text  # 无资料时不许出现「资料」措辞（I7 的反面）
    assert recorder.last["system_prompt"] == answer_mod.NO_MATERIAL_SYSTEM_PROMPT


# --- ④ 流式 × 有资料 --------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_with_materials_feeds_material_block(client, db, monkeypatch):
    document = await seed_document(db, [MATERIAL_CHUNK], filename=MATERIAL_FILENAME)
    recorder = _install(monkeypatch, reply="据资料 [1]，向量库用 Qdrant。")

    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "向量库用什么存储？"}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")

    events = parse_sse(raw)
    kinds = [event["type"] for event in events]
    assert kinds[0] == "meta"
    assert kinds[-1] == "done"
    assert "".join(e["text"] for e in events if e["type"] == "token") == recorder.reply
    done = events[-1]
    assert done["kind"] == "normal"
    assert done["answer_source"] == "kb"

    assert recorder.last["mode"] == "stream"
    assert f"[1]（文档：{MATERIAL_FILENAME}）" in recorder.last_user_text
    assert MATERIAL_CHUNK in recorder.last_user_text
    assert recorder.last["system_prompt"] == (
        answer_mod.SYSTEM_PROMPT + "\n\n" + answer_mod.CITED_ANSWER_CONSTRAINTS
    )
    assert len(done["refs"]) == 1
    assert done["refs"][0]["source_id"] == document.id
    assert done["refs"][0]["ordinal"] == 1
    assert done["refs"][0]["citation_mode"] == "cited"


# --- ④ 流式 × 无资料 --------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_without_materials_keeps_no_material_wording(client, db, monkeypatch):
    recorder = _install(monkeypatch, reply="我按自己的知识回答。")

    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "向量库用什么存储？"}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")

    done = parse_sse(raw)[-1]
    assert done["type"] == "done"
    assert done["kind"] == "direct"
    assert done["answer_source"] == "general"

    assert "资料如下" not in recorder.last_user_text
    assert recorder.last["system_prompt"] == answer_mod.NO_MATERIAL_SYSTEM_PROMPT


# --- ① 范围传递 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_filters_are_forwarded_to_retrieval(client, db, monkeypatch):
    """``payload.filters`` 必须原样进检索（此前调用点只传了问题文字）。"""
    seen: dict = {}

    async def _fake_retrieve(session, text, filters=None, *, stats=None):  # noqa: ANN001
        seen["text"] = text
        seen["filters"] = filters
        return [], "", None

    monkeypatch.setattr(chat_routes, "retrieve_materials", _fake_retrieve)
    _install(monkeypatch, reply="好的。")

    resp = await client.post(
        "/api/chat",
        json={
            "message": "向量库怎么用",
            "filters": {"kb_ids": [7], "categories": ["技术"], "tags": ["重要"]},
        },
    )

    assert resp.status_code == 200
    assert seen["text"] == "向量库怎么用"
    assert seen["filters"] == {"kb_ids": [7], "categories": ["技术"], "tags": ["重要"]}


# --- ② 快照与渲染正文同源 ---------------------------------------------------


@pytest.mark.asyncio
async def test_turn_plan_carries_material_snapshot_and_rendered_text(db, monkeypatch):
    """``TurnPlan`` 同时带**快照**（列表，已编号）与**渲染正文**（字符串），且二者同源。"""
    from app.api.chat_schemas import ChatRequest

    document = await seed_document(db, [MATERIAL_CHUNK], filename=MATERIAL_FILENAME)
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)

    plan = await chat_routes.build_turn_plan(
        db,
        ChatRequest(message="向量库用什么存储？"),
        "向量库用什么存储？",
        [],
        routing_classifier=_StubClassifier("internal_db"),
    )

    assert plan.kind == "normal"
    assert len(plan.materials) == 1
    only = plan.materials[0]
    assert only.document_id == document.id
    assert only.ordinal == 1  # 快照编号 = 模型看到的 [n]
    assert f"[1]（文档：{MATERIAL_FILENAME}）" in plan.materials_text
    assert MATERIAL_CHUNK in plan.materials_text
    assert answer_mod.has_materials(materials=plan.materials) is True
    assert answer_mod.prompt_has_material(plan.materials_text) is True
