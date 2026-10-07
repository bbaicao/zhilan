"""条目 4.4 · 会话详情端到端 + ``attempt_id`` 两条链路（HTTP 层）。

**为什么这层必须在 HTTP 上测**：本条的验收面是「**界面上那次未知的工具结果有没有如实说出来**」，
而 `recovered` 的唯一出口就是 ``GET /api/chats/{chat_id}``（SSE 那条路是堵死的：冻结前端组装
``done`` 时不接 ``ev.recovered``）。所以 D2 把该端点的**只读实现**从 4.5.1 前移到 4.4——
没有它，本条的交货在界面上不可见、无法自证。

| 断言（指令包 §4 编号） | 覆盖点 |
|---|---|
| 8 | ``GET /api/chats/{id}`` 返回 ``messages[].recovered``，**刷新后仍在** |
| 9 | ``MessageOut`` 字段表与契约 ``02:244`` 逐字对齐 |
| 10 | ``done`` 帧**不含** ``recovered``（有意：前端不接） |
| 11 | 成功轮 ``assistant/attempt`` ↔ ``assistant/message`` 同值；停止轮 ↔ ``stream/interrupted`` 同值 |
| 12 | ``stream/interrupted`` 载荷四键齐 + 哈希形状（停止轮的真链路） |
| 13b | ``attempt_id`` 落进 ``session_events.attempt_id`` 列；同步 ``/chat`` 与流式 ``/chat/stream`` **都铸** |
| 17 | **I5 收尾恰好一次**：每条链路恰好一个终态帧（``done`` / ``stopped`` / ``error``） |
| 21 | ``ChatStopIn.request_id`` 上限 64（65 字 ⇒ 422） |
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import chat_routes
from app.models import Chat, Message, SessionEvent
from app.session import attempt as attempt_mod
from app.session import repository as repo
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401


#: 问题不能太短：``clarify.judge_query`` 会把「问题」判 vague 并多调一次模型
QUESTION = "蓝鲸是什么"


# --------------------------------------------------------------------------
# 端到端：GET /api/chats/{id} + recovered
# --------------------------------------------------------------------------


async def _crashed_chat(db) -> tuple[Chat, Message, Message]:
    """造一个「上次崩在工具执行中」的会话：assistant 声明要用工具、日志里却没有 ``tool/call``。

    这正是老基线 ``recover.py`` 的情形 B（也是真机唯一可达的场景——assistant 消息在工具执行中
    **还没落库**时，结论只能退化为挂在那条消息上）。
    """
    chat = await repo.create_chat(db, title="崩溃会话")
    user = await repo.add_message(db, chat_id=chat.id, role="user", content=QUESTION)
    assistant = await repo.add_message(
        db,
        chat_id=chat.id,
        role="assistant",
        content="我查一下",
        agent_trace={
            "mode": "react",
            "steps": [
                {
                    "kind": "tool",
                    "title": "检索：蓝鲸",
                    "tool": "knowledge_search",
                    "tool_call_id": "call-crash-1",
                }
            ],
            "events_incomplete": False,
        },
    )
    await db.commit()
    return chat, user, assistant


@pytest.mark.asyncio
async def test_chat_detail_reports_recovered_and_survives_a_refresh(client, db, fresh_db):  # noqa: F811
    """断言 8：``GET /api/chats/{id}`` 的 ``messages[].recovered`` 为真，且**换一个连接再拉仍然为真**。"""
    chat, user, assistant = await _crashed_chat(db)

    first = await client.get(f"/api/chats/{chat.id}")
    assert first.status_code == 200, first.text
    body = first.json()
    by_id = {item["id"]: item for item in body["messages"]}
    assert by_id[int(assistant.id)]["recovered"] is True, "崩溃那一轮必须被标出来"
    assert by_id[int(user.id)]["recovered"] is False, "用户消息不受牵连（缺省 false）"

    # 「刷新后仍在」：新连接 + 重新拉一遍（现算，不是靠内存里的一次性标志）
    again = await client.get(f"/api/chats/{chat.id}")
    assert again.status_code == 200
    rows = {item["id"]: item for item in again.json()["messages"]}
    assert rows[int(assistant.id)]["recovered"] is True

    # 事件真的落库了（提交过），而且**幂等**：只合成过一次
    events = list(
        (
            await fresh_db.execute(
                select(SessionEvent).where(SessionEvent.chat_id == chat.id)
            )
        )
        .scalars()
        .all()
    )
    synthesized = [row for row in events if row.type == "recovery/synthesized"]
    assert len(synthesized) == 1, [row.type for row in events]
    assert synthesized[0].data["message_id"] == int(assistant.id)

    # 详情里的两个压缩字段保持 Chat 上的原值（NULL ⇒ None）
    assert body["context_summary"] is None
    assert body["compressed_up_to"] is None
    assert body["id"] == chat.id and body["title"] == chat.title


@pytest.mark.asyncio
async def test_chat_detail_keeps_clean_chats_clean(client, db):  # noqa: F811
    """干净会话（配对完整）⇒ 两条消息都是 ``recovered: false``，一条恢复事件都不写。"""
    chat = await repo.create_chat(db, title="干净会话")
    await repo.add_message(db, chat_id=chat.id, role="user", content=QUESTION)
    await repo.add_message(db, chat_id=chat.id, role="assistant", content="蓝鲸是哺乳动物。")
    await db.commit()

    resp = await client.get(f"/api/chats/{chat.id}")
    assert resp.status_code == 200
    assert [item["recovered"] for item in resp.json()["messages"]] == [False, False]

    events = list(
        (
            await db.execute(select(SessionEvent).where(SessionEvent.chat_id == chat.id))
        )
        .scalars()
        .all()
    )
    assert events == [], "没有缺口就不该写任何事件"


@pytest.mark.asyncio
async def test_chat_detail_404_for_unknown_or_trashed_chat(client, db):  # noqa: F811
    """不存在的会话 ⇒ 404「会话不存在」（与老基线同口径；回收站里的同样按不存在处理）。"""
    missing = await client.get("/api/chats/999999")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "会话不存在"

    chat = await repo.create_chat(db, title="进回收站了")
    chat.deleted_at = datetime(2026, 9, 19, 10, 0, 0)
    await db.commit()

    trashed = await client.get(f"/api/chats/{chat.id}")
    assert trashed.status_code == 404
    assert trashed.json()["detail"] == "会话不存在"


def test_message_out_field_list_matches_the_contract():
    """断言 9：``MessageOut`` 的字段表与契约 ``02:244`` **逐字对齐**。

    ⚠️ 这张表是从契约原文抄下来的**字面量**（不是从模型反射的），所以它能抓住「有人改了模型
    却没改契约」这类漂移；指令包写「13 键」，契约那一行实际列了 **14 个**字段——
    以契约原文为准（``id`` … ``created_at``），差异已在证据里登记。
    """
    from app.api.chat_schemas import ChatDetailOut, MessageOut

    expected = [
        "id",
        "role",
        "content",
        "refs",
        "kind",
        "filters",
        "reasoning",
        "thinking",
        "skill_ids",
        "agent_trace",
        "images",
        "answer_source",
        "recovered",
        "created_at",
    ]
    assert list(MessageOut.model_fields) == expected
    # ChatDetailOut = ChatOut 全字段 + 三个自己的键
    assert list(ChatDetailOut.model_fields) == [
        "id",
        "title",
        "created_at",
        "group_id",
        "archived_at",
        "messages",
        "context_summary",
        "compressed_up_to",
    ]
    assert MessageOut.model_fields["recovered"].annotation is bool, "recovered 必须是 bool（前端按真假渲染徽章）"


# --------------------------------------------------------------------------
# 端到端：断言 17（I5 收尾恰好一次）+ 10（done 帧不含 recovered）+ 11/13b（成功链路）
# --------------------------------------------------------------------------


def _faux_provider(reply: str = "蓝鲸是哺乳动物。"):
    from cubeloop.providers.faux import FauxProvider, faux_assistant_message

    provider = FauxProvider(provider_id="faux")
    provider.set_responses([faux_assistant_message(reply)])
    return provider


def _install(monkeypatch, provider) -> None:
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: provider.model("faux-chat"))
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external")
    )


async def _events_of(db, chat_id: int) -> list[SessionEvent]:
    result = await db.execute(
        select(SessionEvent).where(SessionEvent.chat_id == chat_id).order_by(SessionEvent.seq)
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_success_stream_mints_one_attempt_id_shared_with_the_message(
    client, db, monkeypatch  # noqa: F811
):
    """断言 10 + 11 + 13b（流式）：``done`` 帧**不含** ``recovered``；``assistant/attempt``
    与 ``assistant/message`` **同值**；``attempt_id`` 真落进列。
    """
    _install(monkeypatch, _faux_provider())

    async with client.stream(
        "POST", "/api/chat/stream", json={"message": QUESTION, "request_id": "req-4-4-ok"}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")
    events = parse_sse(raw)

    # 断言 17（I5）：收尾**恰好一次**
    terminal = [item for item in events if item["type"] in ("done", "stopped", "error")]
    assert len(terminal) == 1, [item["type"] for item in events]
    assert terminal[0]["type"] == "done"

    # 断言 10（🔴 2026-09-27 就地翻转）：done 帧的 `recovered` 键**已随 02 v1.0.12 加入**
    # （`回归修复` 拍板⑨「补」，前端解冻后真消费）——4.4 原口径「done 帧不含 recovered
    # （前端组装时不接它）」就此作废；本腿改为钉「键存在且为布尔、干净会话为 False」。
    assert terminal[0].get("recovered") is False, terminal[0]

    chat_id = int(terminal[0]["chat_id"])
    await db.commit()
    rows = await _events_of(db, chat_id)
    attempts = [row for row in rows if row.type == "assistant/attempt"]
    messages = [row for row in rows if row.type == "assistant/message"]
    assert len(attempts) == 1, [row.type for row in rows]
    assert len(messages) == 1

    attempt_id = attempts[0].data["attempt_id"]
    assert attempt_id, "载荷必须带 attempt_id"
    assert attempts[0].data["outcome"] == attempt_mod.ATTEMPT_COMMITTED
    assert set(attempts[0].data) == {
        "attempt_id", "provider", "model", "outcome", "generation_status", "finish_reason"
    }
    assert attempts[0].data["generation_status"] == "completed"
    assert attempts[0].data["finish_reason"] == "stop"
    # 断言 11：两处同值
    assert messages[0].attempt_id == attempt_id
    assert messages[0].data["kind"] != "stopped"
    # 断言 13b：列上真的有值（`ix_session_events_attempt` 才用得上）
    assert attempts[0].attempt_id == attempt_id


@pytest.mark.asyncio
async def test_sync_chat_also_mints_an_attempt_id(client, db, monkeypatch):  # noqa: F811
    """断言 13b（同步链路）：老基线只在流式那条铸 —— 只抄那一条会让同步端点**永远没有**
    ``assistant/attempt``（核验代理 B1 打回的那点）。
    """
    _install(monkeypatch, _faux_provider("同步回答"))

    resp = await client.post("/api/chat", json={"message": QUESTION, "request_id": "req-4-4-sync"})
    assert resp.status_code == 200, resp.text
    chat_id = int(resp.json()["chat_id"])

    await db.commit()
    rows = await _events_of(db, chat_id)
    attempts = [row for row in rows if row.type == "assistant/attempt"]
    messages = [row for row in rows if row.type == "assistant/message"]
    assert len(attempts) == 1, [row.type for row in rows]
    assert attempts[0].data["outcome"] == attempt_mod.ATTEMPT_COMMITTED
    assert messages[0].attempt_id == attempts[0].attempt_id
    assert attempts[0].attempt_id, "列上必须有值"


@pytest.mark.asyncio
async def test_sync_chat_does_not_mint_for_a_clarify_turn(client, db, monkeypatch):  # noqa: F811
    """不算 attempt 的轮次：``clarify``（预置文案、没调模型）**不许**铸 id。

    判据只有一条——**会不会落 ``assistant/message`` 的模型回答**。clarify 轮虽然也落消息，
    但它没有模型尝试 ⇒ 没有 ``assistant/attempt``。
    """
    from app.core.config import settings

    _install(monkeypatch, _faux_provider("请问是哪份文档？"))
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", True)

    # 「那个怎么样」是既有用例统一用的**模糊问句**（tests/api/test_acceptance_scenarios.py 场景 3）
    resp = await client.post("/api/chat", json={"message": "那个怎么样", "request_id": "req-4-4-clar"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "clarify", resp.json()

    chat_id = int(resp.json()["chat_id"])
    await db.commit()
    rows = await _events_of(db, chat_id)
    assert [row for row in rows if row.type == "assistant/attempt"] == []


# --------------------------------------------------------------------------
# 停止链路：断言 11（停止轮）+ 12（载荷四键）
# --------------------------------------------------------------------------


class _SlowModel:
    """先吐几个增量、再长时间静默的流式假模型（给停止信号留出确定的落点）。"""

    def __init__(self, parts: list[str], *, delay: float = 0.05, tail_delay: float = 3.0):
        self.parts = list(parts)
        self.delay = delay
        self.tail_delay = tail_delay
        self.spec = SimpleNamespace(id="slow-fake")

    async def stream(self, messages, system_prompt=None):  # noqa: ANN001
        return self._iterate()

    async def _iterate(self):
        for index, part in enumerate(self.parts):
            if index:
                await asyncio.sleep(self.delay)
            yield SimpleNamespace(type="text_delta", delta=part)
        await asyncio.sleep(self.tail_delay)


@pytest.mark.asyncio
async def test_stopped_turn_shares_attempt_id_and_fingerprints_the_delivered_prefix(
    client, db, monkeypatch  # noqa: F811
):
    """断言 11（停止轮）+ 12：``assistant/attempt`` ↔ ``stream/interrupted`` 同值；
    载荷四键齐、哈希形状正确、``chars`` 与 ``delivered_chars`` 同源。
    """
    model = _SlowModel(["蓝鲸", "是", "哺乳动物"])
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("external")
    )

    async def _stop_soon():
        await asyncio.sleep(0.2)
        return await client.post("/api/chat/stop", json={"request_id": "req-4-4-stop"})

    stopper = asyncio.create_task(_stop_soon())
    async with client.stream(
        "POST", "/api/chat/stream", json={"message": QUESTION, "request_id": "req-4-4-stop"}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")
    await stopper
    events = parse_sse(raw)

    terminal = [item for item in events if item["type"] in ("done", "stopped", "error")]
    assert len(terminal) == 1, [item["type"] for item in events]
    assert terminal[0]["type"] == "stopped", [item["type"] for item in events]

    chat_id = int(terminal[0]["chat_id"])
    await db.commit()
    rows = await _events_of(db, chat_id)
    interrupted = [row for row in rows if row.type == "stream/interrupted"]
    attempts = [row for row in rows if row.type == "assistant/attempt"]
    assert len(interrupted) == 1 and len(attempts) == 1, [row.type for row in rows]

    payload = interrupted[0].data
    for key in ("attempt_id", "reason", "delivered_chars", "delivered_text_hash"):
        assert key in payload, payload
    assert payload["reason"] == "stopped"
    assert payload["chars"] == payload["delivered_chars"], "老口径的 chars 与新凭证必须同源"
    assert payload["delivered_text_hash"].startswith(attempt_mod.HASH_PREFIX)
    assert len(payload["delivered_text_hash"]) == len(attempt_mod.HASH_PREFIX) + 16

    # 断言 11：停止轮的尝试 ↔ 中断事件同值；终态是 interrupted
    assert attempts[0].data["outcome"] == attempt_mod.ATTEMPT_INTERRUPTED
    assert attempts[0].attempt_id == payload["attempt_id"] == interrupted[0].attempt_id

    # 停止轮仍要落用户消息（提问不能因停止而消失）
    stored = list(
        (
            await db.execute(
                select(Message).where(Message.chat_id == chat_id).order_by(Message.id)
            )
        )
        .scalars()
        .all()
    )
    assert stored[0].role == "user"


@pytest.mark.asyncio
async def test_attempt_payload_outcome_rejects_unknown_value_in_the_real_path():
    """ ``attempt_payload`` 的取值域是**封闭**的：写错的 ``outcome`` 直接抛（不默写未知值）。"""
    with pytest.raises(ValueError):
        attempt_mod.attempt_payload("x", outcome="abandoned")


# --------------------------------------------------------------------------
# 断言 21：ChatStopIn.request_id 上限 64
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_request_id_over_64_chars_is_422(client):  # noqa: F811
    """契约 ``02:202``：``/chat/stop`` 的 ``request_id`` 与 ``ChatRequest`` / 列宽统一为 **64**。

    128 的那版会让「同一个 id 在两种端点上两种长度语义」，超长 id 还会在 MySQL 严格模式下
    抛 ``DataError``（非 ``IntegrityError`` ⇒ 不重试，被 ``safe_append_event`` 静默吞掉）。
    """
    exact = await client.post("/api/chat/stop", json={"request_id": "x" * 64})
    assert exact.status_code == 200, exact.text

    too_long = await client.post("/api/chat/stop", json={"request_id": "x" * 65})
    assert too_long.status_code == 422
    assert "请求参数不正确" in json.dumps(too_long.json(), ensure_ascii=False)
