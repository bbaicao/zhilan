"""S6 · 条目 161-B 取证：同步（非流式）问答恒不返回 reasoning / thinking。

口径（选 B = **承认同步能力边界**，不是修好它；本批不改 app/**）：
- backend/app/api/chat_routes.py:1902-1913：正常收口 ChatResponse(..., reasoning="", thinking=None, ...)；
- backend/app/api/chat_routes.py:1845-1864：提示（hint）收口同为空 —— 且**不落 assistant 行**；
- 缓存重放（plan.cached）走的是同一条**正常**收口代码（只跳过计费与写缓存）⇒ 同样恒空。

三条腿分别覆盖正常 / 提示 / 缓存重放三种响应形态。判别力：任一处把 reasoning 改接模型侧真值、
把 thinking 从 None 改成对象、或让 hint 分支顺手落一条 assistant 行 —— 对应腿必红。
"""

from __future__ import annotations

import pytest

from app.api import chat_routes
from tests.api.test_bug154_sync_error_semantics import _FailingModel, _error_result, _install as install_failure
from tests.api.test_chat_contract import client, use_faux  # noqa: F401
from tests.api.test_semantic_cache_http import (  # noqa: F401  复用既有「缓存命中」范式的假件
    _RetrievalCounter,
    _flush_cache_writes,
    _install,
    _materials_from,
    cache_on,
)

pytestmark = pytest.mark.asyncio


async def _roles(client, chat_id: int) -> list[str]:
    detail = await client.get(f"/api/chats/{chat_id}")
    assert detail.status_code == 200, detail.text
    return [row["role"] for row in detail.json()["messages"]]


async def test_normal_sync_reply_has_empty_reasoning_and_none_thinking(client, use_faux):  # noqa: F811
    """正常收口：reasoning == ""、thinking is None，且 assistant 行确实落库（前提自证）。"""
    reply = "S6-161B：正常同步回答的正文。"
    use_faux(reply)

    resp = await client.post("/api/chat", json={"message": "S6-161B 正常形态"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message"] == reply
    assert body["reasoning"] == ""
    assert body["thinking"] is None
    assert "assistant" in await _roles(client, body["chat_id"]), (
        "本腿必须走正常收口（assistant 行落库）；走成 hint 分支就说明前提不成立"
    )


async def test_hint_sync_reply_has_empty_reasoning_and_none_thinking(client, monkeypatch):  # noqa: F811
    """提示收口（hint 分支）：同样恒空，且**不落 assistant 行**（只有带提示的 user 行）。"""
    detail = "ProviderUnavailable: Connection error."
    install_failure(monkeypatch, _FailingModel(final=_error_result(
        error_type="ProviderUnavailable", error_message=detail,
    )))

    resp = await client.post("/api/chat", json={"message": "S6-161B 提示形态"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message"] == f"回答生成失败，请稍后重试。（{detail}）"
    assert body["reasoning"] == ""
    assert body["thinking"] is None
    assert body["refs"] == []
    roles = await _roles(client, body["chat_id"])
    assert "assistant" not in roles, "hint 分支只落 user 行（带提示 note），不落 assistant 行"


async def test_cached_replay_has_empty_reasoning_and_none_thinking(  # noqa: F811
    db, client, cache_on, monkeypatch
):
    """缓存重放：第二轮不检索、不调模型（前提自证），响应同样恒空。"""
    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)
    question = "S6-161B：向量库用什么存储？"

    first = await client.post("/api/chat", json={"message": question})
    assert first.status_code == 200, first.text
    assert first.json()["kind"] == "normal"
    await _flush_cache_writes()

    second = await client.post("/api/chat", json={"message": question})
    assert second.status_code == 200, second.text
    assert counter.count == 1 and model.count == 1, (
        f"第二轮没走语义缓存（本腿前提不成立）：检索 {counter.count} 次 / 模型 {model.count} 次"
    )
    body = second.json()
    assert body["reasoning"] == ""
    assert body["thinking"] is None
