"""跨会话记忆（阶段 5 · 条目 5.1）的**端到端**用例：走 ``POST /api/chat`` / ``POST /api/chat/stream``。

## 为什么必须有这一条

``tests/session/test_memory.py`` 把 ``app/session/memory.py`` 自己钉得很死，但**接线**可以整段断掉
而那批用例全绿：把 ``chat_routes.build_turn_plan`` 末尾的 ``memory_text=...`` 删掉、把
``persist_turn`` 末尾的 ``schedule_extract(...)`` 删掉、或把 ``answer.system_prompt`` 的
``memory_text`` 透传删掉——纯函数层一个字都不会红。所以这一条**走 HTTP**，钉四件事：

| 用例 | 编号 | 一句话 |
|---|---|---|
| ``test_a4_...`` | A4 | 开态一轮 ⇒ 抽取真发生、``memories`` 真落行（含 ``source_chat_id`` == 返回的 chat_id） |
| ``test_a5_...`` | A5 | 三类抽取失败（调用炸 / 无 JSON / ``facts`` 空）⇒ 回答照常、正文逐字不变、零行、对应计数 +1 |
| ``test_a6_...`` | A6 | 注入不改 SSE 契约（``done`` 键集、``meta``/``token`` 形状、``refs``/``kind``/``answer_source``） |
| ``test_a7_...`` | A7 | 注入进**系统提示**、**不进 history**（``plan.history`` 两态逐字相同；模型收到的消息里没有记忆段） |
| ``test_a8_...`` | A8 | 同一问题两轮 ⇒ 仍只有一行（去重幂等） |
| ``test_a10_...`` | A10 | 跨会话：会话 1 抽出的记忆在会话 2 的提问里被注入；记忆轮**不带 refs** |
| ``test_a11_...`` | A11 | 关态一轮：``memories`` 行数不变、模型系统提示里没有 ``MEMORY_HEADER``、零抽取调用 |
| ``test_a19_...`` | A19 | Agent 轮：**收尾回答**的提示词带记忆段，CubeLoop 的决策提示不带（+ ``runner.py`` 静态面） |
| ``test_stopped_...`` | 收口 | 停止轮**不抽**（回答不完整）——``kind="stopped"`` ⇒ 零新行、零抽取调用 |

## 纪律

- **零出网**：模型一律 ``monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: fake)``；
  路由分类器换成 ``_StubClassifier``；检索 / 入库 / 重排的隔离由 ``tests/api/conftest.py`` 的
  autouse 钉子负责（本文件不重复造）；
- ``MEMORY_ENABLE`` **每个用例显式给值**（``tests/api/conftest.py`` 的 autouse ``pin_memory_off``
  默认钉 False，开态用例自己覆盖）；澄清判定与压缩在用例内钉死，避免它们多吃一口模型调用
  把「抽取调用了几次」的账算错；
- ``memories`` 的读取一律用 ``fresh_db``（HTTP 那一侧是**另一个会话**提交的；``db`` 夹具在造数时
  已开过读事务，MySQL REPEATABLE READ 下看不见后来的行）。

本文件**不改实现、不改其它用例文件**；实现若有缺陷只写进交付报告。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.agent import runner as agent_runner
from app.api import chat_routes
from app.api.chat_schemas import ChatRequest
from app.core.config import settings
from app.models import Memory, UsageEvent
from app.session import memory as memory_mod
from app.session import repository as repo
from app.usage import recorder as usage_recorder
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401
from tests.api.t0_wire import wire_sse_text
from tests.test_opt5_observation_usage import _provider

pytestmark = pytest.mark.asyncio

SYNC = "/api/chat"
STREAM = "/api/chat/stream"

#: 回答链路的固定正文（ON / OFF 两态对拍用）
ANSWER_TEXT = "好的，这是一句正常回答。"

#: 抽取侧固定产出的事实
FACT_TEXT = "用户在做跨会话记忆验收"
FACT_FACTS_JSON = '{"facts": [{"text": "用户在做跨会话记忆验收", "kind": "interaction"}]}'

#: 预置记忆的正文（造「确实有东西可注入」的场景）
SEEDED_TEXT = "用户偏好深色主题，讨厌蓝光。"


# --------------------------------------------------------------------------
# 助手
# --------------------------------------------------------------------------


class _MemoryAwareModel:
    """双方法替身：``generate``（同步回答 **+** 记忆抽取）、``stream``（流式回答）。

    - 抽取调用按 ``system_prompt == memory.EXTRACT_SYSTEM`` 识别——**同步链路的回答与抽取
      用的是同一个** ``generate``，只有系统提示能区分二者（这也顺带证明「抽取真的调了模型」
      而不是「碰巧没抽也算过」）；
    - ``facts`` 三态对应 A5 的三类失败：字符串（正常）/ 没有 JSON 的散文 / ``{"facts": []}``；
      ``facts_error`` 非空则抽取调用抛异常（回答调用**不受影响**）；
    - 每次调用都记下 ``system_prompt`` 与 ``messages``：A6 / A7 / A11 / A19 的硬证据都在这里。
    """

    def __init__(
        self,
        answer: str = ANSWER_TEXT,
        facts: str = FACT_FACTS_JSON,
        *,
        facts_error: Exception | None = None,
        chunks: list[str] | None = None,
    ) -> None:
        self.answer = answer
        self.facts = facts
        self.facts_error = facts_error
        self.chunks = list(chunks) if chunks is not None else None
        self.generate_calls: list[dict] = []
        self.stream_calls: list[dict] = []
        self._inner = None
        self.spec = SimpleNamespace(id="memory-fake")

    # --- 记账面（用例断言用）-----------------------------------------------
    @property
    def extract_calls(self) -> list[dict]:
        return [call for call in self.generate_calls if call["extract"]]

    @property
    def answer_generate_calls(self) -> list[dict]:
        return [call for call in self.generate_calls if not call["extract"]]

    @property
    def all_prompts(self) -> list[str]:
        return [call["system_prompt"] for call in self.generate_calls + self.stream_calls]

    # --- 模型面 ------------------------------------------------------------
    async def generate(self, messages, **kwargs):  # noqa: ANN001, ANN003
        from cubeloop.providers import faux_assistant_message

        system_prompt = kwargs.get("system_prompt") or ""
        is_extract = system_prompt == memory_mod.EXTRACT_SYSTEM
        self.generate_calls.append(
            {
                "system_prompt": system_prompt,
                "messages": list(messages),
                "extract": is_extract,
                "kwargs": dict(kwargs),
            }
        )
        if is_extract:
            if self.facts_error is not None:
                raise self.facts_error
            if self._inner is not None:
                return await self._inner.generate(messages, **kwargs)
            return faux_assistant_message(self.facts)
        if self._inner is not None:
            return await self._inner.generate(messages, **kwargs)
        return faux_assistant_message(self.answer)

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
        chunks = self.chunks if self.chunks is not None else [self.answer]

        async def _iterate():
            for chunk in chunks:
                await asyncio.sleep(0.01)  # 留出「停止信号落点」，也让 token 分帧
                yield SimpleNamespace(type="text_delta", delta=chunk)

        return _iterate()


@pytest_asyncio.fixture
async def observed_memory_model(db):
    """A13 alone uses SDK HTTP sends; failure/chunk/stop doubles stay unchanged."""
    model = _MemoryAwareModel()
    sends = []

    def handler(request):
        assert str(request.url).startswith("https://controlled.invalid/v1")
        body = json.loads(request.content)
        sends.append(body)
        is_extract = any(
            item.get("role") == "system" and item.get("content") == memory_mod.EXTRACT_SYSTEM
            for item in body["messages"]
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=wire_sse_text(model.facts if is_extract else model.answer),
        )

    provider = _provider(handler)
    model._inner = provider.model("memory-fake")
    try:
        yield model, sends
    finally:
        try:
            await db.rollback()
            await memory_mod.drain_memory_tasks()
            await usage_recorder.drain_pending()
        finally:
            await provider._client.close()


class _RecordingModel:
    """**记录壳**：把每次 ``stream`` / ``generate`` 的 ``system_prompt`` 抄下来，再原样转发。

    为什么 Agent 轮要用它（而不是自己写一个假模型）：CubeLoop 的决策调用会带
    ``tools=`` / ``options=``，交给真 Faux 去跑才不会在「事件形状」上失真；
    这里只加一层「谁收到了什么提示词」的记录。
    """

    def __init__(self, inner) -> None:  # noqa: ANN001
        self._inner = inner
        self.stream_calls: list[dict] = []
        self.generate_calls: list[dict] = []
        self.spec = getattr(inner, "spec", SimpleNamespace(id="faux-recording"))

    async def stream(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.stream_calls.append(
            {
                "system_prompt": kwargs.get("system_prompt") or "",
                "messages": list(messages),
                "tools": kwargs.get("tools"),
            }
        )
        return await self._inner.stream(messages, **kwargs)

    async def generate(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.generate_calls.append({"system_prompt": kwargs.get("system_prompt") or ""})
        return await self._inner.generate(messages, **kwargs)


def _install(monkeypatch, model, *, enabled: bool) -> None:  # noqa: ANN001
    """装模型 + 关掉会多吃一口模型调用的两处（澄清判定 / 历史压缩），并钉住记忆开关。"""
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external")
    )
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    monkeypatch.setattr(settings, "CHAT_COMPRESS_ENABLE", False)
    monkeypatch.setattr(settings, "MEMORY_ENABLE", enabled)


async def _turn(client, *, endpoint: str, question: str, chat_id: int | None = None):  # noqa: ANN001
    """发一轮问，返回归一化的结果（两态对拍只要这几个字段）。"""
    payload: dict = {"message": question}
    if chat_id is not None:
        payload["chat_id"] = chat_id
    resp = await client.post(endpoint, json=payload)
    assert resp.status_code == 200, resp.text

    if endpoint == STREAM:
        frames = parse_sse(resp.text)
        assert frames[-1]["type"] == "done", [frame["type"] for frame in frames]
        return {
            "kind": frames[-1]["kind"],
            "refs": frames[-1]["refs"],
            "answer_source": frames[-1]["answer_source"],
            "text": "".join(frame["text"] for frame in frames if frame["type"] == "token"),
            "chat_id": frames[0]["chat_id"],
            "frames": frames,
        }
    body = resp.json()
    return {
        "kind": body["kind"],
        "refs": body["refs"],
        "answer_source": body["answer_source"],
        "text": body["message"],
        "chat_id": body["chat_id"],
        "frames": None,
    }


def _memory_row(body: str) -> Memory:
    """直接造一行记忆（关态用例不能用 ``remember``：它首行就早退了）。"""
    return Memory(
        scope=memory_mod.SCOPE_GLOBAL,
        kind="interaction",
        text=body,
        text_hash=memory_mod._text_hash(body),
    )


async def _count(session) -> int:  # noqa: ANN001
    return int((await session.execute(select(func.count()).select_from(Memory))).scalar_one())


def _block_text(item) -> str:  # noqa: ANN001
    """取类型化消息的正文（``UserMessage`` / ``AssistantMessage`` 的 content 是块列表）。"""
    return "".join(
        str(getattr(block, "text", "") or "")
        for block in (getattr(item, "content", None) or [])
    )


def _sent_messages(call: dict) -> list[str]:
    return [_block_text(item) for item in call["messages"]]


def _frame_keys(frames: list[dict], kind: str) -> set[str]:
    matched = [frame for frame in frames if frame["type"] == kind]
    assert matched, f"{kind} 帧必须存在"
    return {key for frame in matched for key in frame}


@pytest.fixture
def plan_spy(monkeypatch):
    """给 ``build_turn_plan`` 加一层「把真 plan 抄下来」的壳（端点调的是模块全局名）。"""
    captured: list = []
    original = chat_routes.build_turn_plan

    async def spy(*args, **kwargs):  # noqa: ANN002, ANN003
        plan = await original(*args, **kwargs)
        captured.append(plan)
        return plan

    monkeypatch.setattr(chat_routes, "build_turn_plan", spy)
    return captured


# ==========================================================================
# A4 · 开态一轮 ⇒ 抽取真发生、行真落库
# ==========================================================================


@pytest.mark.parametrize("endpoint", [SYNC, STREAM])
async def test_a4_enabled_round_extracts_and_stores_a_row(
    client, db, fresh_db, monkeypatch, endpoint
):  # noqa: ANN001
    """A4：开记忆跑一轮 ⇒ ``memories`` ≥1 行，字段齐备，``source_chat_id`` == 返回的 chat_id。"""
    memory_mod.reset_failure_counts()
    model = _MemoryAwareModel()
    _install(monkeypatch, model, enabled=True)

    result = await _turn(client, endpoint=endpoint, question="我叫小明，请记住这件事")
    # 唯一等待点：fire-and-forget 的抽取任务在这里跑完（别拿 usage 的 drain 等它）
    await memory_mod.drain_memory_tasks()

    assert len(model.extract_calls) == 1, "两条链路都要各排一次抽取调用"
    assert model.extract_calls[0]["kwargs"]["system_prompt"] == memory_mod.EXTRACT_SYSTEM

    rows = list((await fresh_db.execute(select(Memory).order_by(Memory.id))).scalars().all())
    assert len(rows) == 1
    row = rows[0]
    assert row.scope == memory_mod.SCOPE_GLOBAL == "global"
    assert row.kind == "interaction"
    assert row.text == FACT_TEXT
    assert row.text_hash == memory_mod._text_hash(FACT_TEXT)
    assert row.source_chat_id == result["chat_id"], "来源会话必须是**本轮**这一条"
    assert row.source_message_id is not None, "来源消息 id 由 persist_turn 回填"
    assert row.created_at is not None

    counts = memory_mod.failure_counts()
    assert counts["stored"] == 1 and counts["call_failed"] == 0
    assert counts["parse_failed"] == 0 and counts["empty"] == 0


# ==========================================================================
# A5 · 三类抽取失败都不炸穿
# ==========================================================================

_MODE_ARGS = {
    "raise": dict(facts_error=RuntimeError("模拟抽取调用挂了")),
    "prose": dict(facts="这一轮没什么可记的，我就不输出 JSON 了。"),
    "empty": dict(facts='{"facts": []}'),
}
_MODE_COUNTER = {"raise": "call_failed", "prose": "parse_failed", "empty": "empty"}


@pytest.mark.parametrize("endpoint", [SYNC, STREAM])
@pytest.mark.parametrize("mode", ["raise", "prose", "empty"])
async def test_a5_extraction_failures_never_break_the_turn(
    client, db, fresh_db, monkeypatch, endpoint, mode
):  # noqa: ANN001
    """A5：抽取的三类失败 ⇒ 回答照常、正文与**关态**逐字相同、零新行、对应计数 +1。"""
    question = "帮我看看这件事怎么办"

    # ① 基线：关态同一句问、同一份回答正文
    off_model = _MemoryAwareModel()
    _install(monkeypatch, off_model, enabled=False)
    baseline = await _turn(client, endpoint=endpoint, question=question)
    assert off_model.extract_calls == [], "关态不该有抽取调用"

    # ② 开态 + 抽取失败
    memory_mod.reset_failure_counts()
    bad_model = _MemoryAwareModel(**_MODE_ARGS[mode])
    _install(monkeypatch, bad_model, enabled=True)
    actual = await _turn(client, endpoint=endpoint, question=question)
    await memory_mod.drain_memory_tasks()

    assert len(bad_model.extract_calls) == 1, "抽取确实被调了（不是「没抽所以没红」）"
    assert actual["text"] == baseline["text"], "本轮回答正文必须与关态逐字相同"
    assert actual["kind"] == baseline["kind"]
    assert actual["refs"] == baseline["refs"] == []
    assert actual["answer_source"] == baseline["answer_source"]

    await fresh_db.commit()  # 结束读事务，重新拿一次快照（HTTP 那一侧是别的会话提交的）
    assert await _count(fresh_db) == 0, f"{mode}：失败轮不许留下任何行"

    counts = memory_mod.failure_counts()
    assert counts[_MODE_COUNTER[mode]] == 1, counts
    others = [key for key in ("call_failed", "parse_failed", "empty") if key != _MODE_COUNTER[mode]]
    assert all(counts[key] == 0 for key in others), counts
    assert counts["stored"] == 0, counts


# ==========================================================================
# A6 · 注入不改 SSE 契约
# ==========================================================================


async def test_a6_injection_keeps_sse_contract(client, db, fresh_db, monkeypatch):  # noqa: ANN001
    """A6：同场景开 / 关两态 ⇒ ``done`` 键集相同、``meta``/``token`` 形状相同、三字段相等。"""
    db.add(_memory_row(SEEDED_TEXT))
    await db.commit()

    question = "聊聊我偏好的主题和蓝光"

    on_model = _MemoryAwareModel(facts='{"facts": []}')  # 抽取不再写行，避免影响第二轮
    _install(monkeypatch, on_model, enabled=True)
    on = await _turn(client, endpoint=STREAM, question=question)
    await memory_mod.drain_memory_tasks()

    off_model = _MemoryAwareModel(facts='{"facts": []}')
    _install(monkeypatch, off_model, enabled=False)
    off = await _turn(client, endpoint=STREAM, question=question)

    on_frames, off_frames = on["frames"], off["frames"]
    assert [frame["type"] for frame in on_frames] == [frame["type"] for frame in off_frames]
    assert set(on_frames[-1]) == set(off_frames[-1]), "done 帧键集必须一致（只增字段就会破契约）"
    assert _frame_keys(on_frames, "meta") == _frame_keys(off_frames, "meta")
    assert _frame_keys(on_frames, "token") == _frame_keys(off_frames, "token")
    assert on["refs"] == off["refs"] == []
    assert on["kind"] == off["kind"]
    assert on["answer_source"] == off["answer_source"]
    assert on["text"] == off["text"] == ANSWER_TEXT

    # 两态确实不同：开态真的注入了（否则上面那批「相同」是假绿）
    assert on_model.stream_calls[-1]["system_prompt"] != off_model.stream_calls[-1]["system_prompt"]
    assert memory_mod.MEMORY_HEADER in on_model.stream_calls[-1]["system_prompt"]
    assert SEEDED_TEXT in on_model.stream_calls[-1]["system_prompt"]
    assert memory_mod.MEMORY_HEADER not in off_model.stream_calls[-1]["system_prompt"]


# ==========================================================================
# A7 · 注入进系统提示、不进 history
# ==========================================================================


@pytest.mark.parametrize("endpoint", [SYNC, STREAM])
async def test_a7_memory_block_goes_to_system_prompt_only(
    client, db, fresh_db, monkeypatch, endpoint, plan_spy
):  # noqa: ANN001
    """A7：``plan.history`` 两态**逐字相同**；记忆段在 ``system_prompt`` 里、不在任何消息体里。"""
    db.add(_memory_row(SEEDED_TEXT))
    await db.commit()

    question = "聊聊我偏好的深色主题"

    on_model = _MemoryAwareModel(facts='{"facts": []}')
    _install(monkeypatch, on_model, enabled=True)
    on = await _turn(client, endpoint=endpoint, question=question)
    await memory_mod.drain_memory_tasks()

    off_model = _MemoryAwareModel(facts='{"facts": []}')
    _install(monkeypatch, off_model, enabled=False)
    off = await _turn(client, endpoint=endpoint, question=question)

    on_plan, off_plan = plan_spy[0], plan_spy[1]

    # ① history 形状冻结（只有 role / content 两键）且两态逐字相同——注入**没有**动它
    assert on_plan.history == off_plan.history
    assert all(set(item) == {"role", "content"} for item in on_plan.history)
    assert [item["role"] for item in on_plan.history] == [
        item["role"] for item in off_plan.history
    ]

    # ② 注入段确实渲染在 plan 上（开态非空、关态恒空串）
    assert on_plan.memory_text.startswith(memory_mod.MEMORY_HEADER)
    assert SEEDED_TEXT in on_plan.memory_text
    assert off_plan.memory_text == ""

    # ③ 模型侧：记忆段在**系统提示**里（同步走 answer_direct 的 generate，流式走 stream）
    on_call = on_model.answer_generate_calls[-1] if endpoint == SYNC else on_model.stream_calls[-1]
    off_call = (
        off_model.answer_generate_calls[-1] if endpoint == SYNC else off_model.stream_calls[-1]
    )
    assert memory_mod.MEMORY_HEADER in on_call["system_prompt"]
    assert SEEDED_TEXT in on_call["system_prompt"]
    assert memory_mod.MEMORY_HEADER not in off_call["system_prompt"]

    # ④ 消息体两态逐字相同，且**任何一条**里都没有记忆段（塞进去会顶偏轮次号、污染澄清判定）
    on_sent, off_sent = _sent_messages(on_call), _sent_messages(off_call)
    assert on_sent == off_sent
    assert all(memory_mod.MEMORY_HEADER not in body for body in on_sent)
    assert all(SEEDED_TEXT not in body for body in on_sent)


# ==========================================================================
# A8 · 去重幂等（两轮）
# ==========================================================================


@pytest.mark.parametrize("endpoint", [SYNC, STREAM])
async def test_a8_same_question_twice_still_one_row(
    client, db, fresh_db, monkeypatch, endpoint
):  # noqa: ANN001
    """A8：同一问题两轮 ⇒ 抽取跑了两次、``memories`` 仍只有 1 行、``stored`` 只记 1。"""
    memory_mod.reset_failure_counts()
    model = _MemoryAwareModel()
    _install(monkeypatch, model, enabled=True)
    question = "我叫小明，请记住这件事"

    first = await _turn(client, endpoint=endpoint, question=question)
    await memory_mod.drain_memory_tasks()
    assert await _count(fresh_db) == 1

    second = await _turn(client, endpoint=endpoint, question=question, chat_id=first["chat_id"])
    await memory_mod.drain_memory_tasks()

    assert second["text"] == ANSWER_TEXT, "重复轮的回答照常给"
    assert len(model.extract_calls) == 2, "两轮各抽一次（去重在落库那一步，不靠不抽）"
    await fresh_db.commit()
    assert await _count(fresh_db) == 1, "同一句事实只留一行"
    assert memory_mod.failure_counts()["stored"] == 1


# ==========================================================================
# A10 · 跨会话可见 + 记忆轮零 refs
# ==========================================================================


@pytest.mark.parametrize("endpoint", [SYNC, STREAM])
async def test_a10_memory_crosses_chats_without_refs(
    client, db, fresh_db, monkeypatch, endpoint
):  # noqa: ANN001
    """A10：会话 2 的提问吃到会话 1 抽出的记忆；记忆轮 ``refs == []``、``kind`` 不变。

    两条链路都过一遍：``/chat`` 的 ``refs`` 看响应体，``/chat/stream`` 的看 **``done`` 帧**
    （``refs`` / ``kind`` 是契约九字段里的两个，注入不许把它们搅了）。
    """
    memory_mod.reset_failure_counts()
    model = _MemoryAwareModel()
    _install(monkeypatch, model, enabled=True)

    first = await _turn(client, endpoint=endpoint, question="我叫小明，请记住这件事")
    await memory_mod.drain_memory_tasks()
    await fresh_db.commit()
    assert await _count(fresh_db) == 1
    assert first["refs"] == [], "记忆轮不产生引用"
    if endpoint == STREAM:
        assert first["frames"][-1]["refs"] == [], "done 帧的 refs 同样为空"
        assert first["frames"][-1]["kind"] == first["kind"]

    second_chat = await repo.create_chat(db, title="第二个会话")
    await db.commit()
    assert second_chat.id != first["chat_id"]

    second = await _turn(
        client, endpoint=endpoint, question="我们之前做的记忆验收怎么样？", chat_id=second_chat.id
    )

    assert second["chat_id"] == second_chat.id
    assert second["refs"] == [] and second["kind"] == first["kind"], "kind 不因记忆而改义"
    if endpoint == STREAM:
        assert second["frames"][-1]["refs"] == []
        assert second["frames"][-1]["kind"] == first["kind"]

    answer_call = (
        model.answer_generate_calls[-1] if endpoint == SYNC else model.stream_calls[-1]
    )
    injected = answer_call["system_prompt"]
    assert memory_mod.MEMORY_HEADER in injected, "新会话必须拿到旧会话记下的事实"
    assert FACT_TEXT in injected

    # 反面：会话 2 的 history 是空的（记忆不是靠 history 传过去的）
    assert answer_call["messages"], "至少还有本轮提问"
    assert all(FACT_TEXT not in body for body in _sent_messages(answer_call))


# ==========================================================================
# A11 · 关态不查不写、不注入
# ==========================================================================


@pytest.mark.parametrize("endpoint", [SYNC, STREAM])
async def test_a11_disabled_state_writes_nothing_and_injects_nothing(
    client, db, fresh_db, monkeypatch, endpoint
):  # noqa: ANN001
    """A11：关态（autouse 钉子默认值）跑完整一轮 ⇒ 行数不变、零抽取调用、提示词里没有记忆段。"""
    model = _MemoryAwareModel()
    _install(monkeypatch, model, enabled=False)  # 显式再钉一次：这条测的就是关态
    db.add(_memory_row(SEEDED_TEXT))
    await db.commit()
    await fresh_db.commit()
    assert await _count(fresh_db) == 1, "前提：库里确实有一条可注入的记忆"

    await _turn(client, endpoint=endpoint, question="关态下的一轮提问")
    await memory_mod.drain_memory_tasks()

    await fresh_db.commit()
    assert await _count(fresh_db) == 1, "关态不许写"
    assert model.extract_calls == [], "关态连抽取调用都不该发生"
    assert model.all_prompts, "模型确实被调过（否则这条是假绿）"
    assert all(memory_mod.MEMORY_HEADER not in prompt for prompt in model.all_prompts), (
        "关态注入段恒空串：系统提示里不许出现记忆抬头"
    )
    assert all(SEEDED_TEXT not in prompt for prompt in model.all_prompts)


# ==========================================================================
# A13 · 用量：抽取那条也记一笔（复用 kind="chat" + meta 标记）
# ==========================================================================


@pytest.mark.parametrize("endpoint", [SYNC, STREAM])
async def test_a13_extraction_records_one_usage_row_with_meta(
    client, db, fresh_db, monkeypatch, endpoint, observed_memory_model
):  # noqa: F811
    """A13：抽取发送写一条 ``kind="chat"`` + ``meta.role="memory_extract"``。

    ``meta`` 是**唯一**能把「抽取调用」与「回答调用」分开的字段（``usage/report.py`` 的
    ``by_kind`` 只有固定四类、**不读 meta**）——所以这条断言必须落在 ``meta`` 上，
    只断「chat 桶多了一条」等于没断（回答那条本来就在里面）。
    tokens 三个列拿不到 ⇒ NULL（不是 0）。
    """
    memory_mod.reset_failure_counts()
    model, sends = observed_memory_model
    _install(monkeypatch, model, enabled=True)

    result = await _turn(client, endpoint=endpoint, question="我叫小明，请记住这件事")
    await db.rollback()
    await memory_mod.drain_memory_tasks()
    # 用量走的是 **fire-and-forget**（record_usage），要等它自己的后台任务跑完
    await usage_recorder.drain_pending()

    await fresh_db.commit()
    rows = (
        await fresh_db.execute(select(UsageEvent).order_by(UsageEvent.id))
    ).scalars().all()
    assert len(sends) == len(rows) == 2, "正常回答一次 + 抽取一次，每个 HTTP send 记一行"
    assert len(model.extract_calls) == 1
    assert model.extract_calls[0]["system_prompt"] == memory_mod.EXTRACT_SYSTEM
    assert result["text"] == ANSWER_TEXT
    assert all(row.kind == "chat" and row.calls == 1 for row in rows)
    assert all((row.meta or {}).get("usage_received") is False for row in rows)
    assert all(
        (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (None, None, None)
        for row in rows
    )
    assert sum((row.meta or {}).get("role") == "main_answer" for row in rows) == 1

    memory_rows = [
        row for row in rows if (row.meta or {}).get("role") == "memory_extract"
    ]
    assert len(memory_rows) == 1, (
        f"抽取应当**恰好**记一条用量行，实际 {len(memory_rows)} 条；全部行的 meta："
        f"{[r.meta for r in rows]}"
    )
    row = memory_rows[0]
    assert row.kind == "chat", "复用 chat（不新增 usage kind —— 四值封闭）"
    assert row.calls == 1
    assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (None, None, None), (
        "拿不到 tokens 就写 NULL（绝不用字数估算）"
    )
    # Send accounting is independent of whether extraction adds a new fact.
    assert memory_mod.failure_counts()["stored"] == 1


# ==========================================================================
# A19 · Agent 轮：收尾回答带记忆段，决策提示不带
# ==========================================================================


def _faux_chain(replies: list[str]):  # noqa: ANN201
    """Faux 响应队列：Agent 决策先吃一格，回答链路再吃一格。"""
    from cubeloop.providers import FauxProvider, faux_assistant_message

    provider = FauxProvider(provider_id="faux")
    provider.set_responses([faux_assistant_message(text) for text in replies])
    return provider.model("faux-chat")


async def test_a19_agent_round_answer_prompt_has_memory_block(client, db, monkeypatch):  # noqa: ANN001
    """A19：``agent_mode="react"`` ⇒ **收尾回答**的提示词带记忆段；CubeLoop 决策提示不带。

    这条走**真**的 Agent 轮（``POST /api/chat/stream`` + 真 CubeLoop + 假模型），
    所以「决策提示不带记忆」是实测出来的，不是读注释读出来的；
    外加一条静态面：``app/agent/runner.py`` 里根本没有记忆模块的进口
    （结构上不可能给决策提示塞记忆段）。
    """
    memory_mod.reset_failure_counts()
    db.add(_memory_row(SEEDED_TEXT))
    await db.commit()

    inner = _faux_chain(["先想一下该怎么答。", "这是最终回答。", "兜底回答。"])
    model = _RecordingModel(inner)
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    monkeypatch.setattr(settings, "CHAT_COMPRESS_ENABLE", False)

    resp = await client.post(
        STREAM, json={"message": "聊聊我的主题和蓝光偏好", "agent_mode": "react"}
    )
    assert resp.status_code == 200, resp.text
    frames = parse_sse(resp.text)
    assert frames[-1]["type"] == "done", [frame["type"] for frame in frames]
    await memory_mod.drain_memory_tasks()

    decision_prompts = [
        call["system_prompt"]
        for call in model.stream_calls
        # ⚠️ 身份识别从「**相等**」改为「**前缀**」（R2-08 · 渐进式工具披露接线，2026-09-23）：
        # 接线后决策调用的 system prompt = ``DECISION_SYSTEM_PROMPT`` + 折叠组目录
        # （``cubeloop`` 的 ``DeferredToolsMiddleware.transform_system_prompt``，dispatch 策略下
        # **整轮字节稳定**）⇒「相等」不再成立。**本条要守的断言（决策提示不带记忆段）一字未改**；
        # 过匹配由紧随其后的 ``answer_prompt not in decision_prompts`` 反向钉住
        # （回答链路的系统提示不是这个前缀）。
        if call["system_prompt"].startswith(agent_runner.DECISION_SYSTEM_PROMPT)
    ]
    assert decision_prompts, "决策循环的提示词必须真的到过模型（否则这条是假绿）"
    assert all(memory_mod.MEMORY_HEADER not in prompt for prompt in decision_prompts), (
        "CubeLoop 的决策/计划提示不许带记忆段（这是本条唯一的有意差异）"
    )
    assert isinstance(agent_runner.DECISION_SYSTEM_PROMPT, str)
    assert agent_runner.DECISION_SYSTEM_PROMPT.strip(), "决策提示不是空串（不是空对空对上号）"

    answer_prompt = model.stream_calls[-1]["system_prompt"]
    assert answer_prompt not in decision_prompts, "最后一格是回答链路，不是决策"
    assert memory_mod.MEMORY_HEADER in answer_prompt, "Agent 轮的**收尾回答**也要带记忆段"
    assert SEEDED_TEXT in answer_prompt

    # 静态面：runner 整份源码里连「memory」这个词都不出现（含 import 与注释）——结构性证据，
    # 挡住「顺手给决策提示也塞一段记忆」这种改法。
    source = pathlib.Path(agent_runner.__file__).read_text(encoding="utf-8")
    assert "memory" not in source.lower()
    assert memory_mod.MEMORY_HEADER not in agent_runner.DECISION_SYSTEM_PROMPT


# ==========================================================================
# 收口：停止轮不抽
# ==========================================================================


async def test_stopped_round_does_not_extract(client, db, fresh_db, monkeypatch):  # noqa: ANN001
    """停止轮（``kind="stopped"``）⇒ 零新行、零抽取调用——回答不完整，抽出来是半句。

    走生产路径：直接拿 ``chat_stream`` 的响应体逐帧消费，看到 ``meta`` 就发 ``/api/chat/stop``
    （与 ``tests/api/test_rerank_provenance_029.py`` 的停止用例同型）。
    """
    memory_mod.reset_failure_counts()
    model = _MemoryAwareModel(chunks=["已交付的前半句", "不该被抽到", "也不该"])
    _install(monkeypatch, model, enabled=True)

    request_id = "memory-stop-1"
    response = await chat_routes.chat_stream(
        ChatRequest(message="这一轮会被停止", request_id=request_id, agent_mode="off"), db
    )
    frames: list[dict] = []
    async for data in response.body_iterator:
        parsed = parse_sse(data.decode() if isinstance(data, bytes) else data)
        if not parsed:
            continue
        frame = parsed[0]
        frames.append(frame)
        if frame["type"] == "meta":
            stopped = await client.post("/api/chat/stop", json={"request_id": request_id})
            assert stopped.json() == {"stopped": True}

    assert frames[-1]["type"] == "stopped", [frame["type"] for frame in frames]
    assert "done" not in [frame["type"] for frame in frames], "停止轮不产 done 帧"

    await memory_mod.drain_memory_tasks()
    await fresh_db.commit()
    assert await _count(fresh_db) == 0, "停止轮不许抽（回答不完整）"
    assert model.extract_calls == [], "停止轮压根不该排抽取任务"
    assert memory_mod.failure_counts()["stored"] == 0
