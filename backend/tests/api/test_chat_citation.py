"""引用与归因的端到端用例（阶段 3 · 条目 3.2）。

打的是真 HTTP 链路（ASGI 直连）+ 真测试库；模型用记模型的假件（``_RecorderModel``）——
要断言的是「模型写的 ``[n]`` → 落库的 refs 与 ``message_attributions`` 一致」这条闭环。

向量路由 ``tests/api/conftest.py`` 的 autouse 夹具钉成假件，所以「有资料」那一态走关键词路。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Chunk, Message, MessageAttribution
from tests.api.test_chat_contract import client, parse_sse  # noqa: F401 —— client 是夹具
from tests.api.test_chat_retrieval_wiring import _install
from tests.retrieval.conftest import seed_document

CHUNK_TEXT = "向量库使用 Qdrant 内嵌模式，向量化由上游接口完成。"


async def _assistant_message(db, chat_id: int) -> Message:
    rows = (
        await db.execute(
            select(Message)
            .where(Message.chat_id == chat_id, Message.role == "assistant")
            .order_by(Message.id.desc())
        )
    ).scalars().all()
    assert rows, "没有落库的 assistant 消息"
    return rows[0]


async def _attributions(db, message_id: int) -> list[MessageAttribution]:
    rows = (
        await db.execute(
            select(MessageAttribution)
            .where(MessageAttribution.message_id == message_id)
            .order_by(MessageAttribution.ordinal)
        )
    ).scalars().all()
    return list(rows)


@pytest.mark.asyncio
async def test_sync_reply_with_marker_writes_matching_refs_and_attributions(client, db, monkeypatch):
    document = await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    _install(monkeypatch, reply="据资料 [1]，向量库用 Qdrant。")

    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["kind"] == "normal"
    assert len(body["refs"]) == 1
    ref = body["refs"][0]
    assert ref["ordinal"] == 1
    assert ref["type"] == "document"
    assert ref["title"] == "检索手册.md"
    assert ref["snippet"].startswith("向量库使用 Qdrant")
    assert ref["source_id"] == document.id
    assert ref["chunk_id"].startswith(f"kb:1:document:{document.id}:")
    assert ref["retrieval_id"].startswith("rl-")
    assert ref["citation_mode"] == "cited"
    assert 0.0 < ref["score"] <= 1.0

    # 消息行里落的就是同一份 refs
    message = await _assistant_message(db, body["chat_id"])
    assert message.refs == body["refs"]

    # 归因行与 [n] 一致
    chunk_id = (await db.execute(select(Chunk.id))).scalars().one()
    rows = await _attributions(db, message.id)
    assert len(rows) == 1
    assert rows[0].ordinal == 1
    assert rows[0].chunk_id == chunk_id  # 列里存的是 chunks.id（FK），不是冻结形态的字符串
    assert rows[0].meta["chunk_key"] == ref["chunk_id"] == f"kb:1:document:{document.id}:1"
    assert rows[0].meta["citation_mode"] == "cited"
    assert rows[0].meta["retrieval_id"] == ref["retrieval_id"]
    assert rows[0].title == "检索手册.md"


@pytest.mark.asyncio
async def test_sync_reply_without_marker_falls_back_to_all_sources(client, db, monkeypatch):
    await seed_document(db, [CHUNK_TEXT], filename="甲.md")
    await seed_document(db, ["向量库也支持关键词检索。"], filename="乙.md")
    _install(monkeypatch, reply="我没有写编号，但本轮有两条资料。")

    resp = await client.post("/api/chat", json={"message": "向量库"})
    body = resp.json()

    assert body["kind"] == "normal"
    assert len(body["refs"]) == 2
    assert {ref["citation_mode"] for ref in body["refs"]} == {"fallback"}
    assert [ref["ordinal"] for ref in body["refs"]] == [1, 2]

    message = await _assistant_message(db, body["chat_id"])
    rows = await _attributions(db, message.id)
    assert [row.ordinal for row in rows] == [1, 2]


@pytest.mark.asyncio
async def test_out_of_range_marker_falls_back_rather_than_dropping_sources(client, db, monkeypatch):
    await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    _install(monkeypatch, reply="据资料 [7] 可知……")

    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    refs = resp.json()["refs"]

    assert [ref["ordinal"] for ref in refs] == [1]
    assert refs[0]["citation_mode"] == "fallback"  # 越界不算有效编号 → 兜底带出全部


@pytest.mark.asyncio
async def test_direct_round_has_no_refs_and_no_attributions(client, db, monkeypatch):
    _install(monkeypatch, reply="我按自己的知识回答。")

    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    body = resp.json()

    assert body["kind"] == "direct"
    assert body["refs"] == []
    message = await _assistant_message(db, body["chat_id"])
    assert await _attributions(db, message.id) == []


@pytest.mark.asyncio
async def test_stream_round_carries_refs_into_done_and_attributions(client, db, monkeypatch):
    document = await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    _install(monkeypatch, reply="据资料 [1]，向量库用 Qdrant。")

    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "向量库用什么存储？"}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")

    done = parse_sse(raw)[-1]
    assert done["type"] == "done"
    assert done["kind"] == "normal"
    assert len(done["refs"]) == 1
    assert done["refs"][0]["chunk_id"] == f"kb:1:document:{document.id}:1"

    message = await _assistant_message(db, done["chat_id"])
    rows = await _attributions(db, message.id)
    assert len(rows) == 1
    assert rows[0].ordinal == 1
    assert rows[0].meta["retrieval_id"] == done["refs"][0]["retrieval_id"]


@pytest.mark.asyncio
async def test_two_rounds_keep_their_own_attributions(client, db, monkeypatch):
    """两轮各自的归因互不污染（每轮的快照 id 与编号都是本轮自己的）。"""
    await seed_document(db, [CHUNK_TEXT], filename="检索手册.md")
    _install(monkeypatch, reply="据资料 [1] 可知。")

    first = (await client.post("/api/chat", json={"message": "向量库用什么存储？"})).json()
    second = (
        await client.post(
            "/api/chat", json={"message": "再说一次向量库用什么？", "chat_id": first["chat_id"]}
        )
    ).json()

    assert first["refs"][0]["retrieval_id"] != second["refs"][0]["retrieval_id"]

    messages = (
        await db.execute(
            select(Message)
            .where(Message.chat_id == first["chat_id"], Message.role == "assistant")
            .order_by(Message.id)
        )
    ).scalars().all()
    assert len(messages) == 2
    rows_first = await _attributions(db, messages[0].id)
    rows_second = await _attributions(db, messages[1].id)
    assert len(rows_first) == 1 and len(rows_second) == 1
    assert rows_first[0].meta["retrieval_id"] == first["refs"][0]["retrieval_id"]
    assert rows_second[0].meta["retrieval_id"] == second["refs"][0]["retrieval_id"]
