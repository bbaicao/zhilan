"""器官的端到端用例（阶段 3 · 条目 3.3）：事件真的落进 ``session_events``、SSE 收尾恰好一次。

用真 HTTP 链路（ASGI 直连）+ 真测试库；模型用记模型的假件（见 ``test_chat_retrieval_wiring``）。
"""

from __future__ import annotations

import pytest

from app.api import chat_routes
from app.session import events as event_log
from tests.api.test_chat_contract import client, parse_sse  # noqa: F401 —— client 是夹具
from tests.api.test_chat_retrieval_wiring import _install
from tests.retrieval.conftest import seed_document

CHUNK_TEXT = "向量库使用 Qdrant 内嵌模式，向量化由上游接口完成。"


async def _events_by_type(db, chat_id: int, event_type: str):
    return await event_log.list_events_by_type(db, chat_id, event_type)


@pytest.mark.asyncio
async def test_normal_round_writes_trace_and_citation_events(client, db, monkeypatch):
    document = await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    _install(monkeypatch, reply="据资料 [1]，向量库用 Qdrant。")

    # 带上 request_id：它只允许挂在**一条**事件上（uk(chat_id, request_id)），
    # trace/citation 事件改用 trace 列串链——这条用例就是那个口径的回归钉
    resp = await client.post(
        "/api/chat", json={"message": "向量库用什么存储？", "request_id": "req-organs-1"}
    )
    assert resp.status_code == 200
    body = resp.json()
    chat_id = body["chat_id"]

    # trace：一次检索 = 一对事件，用同一个 retrieval_id 配对
    starts = await _events_by_type(db, chat_id, "retrieval/start")
    results = await _events_by_type(db, chat_id, "retrieval/result")
    assert len(starts) == 1 and len(results) == 1
    retrieval_id = results[0].data["retrieval_id"]
    assert starts[0].data["retrieval_id"] == retrieval_id
    assert starts[0].data["question"] == "向量库用什么存储？"
    assert results[0].data["hits"] == 1
    assert results[0].data["from_keyword"] == 1
    assert results[0].data["degraded"] is False
    assert starts[0].trace == results[0].trace == retrieval_id
    assert starts[0].request_id is None and results[0].request_id is None

    # 一轮里只有一条事件带 request_id（唯一的那个是路由决策）
    with_request = [
        row
        for row in await event_log.list_events(db, chat_id)
        if row.request_id is not None
    ]
    assert [row.type for row in with_request] == ["routing/decided"]

    # citation：模型写的 [1] 被判定为 used，且身份字段与响应里的 refs 同源
    used = await _events_by_type(db, chat_id, "citation/used")
    assert len(used) == 1
    payload = used[0].data
    assert payload["marker"] == "[1]"
    assert payload["ordinal"] == 1
    assert payload["chunk_id"] == body["refs"][0]["chunk_id"]
    assert payload["retrieval_id"] == retrieval_id
    assert used[0].trace == retrieval_id
    assert payload["chunk_id"].endswith(f"{document.id}:1")


@pytest.mark.asyncio
async def test_direct_round_records_unresolved_markers_with_available_zero(client, db, monkeypatch):
    """直答轮没有资料，模型若仍写编号 → ``citation/unresolved``（available=0），不静默。"""
    _install(monkeypatch, reply="据资料 [1] 可知……")

    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    body = resp.json()
    assert body["kind"] == "direct"

    unresolved = await _events_by_type(db, body["chat_id"], "citation/unresolved")
    assert len(unresolved) == 1
    assert unresolved[0].data == {"marker": "[1]", "reason": "out_of_range", "available": 0}


@pytest.mark.asyncio
async def test_refuse_round_writes_no_citation_events(client, db, monkeypatch):
    from app.core.config import settings

    _install(monkeypatch, reply="不该被调用。")
    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", False)

    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    body = resp.json()
    assert body["kind"] == "refuse"

    assert await _events_by_type(db, body["chat_id"], "citation/used") == []
    assert await _events_by_type(db, body["chat_id"], "citation/unresolved") == []


@pytest.mark.asyncio
async def test_degraded_vector_path_is_recorded_in_trace(client, db, monkeypatch):
    """向量路降级（embed 抛错）时 ``retrieval/result.degraded`` 必须为真。"""
    from app.retrieval import vector as retrieval_vector

    class _Boom:
        async def embed_texts(self, texts):  # noqa: ANN001
            raise RuntimeError("未配置 embed 模型（EMBED_* 或 MODEL_ROLES）")

    monkeypatch.setattr(
        retrieval_vector, "get_embedding_client", lambda *, fake=False: _Boom()
    )
    await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    _install(monkeypatch, reply="据资料 [1] 可知。")

    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    body = resp.json()

    results = await _events_by_type(db, body["chat_id"], "retrieval/result")
    assert len(results) == 1
    assert results[0].data["degraded"] is True
    assert "仅使用关键词检索" in results[0].data["warning"]
    # 降级不影响可用性：关键词路照样召回，回答仍是库内命中
    assert body["kind"] == "normal"
    assert results[0].data["hits"] == 1


@pytest.mark.asyncio
async def test_stream_ends_with_exactly_one_terminal_frame(client, db, monkeypatch):
    await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    _install(monkeypatch, reply="据资料 [1]，向量库用 Qdrant。")

    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "向量库用什么存储？"}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")

    events = parse_sse(raw)
    terminals = [event["type"] for event in events if event["type"] in ("done", "error")]
    assert terminals == ["done"]
    assert events[-1]["type"] == "done"

    chat_id = events[-1]["chat_id"]
    assert len(await _events_by_type(db, chat_id, "citation/used")) == 1
    assert len(await _events_by_type(db, chat_id, "retrieval/result")) == 1


@pytest.mark.asyncio
async def test_stream_error_path_still_terminates_once(client, db, monkeypatch):
    """落库失败 → **一条 error、没有 done**（guard：收尾恰好一次，且绝不静默结束）。"""
    await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    _install(monkeypatch, reply="据资料 [1] 可知。")

    async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("落库炸了")

    monkeypatch.setattr(chat_routes, "persist_turn", _boom)

    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "向量库用什么存储？"}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")

    events = parse_sse(raw)
    terminals = [event["type"] for event in events if event["type"] in ("done", "error")]
    assert terminals == ["error"]
    assert "保存失败" in events[-1]["detail"]
