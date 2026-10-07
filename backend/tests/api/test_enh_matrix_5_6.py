"""阶段 5 · 条目 `5.6` 的**增强开关组合矩阵**（`2³ = 8` 格全因子）+ 换键断言 + 笔记材料只读探针。

## 这个文件为什么存在

`tests/verify_v2.py` 那六组跑的是**默认态**（三个增强开关全关）下的既有链路；
本文件补的是它**结构性覆盖不到**的那一片：`MEMORY_ENABLE` × `SEMANTIC_CACHE_ENABLE` ×
`GRAPHRAG_ENABLE` 的**全部 8 种组合**。开工包 §0.0 第 6 条实测标出三个**真空洞**——
`M+G` 从未同开、三开从未、`M+C` 有端到端用例但把抽取 stub 掉了
（`tests/api/test_semantic_cache_http.py:776`）⇒ 「抽取真跑 × 缓存命中」的交互**从未验过**。

| 面 | 断言 | 依据 |
|---|---|---|
| 8 格 × 四类 | 模型调用数 / 落库行数 / 检索与材料 / SSE 帧键集 | 开工包 §2.2 的表 |
| 换键（9 条） | `GRAPHRAG_*` 5 个旋钮改了 ⇒ **必须不命中**；`MEMORY_INJECT_MAX_CHARS` / `MEMORY_TOP_K` / `SEMANTIC_CACHE_TTL_SECONDS` / `SEMANTIC_CACHE_MAX_ENTRIES` 改了 ⇒ **仍必须命中** | 侦察件 §三「最关键的不对称」：前 6 个 `GRAPHRAG_*` 进 `cache/entry.py:175-182` 的 `graph` 子对象，后四者**不进任何键** |
| 只读探针 | 三开格：笔记材料（负数 `note_chunk_id` 占位）被删后缓存是否仍命中 | 开工包 §7.2 第 14 条（**未验证**的可疑边界，只探只报） |

## 纪律（照既有先例，不另立口径）

- **零出网**：本文件自带函数级 autouse 的 `deny_remote_io`（照 `tests/verify_v2.py:31-65` 抄），
  逐例打印 `OUTBOUND_ATTEMPTS=0`；假模型一律 `monkeypatch.setattr(chat_routes, "resolve_chat_model", ...)`
  注入（模板 `tests/api/test_memory_http.py:183-191`），并顺手关掉 `QUERY_CLARIFY_ENABLE` /
  `CHAT_COMPRESS_ENABLE`——否则会**第二个消费者**吃掉假模型的响应队列。
  **只有 `MEMORY_ENABLE` 会多一次模型调用**（记忆抽取），所以断言里单独给它记账。
- **不替换 `chat_routes.retrieve_materials`**：图通道就在它里面，替掉 = 没测
  （`tests/api/test_agent_audit_5_5.py:596` 有明文警告）。本文件**一次都没碰它**。
- **进程内状态每格前后都清**（缓存表 / 图计数 / 记忆计数）：`tests/api/conftest.py` 的两个钉子
  已经清过一次，**开态用例自己也要在开头清**（既有口径，别省）。
- **SSE 帧键集**用与 `tests/api/test_graph_channel_http.py:46-56` **同源同值**的 `DONE_KEYS`；
  `meta` 帧的键集 = `{"type", "chat_id", "skill_ids"}`（`chat_routes.py:1699` 是唯一构造点）。
- 本文件**只新建这一个文件**，不改任何既有资产、不碰 `app/**`。

> 诚实边界：本文件量的是「开了以后**多了什么**」与「关了以后**一点不多**」，
> 不断言增强的质量（那是 `5.4` 的 RAGAS 门禁与 `tests/eval` 的活）。
"""

from __future__ import annotations

import json
import socket
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.api import chat_routes
from app.cache import store as cache_store
from app.core.config import settings
from app.models import Chunk, Memory, MessageAttribution, Note, UsageEvent, WikiLink
from app.retrieval import graph
from app.retrieval.types import note_chunk_id
from app.session import memory as memory_mod
from app.usage import recorder as usage_recorder
# client / parse_sse / _StubClassifier 都在契约套件里（不重复造，与 5.1/5.2/5.3 同型）
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401
from tests.api.t0_wire import wire_sse_text
from tests.retrieval.conftest import seed_document
from tests.test_opt5_observation_usage import _provider

pytestmark = pytest.mark.asyncio

STREAM = "/api/chat/stream"

#: 冻结的聊天 ``done`` 帧键集（与 ``test_graph_channel_http.py:46-58`` 同源同值）
#: 🔴 2026-09-27：`回归修复` 拍板⑨「补」⇒ 新增第 10 键 `recovered`（02 v1.0.12）。
DONE_KEYS = frozenset(
    {
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
)
#: ``meta`` 帧的键集（唯一构造点 ``chat_routes.py:1699``）
META_KEYS = frozenset({"type", "chat_id", "skill_ids"})

#: `2³ = 8` 格全因子（`m` 外层、`c` 中层、`g` 内层 ⇒ 用例顺序固定，日志可逐格对照）
CELLS = [(m, c, g) for m in (False, True) for c in (False, True) for g in (False, True)]
CELL_IDS = [f"M{int(m)}C{int(c)}G{int(g)}" for m, c, g in CELLS]

#: 每格跑几轮。为什么是 3：`C+M` 同开时第 2 轮**必然**换键（第 1 轮的抽取写出了新记忆
#: ⇒ 第 2 轮的 `memory_text` 变了，这正是「5.1 的注入不被 5.2 静默吞掉」的机制），
#: 第 3 轮才与第 2 轮同键 ⇒「命中轮不再抽取」这条只能在第 3 轮上断。
ROUNDS = 3

#: 语料：A 是主命中的锚点，B **只可能由图通道的一跳带回来**（两篇互链）
ANCHOR_FILENAME = "系统概览.md"
ANCHOR_TEXT = "系统概览 讲的是整体架构。"
NEIGHBOUR_FILENAME = "向量索引.md"
NEIGHBOUR_TEXT = "倒排表与近邻图都存在本地。"
QUESTION = "系统概览"
#: 同时点 `[1]` 与 `[2]`：关态只有 1 条材料 ⇒ 解析器把越界的 `[2]` 丢掉（refs 仍恰好 1 条），
#: 开态有 2 条材料 ⇒ 两条都被点到。**一条固定回答覆盖两态**。
ANSWER = "据资料 [1] 与 [2]，整体架构见系统概览。"
#: 抽取侧固定产出的事实（第 1 轮真落库，第 2 轮起被 `(scope, text_hash)` 唯一键去重）
FACT_TEXT = "用户关注系统概览"
FACT_FACTS_JSON = '{"facts": [{"text": "用户关注系统概览", "kind": "interaction"}]}'
#: 换键断言里预置的那条记忆（**短**：改 `MEMORY_INJECT_MAX_CHARS` 也装得下 ⇒ 渲染段不变）
SEEDED_MEMORY = "用户偏好深色主题。"

#: 图旋钮（**进**缓存键 ⇒ 改了必须不命中）
GRAPH_KNOBS = {
    "GRAPHRAG_TOP_K": 4,
    "GRAPHRAG_SCORE_FACTOR": 0.75,
    "GRAPHRAG_HOP_DECAY": 0.25,
    "GRAPHRAG_WEAK_ALIAS_WEIGHT": 0.8,
    "GRAPHRAG_MAX_TERMS": 5,
}
#: 与缓存键**无关**的旋钮（改了仍必须命中）。四个都选「不会把渲染结果改掉」的值：
#: `MEMORY_INJECT_MAX_CHARS` / `MEMORY_TOP_K` 只影响**渲染**，而渲染段本身（`memory_text`）
#: 才是键的成分 ⇒ 只要注入段逐字不变，换旋钮就是**同键**（这正是本条要证明的那条不对称）。
NEUTRAL_KNOBS = {
    "MEMORY_INJECT_MAX_CHARS": 400,
    "MEMORY_TOP_K": 3,
    "SEMANTIC_CACHE_TTL_SECONDS": 3600,
    "SEMANTIC_CACHE_MAX_ENTRIES": 8,
}
KNOB_SWAPS = {**GRAPH_KNOBS, **NEUTRAL_KNOBS}
KNOB_DEFAULTS = {
    "GRAPHRAG_TOP_K": 6,
    "GRAPHRAG_SCORE_FACTOR": 0.5,
    "GRAPHRAG_HOP_DECAY": 0.5,
    "GRAPHRAG_WEAK_ALIAS_WEIGHT": 0.5,
    "GRAPHRAG_MAX_TERMS": 8,
    "MEMORY_INJECT_MAX_CHARS": 800,
    "MEMORY_TOP_K": 5,
    "SEMANTIC_CACHE_TTL_SECONDS": 86400,
    "SEMANTIC_CACHE_MAX_ENTRIES": 256,
}

#: 三开格里的只读探针（笔记材料：负数 `note_chunk_id` 占位）。
#: 构造要点：问题词命中的是笔记的 **分类**（图通道的弱别名），而**不在**笔记正文里
#: （关键词路按 ``row[2]``=content 打分 ⇒ 命中不了）⇒ 这条笔记材料**只能由图通道带回**。
PROBE_NOTE_TITLE = "随手记 07"
PROBE_NOTE_CATEGORY = "黑天鹅观测"
PROBE_NOTE_BODY = "该物种每年从西伯利亚迁到鄱阳湖过冬。"
PROBE_QUESTION = "黑天鹅"
PROBE_ANSWER = "据笔记 [1]，该物种会沿固定路线迁移。"

# --- 进程内的观测账（模块级；汇总夹具在 teardown 打印）------------------------
CELL_METRICS: dict[str, dict] = {}
FRAME_SETS: dict[str, list[tuple[frozenset, frozenset]]] = {}
OUTBOUND: list[str] = []


# ==========================================================================
# 两个模块级夹具：禁网（函数级）+ 汇总（模块级）
# ==========================================================================


@pytest.fixture(scope="module", autouse=True)
def matrix_summary(request):  # noqa: ARG001
    """模块级汇总（照 ``tests/verify_v2.py:68-77`` 的写法）。

    teardown 打印三件判据：**逐格结果行**（含实测数字）· ``增强组合：N/8 PASS``
    （**只有 8 格参与这个计数**，换键断言与探针不算）· ``TOTAL=<零出网计数>`` · 帧键集一致性。
    """
    results: dict[str, bool] = {}
    yield results

    cell_names = [f"test_enh_cell[{cell_id}]" for cell_id in CELL_IDS]
    for cell_id, name in zip(CELL_IDS, cell_names):
        passed = results.get(name)
        metrics = CELL_METRICS.get(name, {})
        print(f"{cell_id} 结果：{'PASS' if passed else 'FAIL'} | {metrics}")
    for name in sorted(results):
        if not name.startswith("test_enh_cell["):
            print(f"{name}: {'PASS' if results[name] else 'FAIL'}")

    passed_cells = sum(1 for name in cell_names if results.get(name))
    print(f"增强组合：{passed_cells}/{len(cell_names)} PASS")

    all_sets = [item for sets in FRAME_SETS.values() for item in sets]
    distinct = len(set(all_sets))
    same = bool(all_sets) and distinct == 1 and len(all_sets) == len(cell_names) * ROUNDS
    print(f"帧键集两态一致：{'是' if same else '否'}（{len(all_sets)} 份 / {distinct} 种）")
    print(f"TOTAL={len(OUTBOUND)}")


@pytest.fixture(autouse=True)
def deny_remote_io(monkeypatch, request, matrix_summary):  # noqa: ARG001
    """**零出网**守门（照 ``tests/verify_v2.py:31-65`` 抄：劫持三个 socket 入口，先记账再抛）。

    白名单只 ``127.0.0.1`` / ``::1`` / ``localhost`` / ``None``——测试库与 Qdrant 都在本机。
    """
    attempts: list[str] = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_lookup = socket.getaddrinfo

    def check(host):
        if str(host) not in {"127.0.0.1", "::1", "localhost", "None"}:
            attempts.append(str(host))
            raise AssertionError("增强组合矩阵试图出网")

    def connect(sock, address):
        if isinstance(address, tuple):
            check(address[0])
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if isinstance(address, tuple):
            check(address[0])
        return original_connect_ex(sock, address)

    def lookup(host, *args, **kwargs):
        check(host)
        return original_lookup(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(settings, "MCP_SERVERS", "")
    failures_before = request.session.testsfailed
    yield
    OUTBOUND.extend(attempts)
    matrix_summary[request.node.name] = (
        request.session.testsfailed == failures_before and not attempts
    )
    assert attempts == [], f"出网尝试（含被吞掉的）：{attempts}"
    print("OUTBOUND_ATTEMPTS=0")


@pytest.fixture(autouse=True)
def _pin_cell(monkeypatch):
    """每格统一：**三开关显式钉关** + 前后各清一次进程内状态。

    开关由用例自己覆盖（本夹具先跑，用例内的 ``setattr`` 后跑 ⇒ 后写者生效）。
    """
    monkeypatch.setattr(settings, "MEMORY_ENABLE", False)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", False)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)
    _reset_process_state()
    yield
    _reset_process_state()


def _reset_process_state() -> None:
    """清三个模块级状态（缓存表 / 缓存计数 / 图计数 / 记忆计数）。"""
    cache_store.reset_cache()
    cache_store.reset_failure_counts()
    graph.reset_counters()
    memory_mod.reset_failure_counts()


# ==========================================================================
# 助手：假模型 / 语料 / 一轮
# ==========================================================================


class _MatrixModel:
    """双方法替身：``stream``（流式回答）与 ``generate``（**只**给记忆抽取用）。

    - 抽取调用按 ``system_prompt == memory_mod.EXTRACT_SYSTEM`` 识别——同步链路的回答与抽取
      用的是同一个 ``generate``，只有系统提示能区分二者（这顺带证明「抽取真的调了模型」）；
    - ``answer`` / ``facts`` 都是**可变属性**（三开格的探针要临时把它们换掉）。
    """

    def __init__(self, answer: str = ANSWER, facts: str = FACT_FACTS_JSON) -> None:
        self.answer = answer
        self.facts = facts
        self.generate_calls: list[dict] = []
        self.stream_calls: list[dict] = []
        self.http_sends: list[dict] = []
        self._inner = None
        self.spec = SimpleNamespace(
            id="enh-matrix-fake", kind="chat", provider="fake",
            base_url="http://127.0.0.1/enh-matrix-fixture", model="enh-matrix-fake",
        )

    @property
    def extract_calls(self) -> list[dict]:
        return [call for call in self.generate_calls if call["extract"]]

    @property
    def answer_generate_calls(self) -> list[dict]:
        return [call for call in self.generate_calls if not call["extract"]]

    @property
    def answer_calls(self) -> int:
        return len(self.stream_calls) + len(self.answer_generate_calls)

    async def generate(self, messages, **kwargs):  # noqa: ANN001, ANN003
        from cubeloop.providers import faux_assistant_message

        system_prompt = kwargs.get("system_prompt") or ""
        is_extract = system_prompt == memory_mod.EXTRACT_SYSTEM
        self.generate_calls.append(
            {
                "system_prompt": system_prompt,
                "messages": list(messages),
                "extract": is_extract,
            }
        )
        if self._inner is not None:
            return await self._inner.generate(messages, **kwargs)
        return faux_assistant_message(self.facts if is_extract else self.answer)

    async def stream(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.stream_calls.append(
            {
                "system_prompt": kwargs.get("system_prompt") or "",
                "messages": list(messages),
                "kwargs": dict(kwargs),
            }
        )
        if self._inner is not None:
            return await self._inner.stream(messages, **kwargs)
        text = self.answer

        async def _iterate():
            yield SimpleNamespace(type="text_delta", delta=text)

        return _iterate()


@pytest_asyncio.fixture
async def matrix_transport(db):
    """Only usage checks delegate their recording model to real SDK sends."""
    providers = []

    def connect(model):
        def handler(request):
            assert str(request.url).startswith("https://controlled.invalid/v1")
            body = json.loads(request.content)
            model.http_sends.append(body)
            is_extract = any(
                item.get("role") == "system" and item.get("content") == memory_mod.EXTRACT_SYSTEM
                for item in body["messages"]
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content=wire_sse_text(model.facts if is_extract else model.answer),
            )

        provider = _provider(handler)
        providers.append(provider)
        model._inner = provider.model("enh-matrix-fake")
        return model

    try:
        yield connect
    finally:
        try:
            await db.rollback()
            await memory_mod.drain_memory_tasks()
            await usage_recorder.drain_pending()
            await chat_routes.drain_cache_tasks()
        finally:
            for provider in providers:
                await provider._client.close()


def _install(monkeypatch, *, answer: str = ANSWER, facts: str = FACT_FACTS_JSON) -> _MatrixModel:
    """装假模型 + 固定分类器 + 关掉会多吃一口模型调用的两处（澄清判定 / 历史压缩）。

    模板见 ``tests/api/test_memory_http.py:183-191``。**这是本文件唯一不出网的前提**：
    ``tests/api/conftest.py`` 里**没有**假模型 autouse 钉子。
    """
    model = _MatrixModel(answer=answer, facts=facts)
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    monkeypatch.setattr(settings, "CHAT_COMPRESS_ENABLE", False)
    monkeypatch.setattr(settings, "ROUTING_MODE", "regex")
    return model


async def _seed_two_documents(db):
    """造 A（主命中）与 B（**只可能由图通道一跳带回**的邻居）并把两篇互链起来。

    返回 ``(anchor_id, neighbour_id, {document_id: chunk_id})``——**全是裸 int**：
    ``db.rollback()`` 会让 ORM 对象过期，之后再读它们的属性会触发**同步**懒加载
    （``MissingGreenlet``）。口径与 ``tests/api/test_graph_channel_http.py:125-148`` 同源。
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


async def _table_counts(session) -> dict[str, int]:
    """三张表的行数（先 ``commit`` 结束上一个读事务——HTTP 那一侧是**另一个会话**提交的）。

    MySQL REPEATABLE READ 下不这么做会看不见后来落的行（既有口径，见 ``test_memory_http``）。
    """
    await session.commit()
    out: dict[str, int] = {}
    for key, model in (
        ("usage_events", UsageEvent),
        ("memories", Memory),
        ("message_attributions", MessageAttribution),
    ):
        out[key] = int(
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
        )
    return out


async def _turn(client, db, fresh_db, model, *, question: str) -> dict:
    """发一轮流式问答，返回**这一轮的增量账**（模型调用 / 落库 / 命中 / 图计数 / 帧键集）。"""
    graph_before = graph.failure_counts()
    cache_before = cache_store.failure_counts()
    tables_before = await _table_counts(fresh_db)
    answer_calls_before = model.answer_calls
    extract_calls_before = len(model.extract_calls)
    sends_before = len(model.http_sends)
    await fresh_db.rollback()
    await db.rollback()

    response = await client.post(STREAM, json={"message": question})
    assert response.status_code == 200, response.text
    frames = parse_sse(response.text)
    assert frames and frames[-1]["type"] == "done", [frame["type"] for frame in frames]

    # 三个 fire-and-forget 的**唯一等待点**（各自一个；usage 那条不覆盖另外两条）
    await db.rollback()
    await chat_routes.drain_cache_tasks()
    await memory_mod.drain_memory_tasks()
    await usage_recorder.drain_pending()

    tables_after = await _table_counts(fresh_db)
    await fresh_db.rollback()
    graph_after = graph.failure_counts()
    cache_after = cache_store.failure_counts()
    done, meta = frames[-1], frames[0]
    return {
        "chat_id": done["chat_id"],
        "kind": done["kind"],
        "answer_source": done["answer_source"],
        "refs": list(done["refs"]),
        "text": "".join(frame["text"] for frame in frames if frame["type"] == "token"),
        "done_keys": frozenset(done),
        "meta_keys": frozenset(meta),
        "answer_calls": model.answer_calls - answer_calls_before,
        "extract_calls": len(model.extract_calls) - extract_calls_before,
        "http_sends": len(model.http_sends) - sends_before,
        "hits": cache_after["hit"] - cache_before["hit"],
        "graph_calls": graph_after.get("calls", 0) - graph_before.get("calls", 0),
        "graph_materials": (
            graph_after.get("materials", 0) - graph_before.get("materials", 0)
        ),
        "usage": tables_after["usage_events"] - tables_before["usage_events"],
        "memories": tables_after["memories"] - tables_before["memories"],
        "attributions": (
            tables_after["message_attributions"] - tables_before["message_attributions"]
        ),
    }


# ==========================================================================
# 一、`2³ = 8` 格全因子（每格断四类）
# ==========================================================================

async def test_unrelated_memory_keeps_real_first_cache_entry_and_next_hit(
    client, db, fresh_db, monkeypatch
):
    """不相关记忆仍存档，但不注入当前问题；真正首次写入后应继续命中。"""
    _reset_process_state()
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)
    await _seed_two_documents(db)
    model = _install(monkeypatch, facts='{"facts":[{"text":"用户在做增强组合矩阵验收","kind":"interaction"}]}')
    first = await _turn(client, db, fresh_db, model, question=QUESTION)
    assert first["hits"] == 0 and first["answer_calls"] == 1 and first["memories"] == 1
    assert cache_store.cache_size() == 1
    rows = await memory_mod.recall(db, question=QUESTION)
    assert rows == []
    second = await _turn(client, db, fresh_db, model, question=QUESTION)
    assert second["hits"] == 1 and second["answer_calls"] == 0 and second["memories"] == 0
    assert second["text"] == ANSWER


@pytest.mark.parametrize("memory,cache,graph_enabled", CELLS, ids=CELL_IDS)
async def test_enh_cell(
    client, db, fresh_db, monkeypatch, memory, cache, graph_enabled, request, matrix_transport
):
    """**每一格**：四类断言（模型调用数 / 落库行数 / 检索与材料 / SSE 帧键集）+ 零出网。

    命中期望（本轮 `hits` 是否 == 1）：

    - `C` 关 ⇒ 一轮都不命中；
    - `C` 开、`M` 关 ⇒ 第 2 轮起命中（`memory_text` 恒空串，键不变）；
    - `C` 开、`M` 开 ⇒ **第 2 轮不命中**（第 1 轮真抽出了记忆 ⇒ `memory_text` 从空串变成渲染段，
      键随之改变 —— 这就是「5.1 的注入不会被 5.2 静默吞掉」的机制证据），第 3 轮命中。
    """
    name = request.node.name
    cell_id = name.split("[", 1)[1].rstrip("]")
    print(f"生效开关：M={int(memory)} C={int(cache)} G={int(graph_enabled)}")

    # 开态用例**自己也要在开头清**（钉子已清过一次是钉子的事，既有口径别省）
    _reset_process_state()
    monkeypatch.setattr(settings, "MEMORY_ENABLE", memory)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", cache)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", graph_enabled)

    await _seed_two_documents(db)
    model = matrix_transport(_install(monkeypatch))

    if not cache:
        expected_hits = [False] * ROUNDS
    elif memory:
        expected_hits = [False, False] + [True] * (ROUNDS - 2)
    else:
        expected_hits = [False] + [True] * (ROUNDS - 1)

    turns = [
        await _turn(client, db, fresh_db, model, question=QUESTION) for _ in range(ROUNDS)
    ]

    for index, turn in enumerate(turns):
        where = f"{cell_id} 第 {index + 1} 轮"
        want_hit = expected_hits[index]

        # ---- ④ SSE 帧键集：两态**逐字相同**（逐格与冻结值比；跨格一致性由汇总行核）----
        assert turn["done_keys"] == DONE_KEYS, (
            f"{where}：done 帧键集被改动 —— 多 {sorted(turn['done_keys'] - DONE_KEYS)} / "
            f"少 {sorted(DONE_KEYS - turn['done_keys'])}"
        )
        assert turn["meta_keys"] == META_KEYS, (
            f"{where}：meta 帧键集被改动 —— {sorted(turn['meta_keys'])}"
        )
        FRAME_SETS.setdefault(name, []).append((turn["done_keys"], turn["meta_keys"]))

        # ---- 命中与否（`store.failure_counts()['hit']` 是唯一可观测面）----
        assert (turn["hits"] == 1) is want_hit, (
            f"{where}：期望{'命中' if want_hit else '不命中'}，实测 hits={turn['hits']}"
        )

        # ---- ① 模型调用数 ----
        assert turn["answer_calls"] == (0 if want_hit else 1), f"{where}：回答模型调用数 {turn}"
        assert turn["extract_calls"] == (
            0 if (want_hit or not memory) else 1
        ), f"{where}：抽取模型调用数 {turn}"

        # ---- ② 落库行数 ----
        want_usage = 0 if want_hit else 1 + int(memory)
        assert turn["usage"] == want_usage, (
            f"{where}：`usage_events` 增量应为 {want_usage}，实际 {turn['usage']}"
        )
        assert turn["http_sends"] == turn["usage"] == (
            turn["answer_calls"] + turn["extract_calls"]
        ), f"{where}：HTTP 发送数与用量行数必须逐轮一致 {turn}"
        want_memories = 1 if (memory and index == 0) else 0
        assert turn["memories"] == want_memories, (
            f"{where}：`memories` 增量应为 {want_memories}，实际 {turn['memories']}"
            "（第 2 轮起的抽取产出与第 1 轮同一条事实 ⇒ 去重 ⇒ 零新行）"
        )

        # ---- ③ 检索与材料 ----
        assert turn["kind"] == "normal", f"{where}：kind={turn['kind']}"
        assert turn["answer_source"] == "kb", f"{where}：answer_source={turn['answer_source']}"
        assert len(turn["refs"]) == 1 + int(graph_enabled), (
            f"{where}：refs 应为 {1 + int(graph_enabled)} 条，实际 {len(turn['refs'])}"
        )
        assert turn["attributions"] == 1 + int(graph_enabled), (
            f"{where}：`message_attributions` 应落 {1 + int(graph_enabled)} 行，"
            f"实际 {turn['attributions']}"
        )
        assert turn["text"] == ANSWER, f"{where}：正文被改动：{turn['text']!r}"

    hits_total = sum(turn["hits"] for turn in turns)
    assert hits_total == sum(expected_hits)

    # ---- G 开：图材料**确实多了一条**（不是「键集相同」的空对空）----
    refs = turns[-1]["refs"]
    if graph_enabled:
        assert {item["title"] for item in refs} == {ANCHOR_FILENAME, NEIGHBOUR_FILENAME}, refs
        assert [item["ordinal"] for item in refs] == [1, 2]
        assert all(item["type"] == "document" for item in refs)
    else:
        assert [item["title"] for item in refs] == [ANCHOR_FILENAME], refs

    # ---- 终态：`关 = 零影响` / `开 = 有动作` ----
    counts = graph.failure_counts()
    if graph_enabled:
        assert counts["calls"] == ROUNDS - hits_total, (
            f"{cell_id}：图通道调用数应为「未命中轮数」{ROUNDS - hits_total}，实际 {counts}"
        )
        assert counts["materials"] >= 1, counts
    else:
        assert counts == {}, f"{cell_id}：关态图计数必须为空：{counts}"

    if cache:
        want_entries = 2 if memory else 1
        assert cache_store.cache_size() == want_entries, (
            f"{cell_id}：缓存应有 {want_entries} 条（`M+C` 同开时第 1/2 轮各写一条不同键），"
            f"实际 {cache_store.cache_size()}"
        )
    else:
        assert cache_store.cache_size() == 0, (
            f"{cell_id}：关态缓存表必须为空：{cache_store.cache_size()}"
        )

    if not (memory or cache or graph_enabled):
        # A5「关态 = 零影响」的显式版（逐条独立可读，不靠上面的通用式反推）
        assert [turn["usage"] for turn in turns] == [1, 1, 1]
        assert [turn["answer_calls"] for turn in turns] == [1, 1, 1]
        assert [turn["extract_calls"] for turn in turns] == [0, 0, 0]
        assert [turn["hits"] for turn in turns] == [0, 0, 0]
        assert sum(turn["memories"] for turn in turns) == 0
        assert len(refs) == 1

    CELL_METRICS[name] = {
        "M": int(memory),
        "C": int(cache),
        "G": int(graph_enabled),
        "answer_calls": model.answer_calls,
        "extract_calls": len(model.extract_calls),
        "usage_events": sum(turn["usage"] for turn in turns),
        "http_sends": len(model.http_sends),
        "memories": sum(turn["memories"] for turn in turns),
        "attributions": sum(turn["attributions"] for turn in turns),
        "refs": len(refs),
        "hit_rounds": hits_total,
        "cache_size": cache_store.cache_size(),
        "graph": dict(counts),
    }
    print(f"{cell_id} 实测：{CELL_METRICS[name]}")

    # ---- 只读探针（**只探、只报**，不参与 8 格计数）：三开格专属 ----
    if memory and cache and graph_enabled:
        probe = await _note_material_probe(client, db, fresh_db, model)
        CELL_METRICS[name]["probe"] = probe
        print(f"笔记材料探针：{probe}")


# ==========================================================================
# 二、只读探针：笔记材料（负数 `note_chunk_id` 占位）被删后缓存是否仍命中
# ==========================================================================


async def _note_material_probe(client, db, fresh_db, model) -> str:
    """三开格里的**只读探针**（开工包 §7.2 第 14 条，**未验证**的可疑边界）。

    推理链：图通道（以及关键词路）带回的**笔记材料**用负数 ``note_chunk_id`` 占位
    （``app/retrieval/graph.py:589-590`` / ``app/retrieval/keyword.py:337-348``），
    而 ``chat_routes.py:585-589``（写回侧的 ``fingerprints``）与
    ``app/cache/entry.py:403-404``（命中侧的存活校验）**都跳过非正 id**
    ⇒ 理论上「笔记被删后，缓存仍命中该笔记材料」。

    **只探、只报**：本函数不改产品代码、断言只落在「探针本身跑通了没有」上，
    结论以字符串返回，由用例打印。变量隔离：临时把假模型的抽取产出钉成空
    （否则抽取写新记忆 ⇒ ``memory_text`` 变 ⇒ 第二轮因为**另一个**原因不命中，探针会被掩盖）。
    """
    try:
        note = Note(
            title=PROBE_NOTE_TITLE,
            content=PROBE_NOTE_BODY,
            category=PROBE_NOTE_CATEGORY,
            kb_id=1,
        )
        db.add(note)
        await db.commit()
        note_id = int(note.id)
        material_key = str(note_chunk_id(note_id))

        model.facts = '{"facts": []}'
        model.answer = PROBE_ANSWER

        keys_before = set(cache_store._STORE)
        first = await _turn(client, db, fresh_db, model, question=PROBE_QUESTION)
        new_keys = set(cache_store._STORE) - keys_before
        entry = cache_store._STORE[next(iter(new_keys))] if new_keys else None
        fingerprints = dict(entry.fingerprints or {}) if entry is not None else {}
        note_refs = [item for item in first["refs"] if item.get("type") == "note"]

        # 删笔记（软删：`deleted_at` 非空 —— 产品的删除路径就是这个语义）
        note.deleted_at = datetime.now()
        await db.commit()

        second = await _turn(client, db, fresh_db, model, question=PROBE_QUESTION)
        second_notes = [item for item in second["refs"] if item.get("type") == "note"]

        detail = (
            f"图通道本轮产出 {first['graph_materials']} 条 / 本轮 refs {len(first['refs'])} 条"
            f"（其中笔记 {len(note_refs)} 条，chunk_key={note_refs[0]['chunk_id']!r}）；"
            f"笔记 material.chunk_id={material_key}（负数占位，note_id={note_id}）；"
            f"缓存条目的 fingerprints={fingerprints}"
            f"（不含该负数 id：{material_key not in fingerprints}）；"
            f"删笔记后第二轮：hits={second['hits']}、模型调用 {second['answer_calls']} 次、"
            f"refs 里的笔记 {len(second_notes)} 条"
        )
        if not note_refs:
            return f"探不出来（本轮材料里没有笔记材料：{detail}）"
        if entry is None:
            return f"探不出来（第一轮没写回缓存 ⇒ 第二轮不可能命中：{detail}）"
        if second["hits"] == 1 and second["answer_calls"] == 0:
            return f"**成立**：笔记已软删，缓存仍命中该笔记材料（{detail}）"
        if second["hits"] == 0:
            return f"不成立：笔记删后缓存未命中（{detail}）"
        return f"形态不明（{detail}）"
    except Exception as exc:  # noqa: BLE001 —— 探针**只探只报**，绝不把 8 格判据拖红
        return f"探不出来（{exc.__class__.__name__}: {exc}）"


# ==========================================================================
# 三、换键断言（≥9 条，端到端）：旋钮改一个 ⇒ 期望的命中方向
# ==========================================================================


@pytest.mark.parametrize("knob", list(KNOB_SWAPS))
async def test_knob_swap_must_keep_the_expected_hit_behaviour(
    client, db, fresh_db, monkeypatch, knob, request, matrix_transport
):
    """**A9**：缓存开 + 只改**一个**旋钮 ⇒ 前 5 个 `GRAPHRAG_*` 必须**不命中**，后 4 个仍必须**命中**。

    不对称的根源（侦察件 §三，**本条最容易写反的一格**）：

    - ``cache/entry.py:175-182`` 的 ``retrieval_fingerprint().graph`` 把 **6 个** `GRAPHRAG_*`
      全放进了键（``enabled`` + 5 个旋钮）⇒ 改任一个 = 换键 = 不命中；
    - 同一文件的 ``:184-190`` **不含** `MEMORY_INJECT_MAX_CHARS` / `MEMORY_TOP_K` /
      `SEMANTIC_CACHE_TTL_SECONDS` / `SEMANTIC_CACHE_MAX_ENTRIES` ⇒ 改它们**键不变** ⇒ 仍命中。
      ⚠️ 但它们**通过 ``memory_text`` 间接影响结果**（渲染段变了，键当然变）——所以本用例把两个
      `MEMORY_*` 旋钮换成「渲染段逐字不变」的值（记忆只有一条短事实），把间接影响摘干净，
      量到的就是「旋钮本身不在键里」这一条。
    """
    expect_hit = knob in NEUTRAL_KNOBS
    _reset_process_state()
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", knob in GRAPH_KNOBS)
    memory_on = knob in ("MEMORY_INJECT_MAX_CHARS", "MEMORY_TOP_K")
    monkeypatch.setattr(settings, "MEMORY_ENABLE", memory_on)

    await _seed_two_documents(db)
    if memory_on:
        # 一条**短**记忆：800 / 400 的预算、5 / 3 的条数上限都装得下 ⇒ 渲染段逐字不变
        await memory_mod.remember(db, text=SEEDED_MEMORY)
        await db.commit()
    model = matrix_transport(_install(monkeypatch, facts='{"facts": []}'))

    first = await _turn(client, db, fresh_db, model, question=QUESTION)
    assert first["http_sends"] == first["usage"] == 1 + int(memory_on), first
    assert first["hits"] == 0 and first["answer_calls"] == 1, first
    assert cache_store.cache_size() == 1, (
        f"第一轮应当写回**恰好一条**缓存，实际 {cache_store.cache_size()}"
    )
    assert len(first["refs"]) == 1 + int(knob in GRAPH_KNOBS), first["refs"]

    # 只改这一个旋钮，别的（语料 / 问题 / 开关 / 历史 / 记忆）一个字不动
    monkeypatch.setattr(settings, knob, KNOB_SWAPS[knob])
    second = await _turn(client, db, fresh_db, model, question=QUESTION)
    assert second["http_sends"] == second["usage"] == int(not expect_hit), second

    actual_hit = second["hits"] == 1
    print(
        f"换键断言：{knob} {KNOB_DEFAULTS[knob]} → {KNOB_SWAPS[knob]} ⇒ "
        f"期望{'命中' if expect_hit else '不命中'}、实测{'命中' if actual_hit else '不命中'}"
        f"（模型调用 {second['answer_calls']} 次 / 缓存 {cache_store.cache_size()} 条）"
    )
    if expect_hit:
        assert actual_hit, f"{knob} 不在缓存键里，改了它**仍必须命中**（实际没命中）"
        assert second["answer_calls"] == 0, f"{knob}：命中轮不该再调模型"
        assert second["text"] == first["text"], "命中轮应当逐字返回缓存里的正文"
        assert second["usage"] == 0, "命中轮不记用量（没调模型）"
    else:
        assert not actual_hit, f"{knob} 在缓存键里，改了它**必须不命中**（实际命中了旧答案）"
        assert second["answer_calls"] == 1, f"{knob}：换键后应当重跑检索与模型"
        assert second["usage"] == 1, f"{knob}：重跑的那一轮要记一条用量"
