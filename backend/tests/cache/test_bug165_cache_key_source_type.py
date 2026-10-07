"""`回归修复` · 语义缓存键缺 `source_type` 成分（同题在「库内 / 库外」两态共用一条缓存）。

**靶点（实读行号，2026-09-29）**

- `backend/app/api/chat_cache.py:36-45 build_cache_key` 七参（question/filters/agent_mode/
  skill_ids/images/history/memory_text）⇒ `:75-85` 组装 `CacheKeyInputs`，**无 source_type**；
- `backend/app/cache/entry.py:344-365 CacheKeyInputs` 九成分 / `:368-392 build_key` 九键；
- `backend/app/api/chat_cache.py:124-131 map_round("internal_db", …)` 与 `:141-148`
  `TurnPlan(source_type="internal_db", …)` **写死**（命中路径不读本轮 `decision`）；
- `backend/app/api/chat_routes.py:531` 已经拿到 `decision`，`:555-563` 传参 / `:564` `lookup` /
  `:565-585` 命中分支**全段不读 `decision`**。

**症状**：第一轮判 `internal_db` 并存下答案；第二轮**同一道题**被路由判 `external`（库外直答），
缓存键却与第一轮逐字相同 ⇒ 命中旧条目、拿库内口吻的答案 + 引用行，而同轮
`routing/decided` 明明是 `external`（I9 的留痕与用户实际拿到的答案互相矛盾）。

**判据（本文件 · 行为级 / 接口级，⛔ 不匹配产品代码内部写法）**

1. 主判据（**现码 RED**）：`internal_db` 先命中一轮 ⇒ 切 `external` 同题同范围 ⇒ 必须重新生成，
   `kind == "direct"` / `refs == []` / `answer_source == "general"`，且同会话的决策日志真的是
   `库内 → 库外`（证据在 `routing/decided`，见 `app/routing/decisions.py:59 load_decisions`）；
2. 反向保护：同一 `source_type` 下同题**仍命中**（缓存效果不得被这次改动打死）；
3. 反向保护（口径同 `tests/api/test_semantic_cache_http.py:621-639`）：库外轮**不写回**；
4. 反向保护（口径同 `tests/api/test_semantic_cache_http.py:243-267`）：流式 `done` 帧键集
   在两态逐字相同（键成分变了，**契约帧形状不许变**）；
5. 键层（**现码 RED**）：`CacheKeyInputs` 必须带 `source_type` 成分，且只差该成分 ⇒ 键不同；
6. 口径登记：`CACHE_SCHEMA_VERSION` **不为「加键」而升**（先例
   `tests/cache/test_bug139_config_knobs_key.py:59`：键变 ≠ 结构变；键是 sha256、没有版本位
   ⇒ 加成分本来就让存量条目一次性失效）。

**跑测（独占锁，⛔ 不并发 pytest）**：

    cd backend && ./.venv/Scripts/python.exe -m pytest tests/cache/test_bug165_cache_key_source_type.py -q

夹具复用：HTTP 客户端 / 假分类器 / SSE 解析复用 `tests/api/test_chat_contract.py`；开态夹具口径照
`tests/cache/test_cache.py:42-50`；`tests/cache/` 目录下**没有 conftest.py** ⇒ `tests/api` 的 autouse
钉子必须在本文件显式请求（见下 `_api_env_pins`，⛔ 有意不含 `pin_semantic_cache_off`——本件测的就是开态）。
"""

from __future__ import annotations

import dataclasses

import pytest

from app.api import chat_routes
from app.cache import entry as entry_mod
from app.cache import store as cache_store
from app.routing import decisions as decisions_mod
from tests.api.conftest import (  # noqa: F401 —— 显式复用（本目录无 conftest.py，autouse 不会自动生效）
    isolate_retrieval_vectors,
    pin_agent_mode_off,
    pin_aux_model_delegates,
    pin_graphrag_off,
    pin_mcp_off,
    pin_memory_off,
    pin_rerank_off,
    pin_settings_put_keys,
    pin_tool_artifact_dir,
    pin_web_off,
    restore_settings_singleton,
)
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401

QUESTION = "向量库用什么存储？"
NOTE_TITLE = "向量库选型"
NOTE_CONTENT = "向量库选型：知澜用 Qdrant 内嵌模式做向量存储，向量化交给上游接口。"

ANSWER_KB = "据资料 [1]，向量库用 Qdrant 内嵌模式。"
ANSWER_DIRECT = "库外直答：这个问题不查资料，直接回答。"

NOTE_FILTERS = {"source_types": ["note"]}

# 权威 done 帧键集（照 tests/api/test_semantic_cache_http.py:243-267，两态必须逐字相同）
DONE_FRAME_KEYS = {
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


class _SwitchableClassifier:
    """**可在轮次之间换标签**的假分类器：同一道题先判库内、再判库外。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[str] = []

    async def classify(self, text: str) -> str:
        self.calls.append(text)
        return self.label


class _FakeStream:
    """最小流：整段回答当**一个** `text_delta` 发（与产品事件形状一致）。"""

    def __init__(self, parts: list[str]) -> None:
        self._parts = parts

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for part in self._parts:
            yield _ns(type="text_delta", delta=part)


def _ns(**kwargs):
    from types import SimpleNamespace

    return SimpleNamespace(**kwargs)


class _CountingModel:
    """假 bound model：记录被调用次数（「命中轮不再调模型」就断在它身上）。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def generate(self, messages, system_prompt=None, max_output_tokens=None, **extra):  # noqa: ANN001
        self.calls.append({"mode": "generate", "system_prompt": system_prompt})
        return _ns(content=[_ns(type="text", text=self.reply)], stop_reason="end_turn")

    async def stream(self, messages, system_prompt=None, **extra):  # noqa: ANN001
        self.calls.append({"mode": "stream", "system_prompt": system_prompt})
        return _FakeStream([self.reply])

    @property
    def count(self) -> int:
        return len(self.calls)


@pytest.fixture(autouse=True)
def _api_env_pins(  # noqa: PLR0913 —— 逐个显式请求：本目录没有 conftest.py
    isolate_retrieval_vectors,
    pin_rerank_off,
    pin_web_off,
    pin_mcp_off,
    pin_memory_off,
    pin_graphrag_off,
    pin_aux_model_delegates,
    pin_agent_mode_off,
    pin_tool_artifact_dir,
    pin_settings_put_keys,
    restore_settings_singleton,
):
    """把 `tests/api` 的既有钉子搬进本目录（⛔ 有意不含 `pin_semantic_cache_off`）。"""
    return None


@pytest.fixture(autouse=True)
def _clean_cache():
    """进程内缓存跨用例会泄漏 ⇒ 进 / 出各清一次。"""
    cache_store.reset_cache()
    cache_store.reset_failure_counts()
    yield
    cache_store.reset_cache()
    cache_store.reset_failure_counts()


@pytest.fixture
def cache_on(monkeypatch):
    """开态：三个键都设成有效值（本机 `.env` 无这三个键 ⇒ 默认关，必须显式打开）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 86400)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 256)
    cache_store.reset_cache()
    cache_store.reset_failure_counts()
    yield settings
    cache_store.reset_cache()
    cache_store.reset_failure_counts()


def _install(monkeypatch, classifier=None) -> _CountingModel:  # noqa: ANN001
    """装模型 + 分类器，并**强制走分类器**。

    `ROUTING_MODE="regex"` 时正则命中即短路为 `internal_db`（`backend/app/routing/router.py:108-116`，
    不再问分类器）⇒ 本件「同题换 source_type」必须由分类器决定，故把模式钉成 `llm`
    （`router.py:7` 直接问 LLM，`:118-124` 仍然用 `resolve_routing_classifier()` 的返回）。
    """
    from app.core.config import settings

    clf = classifier if classifier is not None else _SwitchableClassifier("internal_db")
    model = _CountingModel(ANSWER_KB)
    monkeypatch.setattr(settings, "ROUTING_MODE", "llm")
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(chat_routes, "resolve_routing_classifier", lambda: clf)
    return model


async def _flush_cache_writes() -> None:
    """等缓存写回的后台任务落定（fire-and-forget ⇒ 用例必须自己等）。"""
    await chat_routes.drain_cache_tasks()


async def _create_note(client) -> dict:  # noqa: ANN001
    resp = await client.post(
        "/api/notes", json={"title": NOTE_TITLE, "content": NOTE_CONTENT, "category": ""}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _ask(client, *, question=QUESTION, chat_id=None) -> dict:  # noqa: ANN001
    body: dict = {"message": question, "filters": dict(NOTE_FILTERS)}
    if chat_id is not None:
        body["chat_id"] = chat_id
    resp = await client.post("/api/chat", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _stream_once(client, *, question=QUESTION) -> list[dict]:  # noqa: ANN001
    resp = await client.post(
        "/api/chat/stream", json={"message": question, "filters": dict(NOTE_FILTERS)}
    )
    assert resp.status_code == 200, resp.text
    return parse_sse(resp.text)


async def _prime(client, model, *, reply=ANSWER_KB) -> dict:  # noqa: ANN001
    """首轮：真检索 + 真生成，并把这一轮写进缓存（其余腿的前提条件）。"""
    model.reply = reply
    body = await _ask(client)
    assert body["kind"] == "normal", f"开态首轮必须是库内命中（笔记范围）：{body}"
    assert body["refs"], "笔记范围首轮必须有引用行（否则后面的断言是空靶）"
    assert body["message"] == reply, body
    await _flush_cache_writes()
    assert cache_store.cache_size() == 1, "首轮之后缓存必须恰好一条（否则后面的断言是空靶）"
    return body


# ---------------------------------------------------------------------------
# 腿① 主判据：同题换 source_type ⇒ 必须 miss（现码 RED）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_round_does_not_replay_the_internal_answer(client, db, cache_on, monkeypatch):  # noqa: ANN001
    """腿①（现码 RED）：库内轮写回之后，**同一道题**判为库外时不得再回库内答案。

    两轮都走**新会话**（body 都不带 `chat_id`）⇒ 键的 `history` 成分两轮都为空。
    ⚠️ 同会话追问**不是**本缺陷的判别构型：历史来自该会话的 messages 行
    （`backend/app/api/chat_routes.py:455-460 build_model_history(db, chat.id, ...)`），
    第二轮键**必然**因历史变长而不同 ⇒ 现码也会 miss ⇒ 那条构型对「键里缺 source_type」
    **零判别力**（第一版如此，实测两态同绿，已按本注释改写）。

    现码下缓存键里没有 `source_type` ⇒ 第二轮键与第一轮**逐字相同** ⇒ 命中旧条目：
    `kind` 仍是 `normal`、`refs` 非空、`answer_source` 仍是 `kb`、模型不会再被调一次。
    """
    classifier = _SwitchableClassifier("internal_db")
    model = _install(monkeypatch, classifier)
    await _create_note(client)
    first = await _prime(client, model)

    # 同一道题、同一范围、**同一个（空的）历史**，只换路由结论；答案串也换掉 ⇒ 回旧答案一眼可辨
    classifier.label = "external"
    model.reply = ANSWER_DIRECT
    second = await _ask(client)

    assert classifier.calls, "本腿要求问题**走分类器**（否则路由不由本腿控制，断言是空靶）"
    await db.commit()  # 结束本会话事务：HTTP 侧刚提交的 routing/decided 行必须对读快照可见
    first_round = [d.source_type for d in await decisions_mod.load_decisions(db, first["chat_id"])]
    second_round = [d.source_type for d in await decisions_mod.load_decisions(db, second["chat_id"])]
    assert second["chat_id"] != first["chat_id"], (
        "两轮必须落成两个会话：本腿靠「两轮历史都为空」把键成分对齐"
    )
    assert first_round == ["internal_db"], f"第一轮的路由决策必须是库内（I9 留痕）：{first_round}"
    assert second_round == ["external"], f"第二轮的路由决策必须是库外（I9 留痕）：{second_round}"

    assert second["kind"] == "direct", (
        f"库外轮仍回了库内答案（kind={second['kind']!r}）⇒ 缓存键没把 source_type 当成分"
    )
    assert second["refs"] == [], "库外（直答）轮不得带引用行"
    assert second["answer_source"] == "general", second
    assert second["message"] == ANSWER_DIRECT, "库外轮必须重新生成，不得回第一轮的库内答案"
    assert model.count == 2, "库外轮必须重新调模型（命中旧条目会少调一次）"
    assert cache_store.cache_size() == 1, (
        "键分流语义：库内那条条目应当原样留着（⛔ 不是「换 source_type 就清表」）"
    )


# ---------------------------------------------------------------------------
# 腿② 反向保护：同一 source_type ⇒ 仍命中
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_source_type_still_hits_cache(client, db, cache_on, monkeypatch):  # noqa: ANN001
    """腿② 两态同绿：不换 source_type 时缓存效果**不得**退化。"""
    model = _install(monkeypatch, _SwitchableClassifier("internal_db"))
    await _create_note(client)
    await _prime(client, model)

    model.reply = ANSWER_DIRECT  # 若真命中，用户拿到的是缓存里的旧答案而不是这一串
    hits_before = cache_store.failure_counts()["hit"]
    body = await _ask(client)

    assert body["message"] == ANSWER_KB, "同一 source_type 下必须仍然命中缓存"
    assert body["kind"] == "normal" and body["refs"], body
    assert model.count == 1, "命中轮不得再调模型"
    assert cache_store.failure_counts()["hit"] == hits_before + 1


# ---------------------------------------------------------------------------
# 腿③ 反向保护：库外轮不写回（口径同 test_semantic_cache_http.py:621-639）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_round_is_not_written_back(client, db, cache_on, monkeypatch):  # noqa: ANN001
    """腿③ 两态同绿：库外轮不写回；库内轮照常写回（库外那轮不留脏条目顶掉它）。"""
    classifier = _SwitchableClassifier("external")
    model = _install(monkeypatch, classifier)
    await _create_note(client)

    first = await _ask(client)
    assert first["kind"] == "direct" and first["refs"] == [], first
    await _flush_cache_writes()
    assert cache_store.cache_size() == 0, "库外轮不得写回缓存"

    classifier.label = "internal_db"
    model.reply = ANSWER_KB
    second = await _ask(client)
    assert second["kind"] == "normal" and second["refs"], second
    await _flush_cache_writes()
    assert cache_store.cache_size() == 1, "库内轮必须正常写回"


# ---------------------------------------------------------------------------
# 腿④ 反向保护：流式 done 帧键集两态一致（口径同 test_semantic_cache_http.py:243-267）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_frame_key_set_is_identical_both_states(client, db, cache_on, monkeypatch):  # noqa: ANN001
    """腿④ 两态同绿：键成分变了，**契约帧形状不许变**（未命中态 vs 命中态）。"""
    _install(monkeypatch, _SwitchableClassifier("internal_db"))
    await _create_note(client)

    first = await _stream_once(client)
    await _flush_cache_writes()
    assert cache_store.cache_size() == 1, "流式首轮之后缓存必须恰好一条（否则第二态是空靶）"
    second = await _stream_once(client)

    first_done = [f for f in first if f.get("type") == "done"]
    second_done = [f for f in second if f.get("type") == "done"]
    assert len(first_done) == 1 and len(second_done) == 1, (first, second)
    assert set(first_done[0]) == DONE_FRAME_KEYS, set(first_done[0])
    assert set(second_done[0]) == DONE_FRAME_KEYS, set(second_done[0])
    assert first_done[0]["kind"] == second_done[0]["kind"] == "normal"
    assert first_done[0]["answer_source"] == second_done[0]["answer_source"] == "kb"
    assert second_done[0]["refs"], "命中态也必须带引用行"
    assert set(first[0]) == set(second[0]), "meta 帧键集也必须在两态逐字相同"


# ---------------------------------------------------------------------------
# 腿⑤ 键层（现码 RED）：CacheKeyInputs 必须带 source_type，且它能改变键
# ---------------------------------------------------------------------------


def _inputs(**overrides) -> entry_mod.CacheKeyInputs:
    base = dict(
        question=QUESTION,
        filters={"kb_ids": [1]},
        agent_mode="off",
        skill_ids=["builtin:knowledge"],
        model_label="stub/fake-chat",
        memory_text="",
        history=[{"role": "user", "content": "你好"}],
        has_images=False,
        retrieval_config={"embed": {"service_id": "s1", "model": "m1"}},
    )
    base.update(overrides)
    return entry_mod.CacheKeyInputs(**base)


def test_cache_key_inputs_carry_source_type():
    """腿⑤（现码 RED）：键成分含 `source_type`；只差这一项 ⇒ 键必须不同。"""
    fields = {f.name for f in dataclasses.fields(entry_mod.CacheKeyInputs)}
    assert "source_type" in fields, (
        f"回归修复：缓存键成分必须含 source_type，现成分 = {sorted(fields)}"
    )

    internal = _inputs(source_type="internal_db")
    external = _inputs(source_type="external")
    assert internal != external

    key_internal = entry_mod.build_key(internal)
    key_external = entry_mod.build_key(external)
    assert key_internal != key_external, (
        "只差 source_type 的两组键成分算出了同一个键 ⇒ 缓存键没把 source_type 纳入"
    )
    assert len(key_internal) == 64 and all(c in "0123456789abcdef" for c in key_internal)


# ---------------------------------------------------------------------------
# 腿⑥ 口径登记：加键 ≠ 结构变 ⇒ 不升 CACHE_SCHEMA_VERSION
# ---------------------------------------------------------------------------


def test_cache_schema_carries_the_current_source_snapshot_version():
    """165 本身只改键；U6 完整来源快照改变条目结构，承接为版本 3。"""
    assert entry_mod.CACHE_SCHEMA_VERSION == 3
