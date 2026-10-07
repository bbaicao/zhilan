"""条目 `5.3` 端到端接线用例（真检索链路 + SSE 契约面）。

覆盖开工包 §5 的**接线面**断言：

| 用例 | 断言 |
|---|---|
| ``test_a6_*`` | **A6**（开/关两态 ``done`` 帧键集完全相同、``meta`` 形状相同、``retrieval/result`` 既有键照旧） |
| ``test_a7_*`` | **A7**（开态下 ``refs`` 的编号与资料区同源、``message_attributions`` 正常落库、``chunk_id`` 是真实 ``chunks.id``） |
| ``test_a11_*`` | **A11**（关态 HTTP 一轮：零 ``search_graph`` 调用） |
| ``test_a18_*`` | **A18**（与 `5.2` 互不吞没：两轮命中且正文逐字相同；改 ``GRAPHRAG_ENABLE`` ⇒ 不命中） |

⚠️ **为什么本条需要 ``tests/api`` 的用例**（而不是全写在 ``tests/retrieval`` 里）：
A6/A7/A18 断的是 **HTTP/SSE 契约面与落库**，只有这里的基建能提供——
① ``client``（ASGI 直连，不起真端口）；② ``tests/api/conftest.py`` 的**七个 autouse 钉子**
（向量路假件 / 重排关 / 联网关 / 记忆关 / 缓存关 / **图谱关**）——少一个就会真出网。
把这三条塞进 ``tests/retrieval`` 等于在那边**重造**这套钉子，风险远大于多一个测试文件。

用工具体例照 5.1/5.2 的先例**跨文件复用**（``from tests.api.test_chat_contract import ...``），
不重复造 ``client`` / ``parse_sse``。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import chat_routes
from app.cache import store as cache_store
from app.core.config import settings
from app.models import Chunk, Message, MessageAttribution, SessionEvent, WikiLink
from app.retrieval import graph
# client / parse_sse / _StubClassifier 都在契约套件里（不重复造）
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401
from tests.retrieval.conftest import seed_document

ANCHOR_FILENAME = "系统概览.md"
ANCHOR_TEXT = "系统概览 讲的是整体架构。"
NEIGHBOUR_FILENAME = "向量索引.md"
NEIGHBOUR_TEXT = "倒排表与近邻图都存在本地。"
QUESTION = "系统概览"
ANSWER = "据资料 [1][2]，整体架构见系统概览。"

#: 冻结的聊天 ``done`` 帧键集（与 ``test_chat_contract.py`` 的那份**同源同值**）。
#: 🔴 2026-09-27：`回归修复` 拍板⑨「补」⇒ done 帧新增第 10 键 `recovered`（02 v1.0.12）。
DONE_KEYS = {
    "type",
    "chat_id",
    "refs",
    "kind",
    "reasoning",
    "thinking",
    "skill_ids",
    "agent_trace",
    "answer_source",
    "recovered",
}


class _FakeStream:
    """最小流：把整段回答当**一个** ``text_delta`` 发（与 CubeLoop 的事件形状一致）。"""

    def __init__(self, parts: list[str]) -> None:
        self._parts = parts

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for part in self._parts:
            yield SimpleNamespace(type="text_delta", delta=part)


class _CountingModel:
    """假 bound model：**记录被调用次数**（A18 的「命中轮没再调模型」断在它身上）。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[str] = []

    async def generate(self, messages, system_prompt=None, max_output_tokens=None, **extra):  # noqa: ANN001
        self.calls.append("generate")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.reply)], stop_reason="end_turn"
        )

    async def stream(self, messages, system_prompt=None, **extra):  # noqa: ANN001
        self.calls.append("stream")
        return _FakeStream([self.reply])

    @property
    def count(self) -> int:
        return len(self.calls)


def _install(monkeypatch, reply: str = ANSWER):
    """装假模型 + 固定分类器（不连真上游；``internal_db`` = 走库内回答链路）。"""
    model = _CountingModel(reply)
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )
    return model


@pytest.fixture
def graph_on(monkeypatch):
    """开态 + 清计数（``tests/api/conftest.py`` 的钉子把默认态钉成关，这里显式打开）。"""
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()
    yield settings
    graph.reset_counters()


@pytest.fixture
def cache_on(monkeypatch):
    """语义缓存开态 + 清表（口径同 5.2 的 ``cache_on``）。"""
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    cache_store.reset_cache()
    cache_store.reset_failure_counts()
    yield settings
    cache_store.reset_cache()
    cache_store.reset_failure_counts()


async def _seed_two_documents(db):
    """造 A（主命中）与 B（**只可能由图通道带回**的邻居）并把两篇互链起来。

    返回 ``(anchor_id, neighbour_id, {document_id: chunk_id})``——**全是裸 int**：
    ``db.rollback()`` 会让 ORM 对象过期，之后再读它们的属性会触发一次**同步**懒加载
    （``MissingGreenlet``，本文件自测踩过），所以这一步就把 id 摘干净。
    """
    anchor = await seed_document(db, [ANCHOR_TEXT], filename=ANCHOR_FILENAME)
    neighbour = await seed_document(db, [NEIGHBOUR_TEXT], filename=NEIGHBOUR_FILENAME)
    anchor_id, neighbour_id = int(anchor.id), int(neighbour.id)
    db.add(
        WikiLink(
            source_type="document",
            source_id=anchor_id,
            kb_id=1,
            target_title=NEIGHBOUR_FILENAME,
            target_document_id=neighbour_id,
            resolved=True,
        )
    )
    await db.commit()
    rows = (await db.execute(select(Chunk.id, Chunk.document_id).order_by(Chunk.id))).all()
    by_document = {int(document_id): int(chunk_id) for chunk_id, document_id in rows}
    return anchor_id, neighbour_id, by_document


def _done(frames: list[dict]) -> dict:
    return [item for item in frames if item["type"] == "done"][-1]


# --------------------------------------------------------------------------
# A6 —— 两态 done 帧键集完全相同
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a6_done_and_meta_shapes_are_identical_in_both_states(
    client, db, monkeypatch, graph_on
):
    """**A6**：开/关两态的 ``done`` 与 ``meta`` 键集**完全相同**——图通道一个键都不加。

    同时断一条**正向证据**：开态真的多回了一条材料（否则「键集相同」可能只是没生效）。
    """
    await _seed_two_documents(db)
    _install(monkeypatch)

    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)
    graph.reset_counters()
    off_frames = parse_sse(
        (await client.post("/api/chat/stream", json={"message": QUESTION})).text
    )

    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()
    on_frames = parse_sse(
        (await client.post("/api/chat/stream", json={"message": QUESTION})).text
    )

    done_off, done_on = _done(off_frames), _done(on_frames)
    assert set(done_on) == set(done_off), (
        f"多 {set(done_on) - set(done_off)} / 少 {set(done_off) - set(done_on)}"
    )
    assert set(done_on) == DONE_KEYS, "done 帧键集被改动了（契约禁令）"
    assert set(off_frames[0]) == set(on_frames[0]), "meta 帧形状变了"
    assert done_on["type"] == "done"

    # 正向证据：开态多一条引用（图通道真的多回了一条材料）
    assert len(done_off["refs"]) == 1
    assert len(done_on["refs"]) == 2
    assert graph.failure_counts()["calls"] == 1

    # `retrieval/result` 的**既有键**照旧（只断存在性：本条目不加键）
    await db.rollback()
    events = list(
        (
            await db.execute(
                select(SessionEvent)
                .where(SessionEvent.type == "retrieval/result")
                .order_by(SessionEvent.id)
            )
        )
        .scalars()
        .all()
    )
    assert events, "两轮都该有 retrieval/result 事件"
    payload = events[-1].data
    for key in ("hits", "from_vector", "from_keyword", "degraded", "warning"):
        assert key in payload, f"retrieval/result 少了既有键 {key}：{sorted(payload)}"
    assert payload["hits"] == 2, f"开态应当有 2 条材料：{payload}"


# --------------------------------------------------------------------------
# A7 —— 引用与归因不破
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a7_refs_and_attributions_stay_intact_with_the_channel_on(
    client, db, monkeypatch, graph_on
):
    """**A7**：开态下 ``refs`` 的 ``ordinal`` 与资料区同源、归因正常落库、``chunk_id`` 真实。"""
    anchor_id, neighbour_id, by_document = await _seed_two_documents(db)
    _install(monkeypatch)

    response = await client.post("/api/chat", json={"message": QUESTION})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "normal"
    refs = body["refs"]
    assert len(refs) == 2, f"开态应当回两条材料：{refs}"
    assert [item["ordinal"] for item in refs] == [1, 2], "引用编号必须与资料区同源"
    assert [item["type"] for item in refs] == ["document", "document"]
    assert {item["title"] for item in refs} == {ANCHOR_FILENAME, NEIGHBOUR_FILENAME}

    await db.rollback()
    messages = list(
        (
            await db.execute(
                select(Message).where(Message.chat_id == int(body["chat_id"])).order_by(Message.id)
            )
        )
        .scalars()
        .all()
    )
    assert [item.role for item in messages] == ["user", "assistant"]
    assistant_id = int(messages[1].id)

    rows = list(
        (
            await db.execute(
                select(MessageAttribution)
                .where(MessageAttribution.message_id == assistant_id)
                .order_by(MessageAttribution.ordinal)
            )
        )
        .scalars()
        .all()
    )
    assert [row.ordinal for row in rows] == [1, 2], "归因行一条不少"
    expected_ids = {by_document[anchor_id], by_document[neighbour_id]}
    assert {int(row.chunk_id) for row in rows} == expected_ids
    assert all(int(row.chunk_id) > 0 for row in rows), "归因的 chunk_id 必须是真实 chunks.id"

    real_ids = set((await db.execute(select(Chunk.id))).scalars().all())
    assert {int(row.chunk_id) for row in rows} <= real_ids


# --------------------------------------------------------------------------
# A11 —— 关态零调用（HTTP 面复核）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a11_disabled_turn_makes_zero_channel_calls(client, db, monkeypatch):
    """**A11**（HTTP 面）：关态跑一整轮问答，``search_graph`` 一次都不许被调。"""
    await _seed_two_documents(db)
    _install(monkeypatch)

    calls: list[str] = []
    original = graph.search_graph

    async def _counting(*args, **kwargs):
        calls.append("called")
        return await original(*args, **kwargs)

    monkeypatch.setattr(graph, "search_graph", _counting)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)

    response = await client.post("/api/chat", json={"message": QUESTION})
    assert response.status_code == 200, response.text
    assert response.json()["refs"], "关态关键词路照常命中（这条用例的前提）"
    assert calls == []
    assert graph.failure_counts() == {}


# --------------------------------------------------------------------------
# A18 —— 与 5.2 语义缓存互不吞没
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a18_cache_and_graph_do_not_swallow_each_other(
    client, db, monkeypatch, graph_on, cache_on
):
    """**A18**：① 两轮命中且正文逐字相同；② 改 ``GRAPHRAG_ENABLE`` 后**不命中**（指纹生效）。

    ``GRAPHRAG_ENABLE`` 进 `retrieval_fingerprint` 的 ``graph`` 子对象（A12③）——本用例是
    它**端到端**的验证：块一条都没变，只有「怎么选块」变了 ⇒ 必须换键、必须重跑检索。
    """
    await _seed_two_documents(db)
    model = _install(monkeypatch)

    first = await client.post("/api/chat", json={"message": QUESTION})
    assert first.status_code == 200, first.text
    await chat_routes.drain_cache_tasks()
    assert cache_store.cache_size() == 1, "第一轮应当写回一条缓存"
    assert cache_store.failure_counts().get("hit", 0) == 0
    assert model.count == 1, "第一轮必然要调模型"
    assert len(first.json()["refs"]) == 2, "开态第一轮应当回两条材料"

    # ① 第二轮（新会话 ⇒ 空历史 ⇒ 同键）必须命中，正文逐字相同、模型不再跑
    second = await client.post("/api/chat", json={"message": QUESTION})
    assert second.status_code == 200, second.text
    assert cache_store.failure_counts()["hit"] >= 1, "第二轮没命中 ⇒ 本用例什么都没测到"
    assert second.json()["message"] == first.json()["message"]
    assert model.count == 1, f"命中轮不该再调模型（实际 {model.count} 次）"

    # ② 关掉图通道再问：块没变、开关变了 ⇒ **必须不命中**（否则就是「开启时的答案被复用」）
    before_hits = cache_store.failure_counts()["hit"]
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)
    third = await client.post("/api/chat", json={"message": QUESTION})
    assert third.status_code == 200, third.text
    assert cache_store.failure_counts()["hit"] == before_hits, (
        "改 GRAPHRAG_ENABLE 之后仍命中了旧答案 ⇒ graph 没进缓存键（A12③ 的端到端反例）"
    )
    assert model.count == 2, "关态是**新的一轮**：按无图通道重跑检索与模型"
    assert len(third.json()["refs"]) == 1, "关态应当只有主命中那一条材料"
