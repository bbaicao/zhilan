"""`回归修复` · 缓存失效接线（写路径必须让语义缓存全失效）——行为级判据载体。

**判据面（不读产品实现的内部写法，只打 HTTP 接口 + 进程内缓存规模）**

- 腿① 反向保护：没有任何写事件时同题**仍命中**（两态同绿；防「顺手把缓存关了」）
- 腿② 改笔记**标题/分类**（正文逐字不动）后同题**不得**回旧答案　⇒ 未接线的现码 RED
- 腿③ 上传答复该问题的新材料后同题**不得**回上传前答案　⇒ 现码 RED
- 腿④ 软删 → 恢复：恢复让行重新进入检索面，旧答案**不得**再被命中　⇒ 现码 RED
- 腿⑤ 彻底删除**被引用的笔记**后不得回旧答案　⇒ 现码 RED（在「全失效」判据上）
- 腿⑥ 彻底删除**与缓存条目无关**的条目（软删会话）后不得回旧答案　⇒ 现码 RED
- 腿⑦ 清空回收站后不得回旧答案　⇒ 现码 RED
- 腿⑧ 反向保护：**读路径不得清缓存**（GET 列表 / 分类过滤之后仍命中，两态同绿）

**拍板口径**（Lead 依业主 2026-09-29 指令 `m00001` 会话内签发 · `#6 = A`）：失效面 =
笔记元数据 / 新增材料 / 恢复 / 彻底删除 **全失效**（正确性优先）⇒ 每条写事件腿除「不得回旧答案」
外，都额外汇总断言 `cache_store.cache_size() == 0`（「全失效」**不是**「逐条精确摘除」）。

**为什么腿⑤ 也需要接线**（不是重复腿②）：存活校验只比**正文**指纹 ⇒ 改元数据 / 新增材料 /
软删前后都不会被判失活；「彻底删除」虽会被存活校验挡住（被引用的条目没了），本载体仍按
`#6=A` 要求**全失效**（缓存里可能还压着与本次删除无关的键）。

夹具复用：开态夹具照 `tests/api/test_cache_clear_on_change_t3_4.py`（:130-142）；
HTTP 客户端与假分类器复用 `tests/api/test_chat_contract.py` 的 `client` / `_StubClassifier`。
"""

from __future__ import annotations

import pytest

from app.api import chat_routes
from app.cache import store as cache_store
from tests.api.test_chat_contract import _StubClassifier, client  # noqa: F401

QUESTION = "向量库用什么存储？"
NOTE_TITLE = "向量库选型"
NOTE_CONTENT = "向量库选型：知澜用 Qdrant 内嵌模式做向量存储，向量化交给上游接口。"
NOTE_TITLE_B = "向量库选型（乙）"
NOTE_CONTENT_B = "向量库备选：Qdrant 也支持服务端模式，向量存储仍走内嵌。"

UPLOAD_FILENAME = "向量库补充材料.md"
UPLOAD_CHUNK = "向量库补充材料：向量存储仍用 Qdrant 内嵌模式，检索侧走双路召回。"

ANSWER_V1 = "据资料 [1]，向量库用 Qdrant 内嵌模式。"
ANSWER_V2 = "补一轮之后的回答：向量库仍用 Qdrant，但引用面变了。"
ANSWER_V3 = "再变一轮之后的回答：向量库存储方案以最新资料为准。"

NOTE_FILTERS = {"source_types": ["note"]}


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
    """假 bound model：**记录被调用次数**（「命中轮不再调模型」就断在它身上）。"""

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


def _install(monkeypatch, reply: str = ANSWER_V1) -> _CountingModel:
    """装模型 + 路由分类器（不连真上游）。"""
    model = _CountingModel(reply)
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )
    return model


@pytest.fixture
def cache_on(monkeypatch):
    """开态 + 独立清表（照 `tests/api/test_cache_clear_on_change_t3_4.py:130-142`）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 86400)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 256)
    cache_store.reset_cache()
    cache_store.reset_failure_counts()
    yield settings
    cache_store.reset_cache()
    cache_store.reset_failure_counts()


async def _flush_cache_writes() -> None:
    """等缓存写回的后台任务落定（fire-and-forget ⇒ 用例必须自己等）。"""
    await chat_routes.drain_cache_tasks()


async def _create_note(client, *, title=NOTE_TITLE, content=NOTE_CONTENT, category=""):  # noqa: ANN001
    resp = await client.post(
        "/api/notes", json={"title": title, "content": content, "category": category}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _ask(client, *, question=QUESTION) -> dict:  # noqa: ANN001
    """同题同范围问一轮（缓存键不含 `chat_id` ⇒ 每轮新会话照样能命中同一条）。"""
    resp = await client.post(
        "/api/chat", json={"message": question, "filters": dict(NOTE_FILTERS)}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _prime(client, model, *, reply=ANSWER_V1) -> dict:  # noqa: ANN001
    """首轮：真检索 + 真生成，并把这一轮写进缓存（其余腿的前提条件）。"""
    model.reply = reply
    body = await _ask(client)
    assert body["kind"] == "normal", f"开态首轮必须是库内命中（笔记范围）：{body}"
    assert body["refs"], "笔记范围首轮必须有引用行（否则后面的失效断言是空靶）"
    assert body["message"] == reply, body
    await _flush_cache_writes()
    assert cache_store.cache_size() == 1, "首轮之后缓存必须恰好一条（否则后面的断言是空靶）"
    return body


async def _ask_fresh(client, model, reply: str, *, expected_count: int) -> dict:  # noqa: ANN001
    """写事件之后重新问一轮：必须**重新生成**（不得命中写之前的条目）。"""
    model.reply = reply
    body = await _ask(client)
    assert body["message"] == reply, f"写事件之后仍回了写之前的缓存答案：{body['message']!r}"
    assert model.count == expected_count, "写事件之后必须重新生成（命中旧条目会少调一次模型）"
    return body


# ---------------------------------------------------------------------------
# 腿① 反向保护：没有写事件 ⇒ 仍命中
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unchanged_question_still_hits_cache(client, db, cache_on, monkeypatch):  # noqa: ANN001
    """腿① 两态同绿：未变场景同题**仍命中**（缓存效果不得被失效接线顺手打死）。"""
    model = _install(monkeypatch)
    await _create_note(client)
    await _prime(client, model)

    hits_before = cache_store.failure_counts()["hit"]
    body = await _ask(client)

    assert body["message"] == ANSWER_V1, body
    assert model.count == 1, "未变场景必须命中缓存（不得再调一次模型）"
    assert cache_store.failure_counts()["hit"] == hits_before + 1
    assert cache_store.cache_size() == 1


# ---------------------------------------------------------------------------
# 腿② 笔记元数据（标题/分类）写路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_note_metadata_change_clears_cache(client, db, cache_on, monkeypatch):  # noqa: ANN001
    """腿② 改标题 / 分类（**正文逐字不动**）⇒ 同题不得回旧答案。

    正文没变 ⇒ 存活校验（比正文指纹）不会判失活 ⇒ 只有**写路径接线**才可能让旧答案作废。
    未接线的现码：缓存仍命中 ⇒ RED。
    """
    model = _install(monkeypatch)
    note = await _create_note(client)
    await _prime(client, model)

    resp = await client.put(
        f"/api/notes/{note['id']}", json={"title": "向量库选型（改）", "category": "存储"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == NOTE_CONTENT, "本腿只改元数据：正文必须逐字未变"
    assert cache_store.cache_size() == 0, "笔记元数据写路径必须让缓存全失效（#6=A）"

    await _ask_fresh(client, model, ANSWER_V2, expected_count=2)


# ---------------------------------------------------------------------------
# 腿③ 新增材料（上传）写路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_material_upload_clears_cache(client, db, cache_on, monkeypatch):  # noqa: ANN001
    """腿③ 上传答复该问题的新材料 ⇒ 同题不得回上传前答案。未接线的现码 RED。"""
    model = _install(monkeypatch)
    await _create_note(client)
    await _prime(client, model)

    resp = await client.post(
        "/api/documents/upload",
        files={"file": (UPLOAD_FILENAME, UPLOAD_CHUNK.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code in (200, 201), resp.text
    assert cache_store.cache_size() == 0, "上传新材料必须让缓存全失效（#6=A）"

    await _ask_fresh(client, model, ANSWER_V2, expected_count=2)


# ---------------------------------------------------------------------------
# 腿④ 软删 → 恢复（恢复是独立写路径，须单独判别）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soft_delete_then_restore_clears_cache(client, db, cache_on, monkeypatch):  # noqa: ANN001
    """腿④ 软删 →（缓存重建）→ 恢复 ⇒ 恢复这一笔也必须让缓存全失效。

    ⚠️ 软删那一笔已把缓存清空 ⇒ 若「恢复」不接线，本腿就会**假绿**。所以中间刻意
    用仍在站内的另一条笔记**重建**一次缓存，再恢复 ⇒ 判别力落在「恢复」本身。
    """
    model = _install(monkeypatch)
    note_a = await _create_note(client)
    note_b = await _create_note(client, title=NOTE_TITLE_B, content=NOTE_CONTENT_B)
    await _prime(client, model)

    resp = await client.delete(f"/api/notes/{note_b['id']}")
    assert resp.status_code == 200, resp.text
    assert cache_store.cache_size() == 0, "软删（进回收站）必须让缓存全失效（#6=A）"

    body = await _ask_fresh(client, model, ANSWER_V2, expected_count=2)
    assert body["kind"] == "normal"
    await _flush_cache_writes()
    assert cache_store.cache_size() == 1, "甲笔记仍在站内 ⇒ 这一轮必须重新写回一条（判别前提）"
    assert note_a["id"] != note_b["id"]

    resp = await client.post("/api/trash/restore", json={"type": "note", "id": note_b["id"]})
    assert resp.status_code == 200, resp.text
    assert cache_store.cache_size() == 0, "恢复让行重新进入检索面 ⇒ 必须让缓存全失效（#6=A）"

    await _ask_fresh(client, model, ANSWER_V3, expected_count=3)


# ---------------------------------------------------------------------------
# 腿⑤ / 腿⑥ 彻底删除（回收站）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_referenced_note_clears_cache(client, db, cache_on, monkeypatch):  # noqa: ANN001
    """腿⑤ 彻底删除**被缓存条目引用的那条笔记** ⇒ 全失效 + 不得回旧答案。

    存活校验在本腿上本来就挡住了「回旧答案」（行没了 ⇒ 指纹无候选）⇒ 本腿的判别力
    落在 `#6=A` 的「**全失效**」上（未接线时 `cache_size()` 仍为 1 ⇒ RED）。
    """
    model = _install(monkeypatch)
    await _create_note(client)
    note_b = await _create_note(client, title=NOTE_TITLE_B, content=NOTE_CONTENT_B)
    await _prime(client, model)

    resp = await client.delete(f"/api/notes/{note_b['id']}")
    assert resp.status_code == 200, resp.text
    assert cache_store.cache_size() == 0

    await _ask_fresh(client, model, ANSWER_V2, expected_count=2)
    await _flush_cache_writes()
    assert cache_store.cache_size() == 1

    resp = await client.delete(f"/api/trash/note/{note_b['id']}")
    assert resp.status_code == 200, resp.text
    assert cache_store.cache_size() == 0, "彻底删除必须让缓存全失效（#6=A）"

    await _ask_fresh(client, model, ANSWER_V3, expected_count=3)


@pytest.mark.asyncio
async def test_purge_unrelated_item_clears_cache(client, db, cache_on, monkeypatch):  # noqa: ANN001
    """腿⑥ 彻底删除**与缓存条目无关**的条目（软删会话）⇒ 仍须全失效。

    这一条是 `#6=A`（正确性优先、全失效）最锋利的判别：被删的东西跟缓存里的材料
    毫无关系，任何「按材料精确摘除」的写法都过不了。未接线的现码 RED。
    """
    model = _install(monkeypatch)
    await _create_note(client)
    first = await _prime(client, model)

    resp = await client.delete(f"/api/chats/{first['chat_id']}")
    assert resp.status_code == 200, resp.text

    resp = await client.delete(f"/api/trash/chat/{first['chat_id']}")
    assert resp.status_code == 200, resp.text
    assert cache_store.cache_size() == 0, "彻底删除（与缓存材料无关的条目）也必须全失效（#6=A）"

    await _ask_fresh(client, model, ANSWER_V2, expected_count=2)


@pytest.mark.asyncio
async def test_empty_trash_clears_cache(client, db, cache_on, monkeypatch):  # noqa: ANN001
    """腿⑦ 清空回收站 ⇒ 全失效 + 不得回旧答案。未接线的现码 RED。"""
    model = _install(monkeypatch)
    await _create_note(client)
    first = await _prime(client, model)

    resp = await client.delete(f"/api/chats/{first['chat_id']}")
    assert resp.status_code == 200, resp.text

    resp = await client.post("/api/trash/empty")
    assert resp.status_code == 200, resp.text
    assert resp.json()["chats"] >= 1, resp.text
    assert cache_store.cache_size() == 0, "清空回收站必须让缓存全失效（#6=A）"

    await _ask_fresh(client, model, ANSWER_V2, expected_count=2)


# ---------------------------------------------------------------------------
# 腿⑧ 反向保护：读路径不得清缓存
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_paths_do_not_clear_cache(client, db, cache_on, monkeypatch):  # noqa: ANN001
    """腿⑧ 两态同绿：只读的笔记面（列表 / 分类过滤）**不得**让缓存失效。

    接线最容易 over-shoot 成「任何请求都清一遍」，那样缓存等于恒失效 ⇒ 本腿钉住上界。
    """
    model = _install(monkeypatch)
    await _create_note(client)
    await _prime(client, model)

    resp = await client.get("/api/notes")
    assert resp.status_code == 200, resp.text
    assert resp.json(), "列表必须有内容（否则这条腿没打到东西）"
    resp = await client.get("/api/notes", params={"category": ""})
    assert resp.status_code == 200, resp.text

    assert cache_store.cache_size() == 1, "读路径不得清缓存"
    body = await _ask(client)
    assert body["message"] == ANSWER_V1, body
    assert model.count == 1, "读路径之后缓存必须仍然有效（不得把缓存打成恒失效）"
