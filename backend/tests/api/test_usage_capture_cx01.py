"""专项 · 上下文管理升级 · **CX-01′「真实 usage 捕获」**的验收钉子（C1–C5 + M1）。

## 本条要钉的是什么（**纯观测**，三面都不许漂）

把厂商回执里的**真 token 读数**读出来并落日志。四条「不许动」是硬约束，所以本文件的
断言写成**两侧**：① 真值确实被读到（C1）；② 帧键集（C2）、请求体（C3）、记账面（C4）
**一个字节都不许变**。

## 为什么必须走「真装配 + 假传输」

`tests/api/t0_wire.py` 的 `attach_wire` 把 SDK 的 httpx 客户端换成 `MockTransport`
（真 `OpenAIProvider`、零出网）。这条假上游**天然带 usage**：

- 依赖自己注入 `stream_options.include_usage=True`（`cubeloop/providers/openai.py:139-147`）
  ⇒ 厂商会多回一个 **usage-only chunk**（无 `choices`）；
- 适配器把它写进 `partial.usage`（`openai.py:245-265`），**不是**事件 —— `StreamEvent(type="done")`
  不带 `partial`（`openai.py:520`）⇒ 旧的消费循环只取 `delta`，这份读数**必被丢掉**；
- 正解是循环结束后 `await stream.result()`（`cubeloop/providers/base.py:364-390`）。

⚠️ `wire_sse_text()`（`t0_wire.py:95-101`）**不带** usage ⇒ 它的形状本身就是 C5 的原料，
本文件只需要再补一个 usage-only chunk 就得到 C1 的形状。

## 真值从哪来（C1 的判据不是「等于某个常量」）

`prompt_tokens` **不是**本地估的：断言同时钉
① 假上游给的原始回执（`PROMPT_TOKENS=120` / `CACHED_TOKENS=40` / `COMPLETION_TOKENS=7`）、
② 归一化口径（`input_tokens = prompt − cached = 80`，`openai.py:257-265`）、
③ **同一份回执的机器可读副本**——从假上游**真正发出去的那段回执正文**里把 `usage` 读回来
（:class:`_ReceiptRecordingWire` 记的 `receipts[0]`）。
两侧同源 ⇒ 「真值来自回执、不是本地估算」这句话是可证的，而不是靠常量对拍。

⚠️ **③ 不能读 `wire.request_bodies`**（实测踩过）：那是**请求**体，`usage` 只出现在**回执**里，
且 C3 专门钉住请求体里不许有 `usage` 键 ⇒ 读请求体永远得到 `{}`，C1 会在这一格**假红**
（`None == 120`）。回执副本必须由假传输自己留（`Wire` 只记请求体）。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.generation import answer as answer_mod
from app.models import UsageEvent
from tests.api.t0_wire import (  # noqa: F401 —— client / wire_env 是夹具
    Wire,
    attach_wire,
    client,
    run_stream,
    wire_env,
    wire_sse_text,
)
from tests.api.test_chat_contract import parse_sse

pytestmark = pytest.mark.asyncio

#: 普通长度的问题（避开短句澄清链：`clarify.MIN_LENGTH`）。
QUESTION = "帮我看一下资料"
#: 假上游回的正文（**不再分段**：C2 要钉的就是 `meta → token* → done`）。
ANSWER = "我看了一眼，没问题。"

#: 假上游**原始回执**里的三个读数（`prompt_tokens` 含缓存命中）。
PROMPT_TOKENS = 120
CACHED_TOKENS = 40
COMPLETION_TOKENS = 7
#: 适配器归一化之后 `Usage.input_tokens` 的值（= prompt − cached，见模块 docstring）。
NORMALIZED_INPUT_TOKENS = PROMPT_TOKENS - CACHED_TOKENS


def _sse_usage_chunk() -> str:
    """OpenAI 兼容的 **usage-only chunk**（无 ``choices``）—— 真厂商形状。

    ⚠️ 这是**回执**，不是请求：本文件**不**参与 ``stream_options`` 的注入
    （那是依赖自己做的，``openai.py:139-147``），这里只负责扮演「厂商回了什么」。
    """
    payload = {
        "id": "chatcmpl-wire",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mimo-v2.5",
        "choices": [],
        "usage": {
            "prompt_tokens": PROMPT_TOKENS,
            "completion_tokens": COMPLETION_TOKENS,
            "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
            "prompt_tokens_details": {"cached_tokens": CACHED_TOKENS},
        },
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


#: SSE 的流终止标记（``wire_sse_text()`` 的结尾）。usage-only chunk 必须排在它**之前**。
_SSE_DONE = "data: [DONE]\n\n"


def wire_sse_text_with_usage(text: str) -> str:
    """正文 + ``finish_reason`` + **usage-only chunk** + ``[DONE]``（真厂商的收尾序）。

    ⚠️ **顺序本身就是判据的一半**（实测踩到）：``usage`` 必须排在 ``[DONE]`` **之前**。
    OpenAI SDK 读到 ``data: [DONE]`` 就 ``break``（``openai/_streaming.py``），排在它**后面**
    的字节根本不会变成 chunk ⇒ 适配器不写 ``partial.usage``（``cubeloop/providers/openai.py:245-265``
    那一格不进），消费方拿到的是 ``AssistantMessage`` 的**构造默认** ``Usage()``（同文件
    ``:196-202``）＝ 全 0 —— 看着像「厂商回了 0」，其实是**这份 wire 自己把回执丢了**。
    实测对照（同一段正文，仅调换这两段的先后）：

    - usage 在 ``[DONE]`` 后 ⇒ ``Usage(input_tokens=0, output_tokens=0, cache_read_tokens=0)``；
    - usage 在 ``[DONE]`` 前 ⇒ ``Usage(input_tokens=80, output_tokens=7, cache_read_tokens=40)``。
    """
    body = wire_sse_text(text)
    assert body.endswith(_SSE_DONE), body[-len(_SSE_DONE) :]
    return body[: -len(_SSE_DONE)] + _sse_usage_chunk() + _SSE_DONE


class _ReceiptRecordingWire(Wire):
    """``Wire`` + **记下真正发出去的回执正文**（``Wire`` 只记请求体，回执副本要自己留）。

    ⚠️ 为什么必须自己记：`usage` 只出现在**回执**里，而 ``Wire.bodies`` 是**请求**体
    （C3 还钉住请求体里不许出现 `usage` 键）⇒ 把 ``request_bodies[0]["usage"]`` 当
    「回执的机器可读副本」必然读到 `{}`（C1 原先就是在这里假红的）。
    """

    def __init__(self, *responses: str) -> None:
        super().__init__(*responses)
        self.receipts: list[str] = []

    def handler(self, request):  # noqa: ANN001 —— 与 ``Wire.handler`` 同签名
        response = super().handler(request)
        self.receipts.append(response.content.decode("utf-8"))
        return response


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _capture_usage(monkeypatch) -> list[tuple[str, object]]:
    """截住 ``answer_mod.note_usage`` 的**入参原文**（两侧都要断言，不只断言「被调过」）。

    用 spy 而不是读日志：判据是「读到的那三个读数与假上游给的一致」，
    而 `ret` 里正是那三个读数（生产函数自己的返回契约）。
    """
    calls: list[tuple[str, object]] = []
    real = answer_mod.note_usage

    def spy(stage, usage=None):  # noqa: ANN001
        calls.append((stage, usage))
        return real(stage, usage)

    monkeypatch.setattr(answer_mod, "note_usage", spy)
    return calls


def _receipt_usage(wire) -> dict:
    """从假上游**真正发出去的那段回执正文**里把 ``usage`` 读回来（③ 的机器可读副本）。

    ⚠️ 读 ``receipts`` 而**不是** ``request_bodies``：后者是**请求**体，`usage` 只在**回执**里
    （C3 钉住请求体里不许出现 `usage` 键）⇒ 读请求体永远得到 `{}`，断言变假红。
    """
    assert len(wire.receipts) == 1, f"假上游应当只回过一段回执：{len(wire.receipts)}"
    for line in wire.receipts[0].splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :].strip()
        if payload == "[DONE]":
            continue
        body = json.loads(payload)
        if body.get("usage"):
            return body["usage"]
    return {}


def _mutate_usage_capture_to_none(monkeypatch) -> None:
    """**变异自证**（M1）：就地把捕获换成恒 ``None`` ⇒ C1 必须真红。

    与既有写法同型（`test_agent_side_effects_t1.py:334-368`）：只替**观测点本身**
    （等价于「新代码没接上」），一行生产代码都不动。
    """
    monkeypatch.setattr(answer_mod, "note_usage", lambda stage, usage=None: None)


#: `done` 帧的十字段（🔴 2026-09-27：`回归修复` 拍板⑨「补」新增第 10 键 `recovered`，02 v1.0.12）。
DONE_FIELDS = {
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
#: `meta` 帧的三字段（`chat_routes.py` 唯一一处 `yield sse({"type": "meta", …})`）。
META_FIELDS = {"type", "chat_id", "skill_ids"}


def _done(events: list[dict]) -> dict:
    done = [event for event in events if event.get("type") == "done"]
    assert len(done) == 1, events
    return done[0]


def _answer_text(events: list[dict]) -> str:
    return "".join(event.get("text", "") for event in events if event.get("type") == "token")


class _RecordingBoundModel:
    """假 bound model：只实现 ``generate()``（**同步链路**那条腿），可注入 ``usage``。"""

    def __init__(self, reply: str = "好", usage=None) -> None:
        self.reply = reply
        self.usage = usage
        self.generates = 0

    async def generate(self, messages, *, system_prompt="", **_kwargs):  # noqa: ANN001
        self.generates += 1
        from cubeloop.providers import faux_assistant_message

        message = faux_assistant_message(self.reply)
        return message.model_copy(update={"usage": self.usage})

    async def stream(self, messages, *, system_prompt="", **_kwargs):  # noqa: ANN001
        raise AssertionError("同步入口不该走 stream()")


async def _run_one_shot(client, wire, monkeypatch, *, text: str = ANSWER) -> list[dict]:
    """装 wire、跑一轮 ``/api/chat/stream``（普通模式，无 Agent、无续问）。"""
    attach_wire(wire, monkeypatch)
    return await run_stream(client, message=QUESTION, agent_mode="off")


def _assert_real_usage_reached_the_observer(calls, wire) -> dict:
    """C1 的核心判据（C1 与 M1 共用；M1 靠它变红）。返回断言过的 ledger。"""
    assert calls, "note_usage 一次都没被调用（流式收尾的读数没接上）"
    stages = [stage for stage, _usage in calls]
    assert "stream" in stages, f"流式那一腿没报读数：stages={stages}"

    stage, usage = next((item for item in calls if item[0] == "stream"))
    ledger = answer_mod.note_usage(stage, usage)
    assert ledger is not None, "捕获上来的 usage 读不出读数（Observation 断了）"

    # ① 归一化口径（`openai.py:257-265` 的三行契约）
    assert ledger["input_tokens"] == NORMALIZED_INPUT_TOKENS, ledger
    assert ledger["cache_read_tokens"] == CACHED_TOKENS, ledger
    assert ledger["output_tokens"] == COMPLETION_TOKENS, ledger
    # ② prompt = 非缓存输入 + 缓存命中（adapter 归一化后的还原口径）
    assert ledger["prompt_tokens"] == PROMPT_TOKENS, ledger
    assert ledger["prompt_tokens"] == ledger["input_tokens"] + ledger["cache_read_tokens"]

    # ③ 同一份回执里也读得到（真值来自回执，不是本地估算）
    raw = _receipt_usage(wire)
    assert raw.get("prompt_tokens") == ledger["prompt_tokens"], (
        f"读数与假上游回执不一致：回执={raw} / 捕获={ledger}"
    )
    assert raw.get("completion_tokens") == ledger["output_tokens"], (raw, ledger)
    assert (raw.get("prompt_tokens_details") or {}).get("cached_tokens") == ledger[
        "cache_read_tokens"
    ], (raw, ledger)
    return ledger


# --------------------------------------------------------------------------
# C1 · 流式一轮：真值被读到，且与假上游一致
# --------------------------------------------------------------------------


async def test_c1_stream_round_reads_the_real_usage_from_the_receipt(
    client, wire_env, monkeypatch, caplog
):
    """C1：``note_usage("stream", …)`` 真被调，三个读数与假上游回执**逐项一致**。

    判据两侧都钉：① 观测点的入参（:func:`_assert_real_usage_reached_the_observer`）；
    ② **INFO 日志真的打了那一行**（含真值口径的 `prompt`，不是估算）——这是本条目
    「读出来并落日志」的交付面。
    """
    calls = _capture_usage(monkeypatch)
    wire = _ReceiptRecordingWire(wire_sse_text_with_usage(ANSWER))

    with caplog.at_level("INFO", logger="app.generation.answer"):
        events = await _run_one_shot(client, wire, monkeypatch)

    assert _answer_text(events) == ANSWER, events
    assert wire.queue == [], "响应队列没吃空（多调了一次上游 / 少回了一段）"
    _assert_real_usage_reached_the_observer(calls, wire)

    messages = [record.getMessage() for record in caplog.records]
    hit = [text for text in messages if "真实用量" in text and "stream" in text]
    assert hit, f"没有打出真实用量那一行 INFO：{messages}"
    assert f"prompt {PROMPT_TOKENS}" in hit[0], hit[0]
    assert f"输入 {NORMALIZED_INPUT_TOKENS}" in hit[0], hit[0]
    assert f"缓存命中 {CACHED_TOKENS}" in hit[0], hit[0]
    assert f"输出 {COMPLETION_TOKENS}" in hit[0], hit[0]


# --------------------------------------------------------------------------
# C2 · 帧键集逐字不变
# --------------------------------------------------------------------------


async def test_c2_frame_key_set_is_unchanged(client, wire_env, monkeypatch):
    """C2：帧 `type` 序列**逐字**是 `meta → token* → done`（不增帧、不改名）。

    ⚠️ 这一条与「取回执」是**同一轮的两次断言**：读 usage 走的是**流外**的
    `stream.result()`，它**不得**往 SSE 里塞任何东西（比如一个 `usage` 帧）。
    另按既有写法（`test_chat_contract.py:298-310` / `:325-346`）钉住
    `meta` / `done` 两侧的**顶层键集**，防「顺手加个 usage 键」。
    """
    wire = _ReceiptRecordingWire(wire_sse_text_with_usage(ANSWER))
    events = await _run_one_shot(client, wire, monkeypatch)

    types = [event["type"] for event in events]
    assert types == ["meta", "token", "done"], types
    assert _done(events)["type"] == "done"

    meta = events[0]
    assert meta["type"] == "meta"
    assert set(meta) == META_FIELDS, set(meta)

    done = _done(events)
    assert set(done) == DONE_FIELDS, f"多 {set(done) - DONE_FIELDS} / 少 {DONE_FIELDS - set(done)}"


# --------------------------------------------------------------------------
# C3 · 请求体逐字不变
# --------------------------------------------------------------------------


async def test_c3_request_body_keys_are_unchanged(client, wire_env, monkeypatch):
    """C3：发往上游的请求体**键集逐字不变**（尤其 `stream_options` 不许被本改动碰）。

    ⚠️ 口径（照 `test_tool_context_t0.py:468-482` 的「空通道 ⇒ 逐字节相同」）：本用例
    断的是**键集 + 依赖注入痕迹**，不是「与上一版对拍」（那需要基线请求体快照）。
    两件事实一起钉住：
    ① `stream_options.include_usage` 在，且是**依赖自动注入**的（`openai.py:139-147`）；
    ② 本轮请求体里**没有** `usage` 这个键 —— 捕获走的是**回执**，不是请求。
    """
    wire = _ReceiptRecordingWire(wire_sse_text_with_usage(ANSWER))
    await _run_one_shot(client, wire, monkeypatch)

    bodies = wire.request_bodies
    assert len(bodies) == 1, f"本轮只该有一次回答调用：{wire.call_kinds()}"
    body = bodies[0]

    # ① 依赖注入的那两个键**逐字**长相不变（本改动绝不碰 `stream_options`）
    assert body["stream"] is True, body["stream"]
    assert body["stream_options"] == {"include_usage": True}, body["stream_options"]
    # ② 捕获走的是**回执**：请求体里不许出现任何 `usage` 键（含大写变体）
    assert not [key for key in body if "usage" in key.lower()], sorted(body)
    # ③ 既有键一个不少（键集漂移 = 改请求体）
    assert {"model", "messages", "stream", "stream_options"} <= set(body), sorted(body)
    assert body["model"] == "mimo-v2.5", body["model"]
    assert body["messages"], body


# --------------------------------------------------------------------------
# C4 · 记账面未动（usage_events 行数 + tokens 恒 NULL）
# --------------------------------------------------------------------------


async def test_c4_usage_events_row_count_and_null_tokens_are_unchanged(
    client, db, wire_env, monkeypatch
):
    """OPT5 C4：一笔实际 send 对应一行，token 三列必须来自本轮原始厂商回执。"""
    wire = _ReceiptRecordingWire(wire_sse_text_with_usage(ANSWER))
    events = await _run_one_shot(client, wire, monkeypatch)
    assert _answer_text(events) == ANSWER, events

    from app.usage.recorder import drain_pending
    await drain_pending()
    await db.rollback()
    rows = list((await db.execute(select(UsageEvent))).scalars().all())
    assert len(rows) == 1, f"一次成功回答应当只落一行用法：{[r.kind for r in rows]}"
    row = rows[0]
    assert row.kind == "chat"
    assert row.prompt_tokens == PROMPT_TOKENS
    assert row.completion_tokens == COMPLETION_TOKENS
    assert row.total_tokens == PROMPT_TOKENS + COMPLETION_TOKENS
    assert row.meta["role"] == "main_answer"
    assert row.meta["usage_received"] is True


# --------------------------------------------------------------------------
# C5 · 取不到 usage ⇒ 本轮照常产出正文（容错）
# --------------------------------------------------------------------------


async def test_c5_missing_usage_does_not_break_the_round(client, wire_env, monkeypatch, caplog):
    """C5：假上游不回 usage chunk ⇒ 正文照常产出、不报错，捕获侧读数全 0。

    这条钉的是**容错**：哨兵坏掉 / 厂商不回 usage 都**不该**影响任何一轮问答。

    ⚠️ 口径（实测，别照抄旧说法）：没有 usage-only chunk 时，捕获侧拿到的**不是** ``None``
    而是**全 0 的 ``Usage()``** —— 适配器构造 ``partial`` 时就写了 ``usage=Usage()``
    （``cubeloop/providers/openai.py:196-202``），回执里没有 usage 块就没人覆盖它。
    所以这一格钉的是「全 0 也照常出正文」，而**不是**「0 是厂商给的 0」。
    """
    calls = _capture_usage(monkeypatch)
    wire = _ReceiptRecordingWire(wire_sse_text(ANSWER))  # ← 不带 usage-only chunk 的形状

    with caplog.at_level("DEBUG", logger="app.generation.answer"):
        events = await _run_one_shot(client, wire, monkeypatch)

    assert _answer_text(events) == ANSWER, events
    assert _done(events)["type"] == "done"
    assert [event["type"] for event in events] == ["meta", "token", "done"]

    # ⚠️ 迭代的是 **``calls`` 的快照** —— 此刻 ``answer_mod.note_usage`` 正是
    # :func:`_capture_usage` 装上的 spy，它每被调用一次就往 ``calls`` 里追加一项；
    # 直接 ``for stage, usage in calls`` 就是**自增长死循环**（本轮 300s 挂死的挂点，
    # faulthandler 栈：本行 → spy（:138-152）→ ``answer_mod.note_usage``）。
    for stage, usage in list(calls):
        ledger = answer_mod.note_usage(stage, usage)
        if ledger is not None:
            assert ledger["input_tokens"] == 0, ledger
            assert ledger["cache_read_tokens"] == 0, ledger
            assert ledger["prompt_tokens"] == 0, ledger
    assert wire.queue == []


# --------------------------------------------------------------------------
# M1 · 变异自证：捕获恒 None ⇒ C1 必红
# --------------------------------------------------------------------------


async def test_m1_mutation_capture_is_none_makes_c1_red(client, wire_env, monkeypatch):
    """M1：就地打开坏实现（捕获恒 ``None``）⇒ C1 的断言**真的会红**。

    ⚠️ 断的正是 C1 的那条：`calls` 非空且能读出 120/80/40/7。坏实现下 `calls == []`
    ⇒ `_assert_real_usage_reached_the_observer` 的第一行就炸 ⇒ 本变异有鉴别力。
    """
    calls = _capture_usage(monkeypatch)
    _mutate_usage_capture_to_none(monkeypatch)
    wire = _ReceiptRecordingWire(wire_sse_text_with_usage(ANSWER))

    events = await _run_one_shot(client, wire, monkeypatch)

    # 本轮照常走完（坏实现只影响观测面，不影响产品链路）——这也顺带证明 C1 的
    # 断言**不是**靠「本轮炸了 / 没炸」判出来的。
    assert _answer_text(events) == ANSWER, events
    assert calls == [], "坏实现（捕获恒 None）必须真的吞掉读数"
    with pytest.raises(AssertionError, match="note_usage 一次都没被调用"):
        _assert_real_usage_reached_the_observer(calls, wire)


# --------------------------------------------------------------------------
# C6 · 同步链路（同一套读法的第二条腿）
# --------------------------------------------------------------------------


async def test_c6_sync_round_reads_the_real_usage(monkeypatch):
    """C6（同步链路）：`answer_direct` 读 `bound.generate()` 返回值上的 `.usage`。

    事实面（为什么这条腿该存在）：`BoundModel.generate` →
    `BaseProvider.generate` 就是 `stream() + await stream.result()`
    （`cubeloop/providers/base.py:766-806`）⇒ 返回值**就是**那份带 `.usage` 的
    `AssistantMessage`。这里用假 bound model 钉住「读法接在返回值的 ``.usage`` 上」。
    """
    from cubeloop.providers import Usage

    calls = _capture_usage(monkeypatch)
    model = _RecordingBoundModel(usage=Usage(input_tokens=80, output_tokens=7, cache_read_tokens=40))

    result = await answer_mod.answer_direct(
        QUESTION, model=model, source_type="external", has_material=False, direct_enabled=True
    )
    assert result.answer == "好"
    assert model.generates == 1

    stages = [stage for stage, _usage in calls]
    assert "sync" in stages, f"同步那一腿没报读数：{stages}"
    ledger = answer_mod.note_usage(*next(item for item in calls if item[0] == "sync"))
    assert ledger == {
        "stage": "sync",
        "input_tokens": 80,
        "cache_read_tokens": 40,
        "output_tokens": 7,
        "prompt_tokens": 120,
    }, ledger


async def test_c6_sync_round_without_usage_does_not_break(monkeypatch):
    """C6 附（容错）：返回值上没有 `.usage` ⇒ 同步链路照常出正文，观测侧空手而归。

    ⚠️ 期望是 `[("sync", None)]`（**调用发生**）而不是 `[("sync", 0)]`（**假装读到 0**）：
    `note_usage(None)` 返回 `None` 且**不发日志** —— 「厂商没给」是常态，不是一条读数。
    """
    calls = _capture_usage(monkeypatch)
    model = _RecordingBoundModel(usage=None)

    result = await answer_mod.answer_direct(
        QUESTION, model=model, source_type="external", has_material=False, direct_enabled=True
    )
    assert result.answer == "好"
    assert calls == [("sync", None)], calls


# --------------------------------------------------------------------------
# C7 · `note_usage` 本体：纯观测、绝不抛、绝不改输入
# --------------------------------------------------------------------------


def test_c7_note_usage_is_total_on_broken_input():
    """C7：`note_usage(None)` / 缺字段 / 非 Usage 对象 / 坏点字段 ⇒ 返回 None 或读数，**绝不抛**。"""
    from cubeloop.providers import Usage

    assert answer_mod.note_usage("stream", None) is None
    assert answer_mod.note_usage("stream", object()) is None
    # 缺字段 / SDK 默认零值不等于厂商给了完整零回执。
    partial = Usage(input_tokens=5)
    assert answer_mod.note_usage("stream", partial) is None
    # 字段不可转 int ⇒ 整条读数放弃（返回 None），不炸穿
    assert answer_mod.note_usage("stream", _Uncoercible()) is None
    # 纯观测：入参对象不被改（dataclass frozen 也一并覆盖）
    usage = Usage(input_tokens=80, output_tokens=7, cache_read_tokens=40)
    before = usage.model_dump()
    answer_mod.note_usage("stream", usage)
    assert usage.model_dump() == before


class _Uncoercible:
    """字段是坏对象（``int()`` 会抛）的替身 —— C7 的「坏点字段」那一格。"""

    input_tokens = object()
    cache_read_tokens = 0
    output_tokens = 0
