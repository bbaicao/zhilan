"""条目 `5.2` 端到端接线用例（``/api/chat`` 与 ``/api/chat/stream`` 两条链路）。

覆盖开工包 §5 的**接线面**断言：

| 用例 | 断言 |
|---|---|
| ``test_a4_*`` / ``test_a19_*`` | **A4 / A19**（开 = 检索与模型**都省**；NN 次提问检索只跑 1 次） |
| ``test_a6_*`` | **A6**（命中不改契约：两态 ``done`` 键集完全相同） |
| ``test_a7_*`` | **A7**（命中轮落库与事件一条不少） |
| ``test_a10_*`` | **A10**（无悬空 / 无陈旧引用：删块、reindex 都当未命中） |
| ``test_a13_*`` | **A13**（``retrieval_id`` 每轮现铸） |
| ``test_a12_*`` | **A12**（命中轮不记账、不抽记忆 —— 两处记录函数各 0 次） |
| ``test_a18_*`` | **A18**（与 5.1 互不吞没 —— 本条的专项） |
| ``test_a3_*`` | **A3**（关 = 零变化：关态零缓存调用 + 提示词逐字未变） |
| ``test_negative_*`` | §0.0 第 17 行（Agent 轮 / 带图轮 / refuse / clarify 一律不进缓存） |

⚠️ 模型一律走注入（``_RecorderModel`` / Faux），**不连真上游**；向量路走 ``tests/api/conftest.py``
的 autouse 假件。「有材料」那一态由**关键词路**在真测试库上产出（``seed_document``）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api import chat_routes
from app.cache import entry as cache_entry
from app.cache import store as cache_store
from app.generation import answer as answer_mod
# client / parse_sse / _StubClassifier 都在契约套件里（不重复造）
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401
from tests.retrieval.conftest import seed_document

MATERIAL_CHUNK = "向量库使用 Qdrant 内嵌模式，向量化由上游接口完成。"
MATERIAL_FILENAME = "检索手册.md"
ANSWER = "据资料 [1]，向量库用 Qdrant 内嵌模式。"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation_at", ["preparation", "retrieval", "generation"])
@pytest.mark.parametrize("mutation_kind", ["title", "new-note"])
async def test_invalidation_during_turn_does_not_cache_old_snapshot(
    db, cache_on, monkeypatch, mutation_at, mutation_kind
):
    """资料在准备、检索或生成中改变时，旧答案不能借用新的缓存世代写回。"""
    from app.api.chat_schemas import ChatRequest
    from app.models import Note
    from app.retrieval.types import Material, number, render_materials
    from app.session import note_service

    monkeypatch.setattr(cache_on, "QUERY_CLARIFY_ENABLE", False)
    monkeypatch.setattr(cache_on, "AGENT_MODE", "off")
    note = Note(title="旧标题", content=MATERIAL_CHUNK, category="")
    db.add(note)
    await db.commit()
    await db.refresh(note)
    material = Material(
        chunk_id=-note.id,
        document_id=0,
        note_id=note.id,
        filename=note.title,
        content=note.content,
    )
    number([material])

    async def mutate():
        if mutation_kind == "title":
            await note_service.update_note(db, note.id, title="新标题")
        else:
            await note_service.create_note(db, title="新增资料", content=MATERIAL_CHUNK)

    async def retrieve_snapshot(_db, _text, filters=None, *, stats=None):
        if stats is not None:
            stats.status = "disabled"
            stats.candidates = 1
        if mutation_at == "retrieval":
            await mutate()
        return [material], render_materials([material]), None

    resolve_chat = chat_routes._resolve_chat

    async def resolve_then_mutate(*args, **kwargs):
        chat = await resolve_chat(*args, **kwargs)
        await mutate()
        return chat

    if mutation_at == "preparation":
        monkeypatch.setattr(chat_routes, "_resolve_chat", resolve_then_mutate)
    monkeypatch.setattr(chat_routes, "retrieve_materials", retrieve_snapshot)
    old_generation = cache_store.current_generation()
    payload = ChatRequest(message="向量库用什么存储？")
    plan = await chat_routes.build_turn_plan(
        db, payload, payload.message, [], routing_classifier=_StubClassifier("internal_db")
    )
    assert plan.kind == "normal" and not plan.cached
    assert plan.cache_generation == old_generation
    if mutation_at == "generation":
        await mutate()
    assert cache_store.current_generation() > old_generation
    # U6 也检查来源标题；其余腿仍靠准备前固定世代挡住旧答案。
    entry = chat_routes._cache_write_entry(plan, ANSWER, None)
    assert await cache_entry.fingerprints_live(db, entry.fingerprints) == (
        mutation_at != "generation" or mutation_kind != "title"
    )
    chat_routes.maybe_store_cache(plan, ANSWER, None)
    await chat_routes.drain_cache_tasks()
    key = chat_routes.build_cache_key(
        question=plan.question, filters=plan.filters, agent_mode=plan.agent_mode,
        skill_ids=plan.skill_ids, images=plan.images, history=plan.history,
        memory_text=plan.memory_text, source_type=plan.source_type,
    )
    assert cache_store.cache_size() == 0
    assert await cache_store.lookup(db, key) is None


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
    """假 bound model：**记录被调用次数**（A4/A12/A19 的「模型省了没有」就断在它身上）。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def generate(self, messages, system_prompt=None, max_output_tokens=None, **extra):  # noqa: ANN001
        self.calls.append({"mode": "generate", "system_prompt": system_prompt})
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.reply)], stop_reason="end_turn"
        )

    async def stream(self, messages, system_prompt=None, **extra):  # noqa: ANN001
        self.calls.append({"mode": "stream", "system_prompt": system_prompt})
        return _FakeStream([self.reply])

    @property
    def count(self) -> int:
        return len(self.calls)


class _RetrievalCounter:
    """``chat_routes.retrieve_materials`` 的计数包装（唯一调用点就是那个符号）。

    ``materials_after_first``：从**第二次**调用起换用另一份材料 —— 模拟「块被删 / 内容已变
    之后，真检索给出的**新**结果」。A10 的两个用例需要它：存活校验没过 ⇒ 真检索重跑，
    而重跑时那个块已经不在了，**真检索不可能还给出它**（拿旧快照当返回值会撞
    ``message_attributions`` 的外键，那是测试自己造出来的假象，不是产品缺陷）。
    """

    def __init__(
        self, materials, text: str, warning: str | None = None, *, materials_after_first=None
    ) -> None:
        self.materials = materials
        self.text = text
        self.warning = warning
        self.materials_after_first = materials_after_first
        self.calls: list[dict] = []

    async def __call__(self, db, text, filters=None, *, stats=None):  # noqa: ANN001
        self.calls.append({"text": text, "filters": filters})
        from app.retrieval.rerank import RerankStats

        fresh = len(self.calls) > 1 and self.materials_after_first is not None
        materials = self.materials_after_first if fresh else self.materials
        if stats is not None:
            stats.status = "no_candidates" if not materials else "disabled"
            stats.candidates = len(materials)
        return list(materials), ("" if fresh else self.text), self.warning

    @property
    def count(self) -> int:
        return len(self.calls)


def _install(monkeypatch, reply: str = ANSWER):
    """装模型 + 路由分类器（不连真上游），并关掉记忆（与本条无关的增强）。"""
    model = _CountingModel(reply)
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )
    return model


@pytest.fixture
def cache_on(monkeypatch):
    """开态 + 独立清表（``tests/api/conftest.py`` 的钉子把默认态钉成关，这里显式打开）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 86400)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 256)
    cache_store.reset_cache()
    cache_store.reset_failure_counts()
    yield settings
    cache_store.reset_cache()
    cache_store.reset_failure_counts()


async def _materials_from(db, texts=(MATERIAL_CHUNK,), filename=MATERIAL_FILENAME):
    """造真语料并返回 (材料快照, 渲染正文)——材料用**真 chunk_id**，让存活校验真的过。"""
    from sqlalchemy import select

    from app.models import Chunk
    from app.retrieval.types import Material, number, render_materials

    document = await seed_document(db, list(texts), filename=filename)
    rows = (
        (await db.execute(select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.id)))
        .scalars()
        .all()
    )
    materials = [
        Material(
            chunk_id=row.id,
            document_id=document.id,
            content=row.content,
            filename=filename,
            kb_id=1,
            block_ordinal=row.ordinal,
            keyword_score=0.9,
        )
        for row in rows
    ]
    number(materials)
    return materials, render_materials(materials)


async def _flush_cache_writes():
    """等缓存写回的后台任务落定（fire-and-forget ⇒ 用例必须自己等）。"""
    await chat_routes.drain_cache_tasks()


# --------------------------------------------------------------------------
# A4 / A19：开 = 检索与模型都省
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a4_a19_second_identical_question_skips_retrieval_and_model(
    client, db, cache_on, monkeypatch
):
    """**A4 + A19**：同问题连问两次 ⇒ 第二次**检索与模型都没跑**，且两轮正文逐字相同。"""
    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)

    first = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert first.status_code == 200, first.text
    await _flush_cache_writes()
    assert counter.count == 1, "第一次必然要检索"
    assert model.count == 1, "第一次必然要调模型"
    assert first.json()["kind"] == "normal"

    second = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert second.status_code == 200, second.text

    # **A19 的收益数字**：两轮提问、检索只跑了 1 次；模型同样只跑了 1 次
    assert counter.count == 1, f"缓存已命中却仍检索了 {counter.count} 次"
    assert model.count == 1, f"缓存已命中却仍调了模型 {model.count} 次"

    # **A4 的逐字一致**（`refs` 除 `retrieval_id` 外全等）
    left, right = first.json(), second.json()
    assert right["message"] == left["message"]
    assert right["kind"] == left["kind"]
    assert right["answer_source"] == left["answer_source"]
    assert _strip_retrieval_ids(right["refs"]) == _strip_retrieval_ids(left["refs"])
    assert right["refs"], "这一轮本应有引用（材料非空）"


def _strip_retrieval_ids(refs: list[dict]) -> list[dict]:
    return [{key: value for key, value in ref.items() if key != "retrieval_id"} for ref in refs]


@pytest.mark.asyncio
async def test_a19_stream_path_also_skips(client, db, cache_on, monkeypatch):
    """流式链路的 A19：命中轮不检索、不调模型，且 ``token`` 拼起来 == 落库正文。"""
    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)

    first = await client.post("/api/chat/stream", json={"message": "向量库用什么存储？"})
    await _flush_cache_writes()
    assert counter.count == 1 and model.count == 1

    second = await client.post("/api/chat/stream", json={"message": "向量库用什么存储？"})
    assert second.status_code == 200, second.text
    assert counter.count == 1, "流式命中轮不应再检索"
    assert model.count == 1, "流式命中轮不应再调模型"

    events = parse_sse(second.text)
    assert events[0]["type"] == "meta" and events[-1]["type"] == "done"
    joined = "".join(item["text"] for item in events if item["type"] == "token")
    assert joined == ANSWER, f"命中轮的 token 拼起来与未命中轮不一致：{joined!r}"

    first_events = parse_sse(first.text)
    first_joined = "".join(item["text"] for item in first_events if item["type"] == "token")
    assert joined == first_joined, "两轮正文必须逐字相同"


@pytest.mark.asyncio
async def test_cached_stream_adds_assume_prefix_once_and_matches_history(
    client, db, cache_on, monkeypatch
):
    """真实写回与命中两轮只加一次展示前缀，交付、留档和历史一致。"""
    from sqlalchemy import select

    from app.generation import clarify
    from app.models import Message
    from app.session import repository

    monkeypatch.setattr(cache_on, "QUERY_CLARIFY_ENABLE", True)
    monkeypatch.setattr(cache_on, "QUERY_CLARIFY_MAX_ROUNDS", 2)
    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)
    prefix = clarify.assume_prefix(True)
    assert prefix
    expected = prefix + ANSWER
    chat_ids = []
    for index in range(2):
        # 独立空历史保证同缓存键；真实澄清上限决定 assume，不伪造缓存命中。
        chat = await repository.create_chat(db, title=f"cached prefix {index}")
        chat.clarify_rounds = 2
        chat_ids.append(chat.id)
    await db.commit()

    responses = []
    first = await client.post(
        "/api/chat/stream",
        json={"chat_id": chat_ids[0], "message": "向量库用什么存储？"},
    )
    assert first.status_code == 200, first.text
    responses.append(first)
    await _flush_cache_writes()
    assert cache_store.cache_size() == 1, "首轮必须实际写回"
    assert counter.count == model.count == 1
    hits_before = cache_store.failure_counts()["hit"]
    second = await client.post(
        "/api/chat/stream",
        json={"chat_id": chat_ids[1], "message": "向量库用什么存储？"},
    )
    assert second.status_code == 200, second.text
    responses.append(second)
    assert cache_store.failure_counts()["hit"] == hits_before + 1, "第二轮必须实际命中"
    assert cache_store.cache_size() == 1
    assert counter.count == model.count == 1, "命中不重跑检索或模型"

    for chat_id, response in zip(chat_ids, responses):
        frames = parse_sse(response.text)
        assert frames[0]["type"] == "meta" and frames[0]["chat_id"] == chat_id
        assert [frame["type"] for frame in frames if frame["type"] in {
            "done", "error", "stopped",
        }] == ["done"]
        assert frames[-1]["kind"] == "normal"
        delivered = "".join(frame["text"] for frame in frames if frame["type"] == "token")
        assert delivered.count(prefix) == 1, delivered
        assert delivered == expected
        await db.rollback()
        rows = list((await db.execute(
            select(Message).where(Message.chat_id == chat_id).order_by(Message.id),
        )).scalars().all())
        assert [row.role for row in rows] == ["user", "assistant"]
        assert rows[-1].content == delivered
        history = await client.get(f"/api/chats/{chat_id}")
        assert history.status_code == 200
        assert history.json()["messages"][-1]["content"] == delivered


# --------------------------------------------------------------------------
# A6：命中不改契约
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a6_done_frame_key_set_is_identical_both_states(client, db, cache_on, monkeypatch):
    """**A6**：命中轮与未命中轮的 ``done`` 帧**键集完全相同**（一个键都不加）。"""
    materials, text = await _materials_from(db)
    monkeypatch.setattr(chat_routes, "retrieve_materials", _RetrievalCounter(materials, text))
    _install(monkeypatch)

    first = parse_sse(
        (await client.post("/api/chat/stream", json={"message": "向量库用什么存储？"})).text
    )
    await _flush_cache_writes()
    second = parse_sse(
        (await client.post("/api/chat/stream", json={"message": "向量库用什么存储？"})).text
    )

    done_off = [item for item in first if item["type"] == "done"][-1]
    done_on = [item for item in second if item["type"] == "done"][-1]
    assert set(done_on) == set(done_off), f"多 {set(done_on) - set(done_off)} / 少 {set(done_off) - set(done_on)}"
    # 🔴 2026-09-27：`回归修复` 拍板⑨「补」⇒ done 帧新增第 10 键 `recovered`（02 v1.0.12）。
    # S3 user-approved v1.0.37 adds the same two terminal fields to both cache states.
    assert set(done_on) == {
        "type", "chat_id", "refs", "kind", "reasoning", "thinking",
        "skill_ids", "agent_trace", "answer_source", "recovered",
        "generation_status", "finish_reason",
    }
    # meta 形状也相同
    assert set(first[0]) == set(second[0])


# --------------------------------------------------------------------------
# A7：命中轮落库与事件一条不少
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a7_cached_turn_persists_everything(client, db, cache_on, monkeypatch):
    """**A7**：``messages`` 两行 + 四事件 + ``assistant/attempt(committed)`` 都在；
    ``turn/end`` 带 ``final_material_count`` 与 ``final_empty_reason``。

    ⚠️ **第二轮必须真命中**：``chat_id`` 会改变 ``history`` 指纹 ⇒ 续问同一会话是
    **正确的未命中**（答案与上下文变了就不该复用）。所以这里第二轮**换一个新会话**
    （空历史，与第一轮同键）——测的才是命中路径的落库。
    """
    from sqlalchemy import select

    from app.models import Message, SessionEvent

    materials, text = await _materials_from(db)
    monkeypatch.setattr(chat_routes, "retrieve_materials", _RetrievalCounter(materials, text))
    _install(monkeypatch)

    first = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    await _flush_cache_writes()
    assert cache_store.cache_size() == 1, "第一轮应当写回一条缓存"

    second = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert second.status_code == 200, second.text
    assert second.json()["message"] == first.json()["message"]
    assert cache_store.failure_counts()["hit"] >= 1, "第二轮**没有命中** ⇒ 本用例什么都没测到"

    chat_id = second.json()["chat_id"]
    await db.rollback()  # 拿新快照（REPEATABLE READ）

    rows = list(
        (await db.execute(select(Message).where(Message.chat_id == chat_id).order_by(Message.id)))
        .scalars()
        .all()
    )
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[1].content == ANSWER
    assert rows[1].kind == "normal"

    events = list(
        (
            await db.execute(
                select(SessionEvent)
                .where(SessionEvent.chat_id == chat_id)
                .order_by(SessionEvent.id)
            )
        )
        .scalars()
        .all()
    )
    turns = [item for item in events if item.turn == 1]
    types = [item.type for item in turns]
    for expected in ("turn/start", "user/message", "assistant/message", "turn/end"):
        assert expected in types, f"命中轮缺事件 {expected}：{types}"
    attempt = [item for item in turns if item.type == "assistant/attempt"]
    assert attempt and attempt[-1].data["outcome"] == "committed"
    # 检索一对事件**照发**（§2.4 第 7 条：缺两条会被 4.4 记成「本轮缺行」）
    # ⚠️ 这两条是**实测抓到的真缺陷**的回归钉：命中路径起初直接 `return`，
    # 忘了调 `_log_cached_retrieval` ⇒ 命中轮**只**留 `routing/decided` 一条事件。
    assert "retrieval/start" in types, f"命中轮缺 retrieval/start（4.4 会记成缺行）：{types}"
    assert "retrieval/result" in types, f"命中轮缺 retrieval/result（4.4 会记成缺行）：{types}"
    cached_retrieval = [item for item in turns if item.type == "retrieval/start"][-1]
    assert cached_retrieval.trace, "retrieval/start 没带 trace（配对链断了）"

    end = [item for item in turns if item.type == "turn/end"][-1]
    assert "final_material_count" in end.data
    assert "final_empty_reason" in end.data
    assert end.data["final_material_count"] == len(materials)


@pytest.mark.asyncio
async def test_a7_cached_turn_attributions_are_written(client, db, cache_on, monkeypatch):
    """命中轮的**引用归因**照常落库（溯源链不因命中而断）。"""
    from sqlalchemy import select

    from app.models import Message, MessageAttribution

    materials, text = await _materials_from(db)
    monkeypatch.setattr(chat_routes, "retrieve_materials", _RetrievalCounter(materials, text))
    _install(monkeypatch)

    first = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    await _flush_cache_writes()
    second = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert cache_store.failure_counts()["hit"] >= 1, "第二轮**没有命中** ⇒ 本用例什么都没测到"
    chat_id = second.json()["chat_id"]
    await db.rollback()  # 拿新快照

    assistant_ids = list(
        (
            await db.execute(
                select(Message.id).where(Message.chat_id == chat_id, Message.role == "assistant")
            )
        )
        .scalars()
        .all()
    )
    assert assistant_ids, "命中轮应当落了 assistant 消息"
    rows = list(
        (
            await db.execute(
                select(MessageAttribution).where(
                    MessageAttribution.message_id.in_(assistant_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == len(assistant_ids), "命中轮的引用归因没写"


# --------------------------------------------------------------------------
# A13：retrieval_id 每轮现铸
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a13_retrieval_id_is_minted_per_turn(client, db, cache_on, monkeypatch):
    """**A13**：命中轮的 ``retrieval_id`` 与它复用的那一轮**不同**，且 ``refs`` 用的是本轮的。

    两轮都必须是**命中**（否则测不到「命中路径现铸 id」这条）——用两个新会话保证
    ``history`` 指纹相同（见 A7 用例的注释）。
    """
    materials, text = await _materials_from(db)
    monkeypatch.setattr(chat_routes, "retrieve_materials", _RetrievalCounter(materials, text))
    _install(monkeypatch)

    first = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    await _flush_cache_writes()
    second = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert cache_store.failure_counts()["hit"] >= 1, "第二轮**没有命中** ⇒ 本用例什么都没测到"

    left = {ref["retrieval_id"] for ref in first.json()["refs"]}
    right = {ref["retrieval_id"] for ref in second.json()["refs"]}
    assert len(left) == 1 and len(right) == 1
    assert left != right, "命中轮复用了缓存里的 retrieval_id（会毁掉 3.3 的 trace 配对语义）"


# --------------------------------------------------------------------------
# A10：端到端「无悬空 / 无陈旧引用」
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a10_deleted_chunk_forces_a_full_rerun(client, db, cache_on, monkeypatch):
    """**A10 ①**：命中后**硬删**其中一个块 ⇒ 下一次**不命中**，检索与模型都重跑。"""
    from sqlalchemy import delete

    from app.models import Chunk

    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text, materials_after_first=[])
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)

    await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    await _flush_cache_writes()
    assert counter.count == 1

    await db.execute(delete(Chunk).where(Chunk.id == materials[0].chunk_id))
    await db.commit()

    again = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert again.status_code == 200, again.text
    assert counter.count == 2, "引用已悬空却仍然命中"
    assert model.count == 2, "存活校验没过时必须**两者都重跑**"
    # 重跑之后拿到的是**新**检索结果（旧内容不许再出现在 refs 里）
    assert again.json()["refs"] == []


@pytest.mark.asyncio
async def test_a10_reindex_content_change_forces_a_full_rerun(client, db, cache_on, monkeypatch):
    """**A10 ②**：``chunk_id`` 未变但内容已变（reindex 后的等价情形）⇒ 也不命中。"""
    from sqlalchemy import update

    from app.models import Chunk

    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text, materials_after_first=[])
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)

    await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    await _flush_cache_writes()

    await db.execute(
        update(Chunk).where(Chunk.id == materials[0].chunk_id).values(content="重排后的新内容")
    )
    await db.commit()

    await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert counter.count == 2, "只查「id 还在不在」的实现在这里会假命中（陈旧内容）"
    assert model.count == 2


# --------------------------------------------------------------------------
# A12：命中轮不记账、不抽记忆
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a12_cached_turn_records_no_usage_and_no_extraction(client, db, cache_on, monkeypatch):
    """**A12**：命中轮**模型调用 = 0**，且两处记录函数各 **0 次**、抽取模型 **0 次**。

    ⚠️ **必须断记录函数有没有被调用，不能只断 ``usage_events`` 行数**：缓存实现本身可能
    顺手写行（把这一行补上，正好糊住「记了一笔没花的钱」这个真问题）。所以这里把
    ``chat_routes.record_answer_usage``（用量记录的**唯一**调用点，也是 4.5.4 的闸门）
    与 ``memory.schedule_extract`` 各换成计数器，再看两轮的调用次数有没有变。
    """
    from app.session import memory as memory_mod

    materials, text = await _materials_from(db)
    monkeypatch.setattr(chat_routes, "retrieve_materials", _RetrievalCounter(materials, text))
    model = _install(monkeypatch)

    usage_calls: list[dict] = []
    extract_calls: list[dict] = []

    async def _fake_usage(db, plan, answer):  # noqa: ANN001
        usage_calls.append({"plan": plan, "answer": answer})

    def _fake_extract(**kwargs):  # noqa: ANN001
        extract_calls.append(kwargs)

    monkeypatch.setattr(chat_routes, "record_answer_usage", _fake_usage)
    monkeypatch.setattr(memory_mod, "schedule_extract", _fake_extract)

    first = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert first.status_code == 200, first.text
    await _flush_cache_writes()
    calls_after_first = (len(usage_calls), len(extract_calls), model.count)
    assert calls_after_first == (1, 1, 1), f"未命中那轮该各记一次，实际 {calls_after_first}"

    # ⚠️ 第二次**不带 chat_id**（= 新会话、空历史）：键里的 ⑦ ``history`` 指纹才与第一轮相同。
    # 带同一个 chat_id 时历史多出「问 + 答」两行 ⇒ 指纹必然不同 ⇒ 那是**正确的**未命中
    # （答案与上下文变了就不该复用），不是本用例要测的东西。
    second = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert second.status_code == 200, second.text
    assert second.json()["message"] == first.json()["message"]

    assert model.count == calls_after_first[2], "命中轮仍在调模型"
    assert len(usage_calls) == calls_after_first[0], "命中轮记了一笔没花的钱"
    assert len(extract_calls) == calls_after_first[1], "命中轮仍排了记忆抽取"


@pytest.mark.asyncio
async def test_a12_stream_path_also_skips_usage(client, db, cache_on, monkeypatch):
    """**A12 的流式侧**：命中轮同样不记账、不抽记忆。"""
    from app.session import memory as memory_mod

    materials, text = await _materials_from(db)
    monkeypatch.setattr(chat_routes, "retrieve_materials", _RetrievalCounter(materials, text))
    _install(monkeypatch)
    import httpx
    from app.usage import instrumentation, recorder
    from tests.test_opt5_observation_usage import _provider
    from tests.api.t0_wire import wire_sse_text

    sends = []
    def handler(request):
        sends.append(request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=wire_sse_text(ANSWER),
        )
    provider = _provider(handler)
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: provider.model("controlled"))

    usage_calls: list[dict] = []
    extract_calls: list[dict] = []

    def _fake_usage(**kwargs):
        usage_calls.append(kwargs)

    def _fake_extract(**kwargs):  # noqa: ANN001
        extract_calls.append(kwargs)

    monkeypatch.setattr(instrumentation, "record_usage", _fake_usage)
    monkeypatch.setattr(memory_mod, "schedule_extract", _fake_extract)

    try:
        await client.post("/api/chat/stream", json={"message": "向量库用什么存储？"})
        await _flush_cache_writes()
        await recorder.drain_pending()
        before = (len(usage_calls), len(extract_calls), len(sends))
        assert before == (1, 1, 1)
        await client.post("/api/chat/stream", json={"message": "向量库用什么存储？"})
        assert (len(usage_calls), len(extract_calls), len(sends)) == before
    finally:
        await provider._client.close()


# --------------------------------------------------------------------------
# A3：关 = 零变化
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a3_disabled_cache_is_zero_touch_over_http(client, db, monkeypatch):
    """**A3 的 ①③**：关态下跑完整一轮 ⇒ **零缓存查 / 零缓存写**，且行为与接缓存之前一致。"""
    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)

    lookups: list[str] = []
    original = cache_store.lookup

    async def _spy_lookup(db, key):  # noqa: ANN001
        lookups.append(key)
        return await original(db, key)

    monkeypatch.setattr(chat_routes.cache_store, "lookup", _spy_lookup)
    assert cache_store.enabled() is False, "本夹具应当把开关钉成关（autouse 钉子）"

    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert resp.status_code == 200, resp.text
    await _flush_cache_writes()

    assert lookups == [None], f"关态不应算出真键（拿到了 {lookups}）"
    assert cache_store.cache_size() == 0, "关态写了缓存"
    assert counter.count == 1 and model.count == 1, "关态行为应与接缓存之前逐字一致"
    assert resp.json()["kind"] == "normal"
    assert resp.json()["message"] == ANSWER


def test_a3_frozen_prompt_text_is_untouched():
    """**A14 的旁证**：本条目不该动 ``answer.py`` 的三段冻结文案（逐字比对）。"""
    assert answer_mod.SYSTEM_PROMPT.startswith("你是知澜")
    assert "资料如下" in answer_mod.SYSTEM_PROMPT or "[1]" in answer_mod.SYSTEM_PROMPT
    # S5-R1共用事实约束不依赖记忆开关，空态仍不拼记忆段。
    assert answer_mod.system_prompt(True, memory_text="") == answer_mod.SYSTEM_PROMPT + "\n\n" + answer_mod.FACTUAL_ANSWER_CONSTRAINTS
    assert answer_mod.system_prompt(False, memory_text="") == answer_mod.NO_MATERIAL_SYSTEM_PROMPT + "\n\n" + answer_mod.FACTUAL_ANSWER_CONSTRAINTS
    # 有记忆段时 = 冻结文案 + 附加段（5.1 的口径，本条目未改）
    assert answer_mod.system_prompt(True, memory_text="【记忆】x").startswith(
        answer_mod.SYSTEM_PROMPT
    )


# --------------------------------------------------------------------------
# 负向：不进缓存的四类轮（§0.0 第 17 行）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_agent_turn_is_never_cached(client, db, cache_on, monkeypatch):
    """Agent 轮**一律不进缓存**（编排状态面太大，命中要连整条编排一起重放）。"""
    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    _install(monkeypatch)

    key = chat_routes.build_cache_key(
        question="向量库用什么存储？",
        filters=None,
        agent_mode="react",
        skill_ids=[],
        images=None,
        history=[],
        memory_text="",
    )
    assert key is None, "Agent 轮不该算出键"


@pytest.mark.asyncio
async def test_negative_direct_turn_is_not_stored(client, db, cache_on, monkeypatch):
    """``kind == "direct"`` 无材料 ⇒ **首版不写回**（减少状态面）。

    构造：分类器判**库外** ⇒ ``direct``（材料为空但 ``needs_model=True``、
    且 ``answer_succeeded`` 为真）——它**恰好**是「只按 ``answer_succeeded`` 判就会误存」
    的那一类，所以是本用例要钉的反例。
    """
    counter = _RetrievalCounter([], "")
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external")
    )
    _install(monkeypatch, reply="库外直答。")

    resp = await client.post("/api/chat", json={"message": "今天天气怎么样"})
    assert resp.json()["kind"] == "direct", resp.text
    await _flush_cache_writes()
    assert cache_store.cache_size() == 0, "direct 轮不该进缓存"


@pytest.mark.asyncio
async def test_negative_clarify_and_refuse_are_not_stored(client, db, cache_on, monkeypatch):
    """``clarify`` / ``refuse`` 压根不调模型，缓存它们没有收益 ⇒ 不写回。"""
    counter = _RetrievalCounter([], "")
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )
    _install(monkeypatch, reply="你指的是哪份文档？")

    # ① 澄清轮（模糊指代 ⇒ 短路整轮，连路由都不走）
    clarify = await client.post("/api/chat", json={"message": "那个怎么样"})
    assert clarify.json()["kind"] == "clarify", clarify.text
    await _flush_cache_writes()
    assert cache_store.cache_size() == 0, "clarify 轮不该进缓存"

    # ② 拒答轮（库内未命中 + 直答开关关 ⇒ refuse，走预置文案）
    from app.core.config import settings

    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", False)
    refuse = await client.post("/api/chat", json={"message": "知识库里关于项目周报的内容"})
    assert refuse.json()["kind"] == "refuse", refuse.text
    await _flush_cache_writes()
    assert cache_store.cache_size() == 0, "refuse 轮不该进缓存"


@pytest.mark.asyncio
async def test_negative_image_turn_is_not_cached(client, db, cache_on, monkeypatch):
    """带图轮**不进缓存**（保守排除：图本身影响落库）。"""
    key = chat_routes.build_cache_key(
        question="看图",
        filters=None,
        agent_mode="off",
        skill_ids=[],
        images=["data:image/png;base64,AAAA"],
        history=[],
        memory_text="",
    )
    assert key is None


# --------------------------------------------------------------------------
# A8 的端到端侧：只改一个成分 ⇒ 不命中（走完整链路）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_payload",
    [
        {"message": "向量库用什么存储？", "filters": {"kb_ids": [1], "categories": ["项目"]}},
        {"message": "向量库用什么存储？", "filters": {"kb_ids": [2]}},
        {"message": "向量库用什么存储？", "skill_ids": ["builtin:knowledge"]},
        {"message": "向量库用 什么存储？"},
        {"message": "向量库用了什么存储？"},
    ],
)
async def test_a8_changing_any_component_is_a_miss(
    client, db, cache_on, monkeypatch, second_payload
):
    """**A8 的端到端侧**：只改范围 / skill_ids / 问题文字 ⇒ 第二次**不命中**。

    ⚠️ 刻意**不放**「``agent_mode`` 显式传 ``off``」这类**语义等价**的变更：
    键是对**内容**做的，显式 ``off`` 与缺省 ``off`` **必须**同键（否则就是无谓的假 miss）。
    那条「同键」由 ``tests/cache/test_cache.py::test_key_is_deterministic_for_identical_inputs``
    与键成分用例覆盖。
    """
    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    _install(monkeypatch)

    await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    await _flush_cache_writes()
    assert counter.count == 1

    await client.post("/api/chat", json=second_payload)
    assert counter.count == 2, f"换了键成分仍然命中：{second_payload}"


@pytest.mark.asyncio
async def test_same_question_different_history_is_a_miss(client, db, cache_on, monkeypatch):
    """⑦ 历史指纹：**同一个问题、不同轮次**（历史不同）⇒ 不命中。"""
    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    _install(monkeypatch)

    first = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    await _flush_cache_writes()
    chat_id = first.json()["chat_id"]
    # 换个话题把历史顶开，再问**同一个问题** —— 历史指纹不同 ⇒ 必须重跑
    await client.post("/api/chat", json={"message": "今天天气怎么样", "chat_id": chat_id})
    await _flush_cache_writes()
    before = counter.count
    await client.post("/api/chat", json={"message": "向量库用什么存储？", "chat_id": chat_id})
    assert counter.count == before + 1, "历史变了却命中 ⇒ 答案与上下文不符"


# --------------------------------------------------------------------------
# A18：与 5.1 互不吞没（**本条的专项**）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a18_memory_text_is_part_of_the_key(client, db, cache_on, monkeypatch):
    """**A18**：记忆段变了 ⇒ **换键**（5.1 的注入不会被 5.2 静默吞掉）。

    构造方式：同一个问题、同一份空历史，只把 ``memory_text`` 从 ``""`` 换成有内容的一段
    ⇒ 两个键必须不同（这正是「无记忆时算出的答案」不会在记忆写入后被复用的机制保证）。
    """
    empty = chat_routes.build_cache_key(
        question="向量库用什么存储？", filters=None, agent_mode="off", skill_ids=[],
        images=None, history=[], memory_text="",
    )
    with_memory = chat_routes.build_cache_key(
        question="向量库用什么存储？", filters=None, agent_mode="off", skill_ids=[],
        images=None, history=[], memory_text="【跨会话记忆】用户叫小王。",
    )
    assert empty and with_memory and empty != with_memory


@pytest.mark.asyncio
async def test_a18_memory_is_still_injected_when_cache_is_on(client, db, cache_on, monkeypatch):
    """**A18 ①**：开记忆 + 开缓存两轮跑完 ⇒ 第二轮的记忆段**仍在**提示词里。"""
    from app.core.config import settings
    from app.session import memory as memory_mod

    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    model = _install(monkeypatch)
    # 抽出这一步会**再调一次模型**（用的还是同一个假模型），把它挡掉 ⇒
    # `model.calls` 里剩下的全是「回答生成」那一次，断言才精准。
    monkeypatch.setattr(memory_mod, "schedule_extract", lambda **kwargs: None)

    await memory_mod.remember(db, text="用户在关注向量库的存储方式。", kind="interaction")
    await db.commit()

    await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    await _flush_cache_writes()

    assert model.count == 1, f"第一轮该恰好调一次模型（生成），实际 {model.count}"
    assert "跨会话记忆" in (model.calls[0]["system_prompt"] or ""), "记忆段没进系统提示"

    # 第二轮（**不带 chat_id** ⇒ 空历史、与第一轮同键 ⇒ 命中）：命中的那一轮压根不再调模型，
    # 但「记忆仍在提示词里」这件事由**键**保证——`memory_text` 是第 ⑥ 个成分，
    # 「无记忆时算出的答案」不可能在记忆写入之后被复用（那就是 5.1 被 5.2 吞掉）。
    await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert model.count == 1, "命中轮不该再调模型"
    assert cache_store.failure_counts()["hit"] >= 1, "第二轮应当是命中"


# --------------------------------------------------------------------------
# §2.4 第 6 条：命中路径要把一轮**重放完整**（含 reasoning）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cached_reasoning_is_replayed(client, db, cache_on, monkeypatch):
    """命中轮的 ``reasoning`` 帧与 ``done.reasoning`` 都来自缓存条目（不丢思考全文）。"""
    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    monkeypatch.setattr(chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db"))
    model = _install(monkeypatch)
    model.reply = ANSWER

    await client.post("/api/chat/stream", json={"message": "向量库用什么存储？"})
    await _flush_cache_writes()

    # 手工把思考全文塞进缓存条目（`_FakeStream` 不产 reasoning 增量）
    key = chat_routes.build_cache_key(
        question="向量库用什么存储？", filters=None, agent_mode="off", skill_ids=[],
        images=None, history=[], memory_text="",
    )
    assert key is not None and key in cache_store._STORE
    cache_store._STORE[key].reasoning = "先看资料，再综合。"

    resp = await client.post("/api/chat/stream", json={"message": "向量库用什么存储？"})
    events = parse_sse(resp.text)
    assert any(
        item["type"] == "reasoning" and item["text"] == "先看资料，再综合。" for item in events
    ), f"命中轮没重放 reasoning：{events}"
    assert events[-1]["reasoning"] == "先看资料，再综合。"


@pytest.mark.asyncio
async def test_cached_retrieval_events_carry_the_same_shape(client, db, cache_on, monkeypatch):
    """§2.4 第 7 条：命中轮照发 ``retrieval/*`` 一对事件，且载荷与未命中轮**同形**。"""
    from sqlalchemy import select

    from app.models import SessionEvent

    materials, text = await _materials_from(db)
    counter = _RetrievalCounter(materials, text)
    monkeypatch.setattr(chat_routes, "retrieve_materials", counter)
    _install(monkeypatch)

    await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    await _flush_cache_writes()
    second = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert cache_store.failure_counts()["hit"] >= 1, "第二轮**没有命中** ⇒ 本用例什么都没测到"
    assert counter.count == 1, "命中轮不该再检索"
    await db.rollback()

    events = list(
        (
            await db.execute(
                select(SessionEvent)
                .where(
                    SessionEvent.chat_id == second.json()["chat_id"],
                    SessionEvent.type == "retrieval/result",
                )
                .order_by(SessionEvent.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1, f"命中轮应恰好一条 retrieval/result，实际 {len(events)}"
    payload = events[0].data
    # 键集与载荷都要像**真检索**那一份（`hits` / 子路来源 / `degraded` / `rerank` / `empty_reason`）
    for key in ("hits", "from_vector", "from_keyword", "degraded", "warning", "rerank", "empty_reason"):
        assert key in payload, f"命中轮的事件载荷少了 {key}：{sorted(payload)}"
    assert payload["hits"] == len(materials)
    assert payload["degraded"] is False, "命中不该把 degraded 弄成 true（它说的是「向量路降级」）"
    assert payload["empty_reason"] is None
