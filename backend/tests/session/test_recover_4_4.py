"""条目 4.4 · 崩溃恢复（``ensure_balanced`` 等价物 + ``recovered`` 现算）的用例。

**行为基线**：``<legacy-repo>\\backend\\app\\rag\\recover.py``（只读快照）。本文件逐条钉住开工指令包
§4 里属于恢复的那几条断言（编号沿用指令包）：

| # | 断言 |
|---|---|
| 1 | 幂等：连跑两次 ⇒ 第二次**零新增事件** |
| 2 | 情形 A（有 ``tool/call`` 无 ``tool/result``）⇒ 补 ``recovery/synthesized``（``TOOL_OUTCOME_UNKNOWN``）+ **成对** ``tool/result`` |
| 3 | 情形 B（assistant 声明要用工具但无 ``tool/call``）⇒ ``TOOL_NOT_STARTED`` |
| 4 | 恢复**绝不改已提交行**（前后对比既有行逐字不变） |
| 5 | ``recovery/synthesized`` 载荷**五键齐** + ``synthesized: True`` |
| 6/7 | ``recovered`` 由事件**现算**，不吃 ``ensure_balanced`` 的返回值 |
| N2′ | 挂载点退化：扫不到归属 assistant 消息时挂到**该会话最后一条消息** |

数据库用具例的 ``db`` 夹具（真方言）；事件一律经 ``app.session.events`` 写入
（**业务代码禁止直接构造 ``SessionEvent``**，用例同样守这条）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import SessionEvent
from app.session import events as event_log
from app.session import recover
from app.session import repository as repo

pytestmark = pytest.mark.asyncio


async def _new_chat(db, title: str = "恢复用例"):
    chat = await repo.create_chat(db, title=title)
    await db.commit()
    return chat


async def _add_message(db, chat_id: int, role: str, content: str, **kwargs):
    message = await repo.add_message(db, chat_id=chat_id, role=role, content=content, **kwargs)
    await db.commit()
    return message


async def _rows(db, chat_id: int) -> list[SessionEvent]:
    """该会话全部事件行（按 ``seq`` 升序）。"""
    result = await db.execute(
        select(SessionEvent).where(SessionEvent.chat_id == chat_id).order_by(SessionEvent.seq)
    )
    return list(result.scalars().all())


def _snapshot(rows: list[SessionEvent]) -> list[tuple]:
    """把事件行**逐字**拍平成可比对形态（含载荷与两个关联 id）。

    ⚠️ 事件行上**没有** ``surface_op`` 列（那是写入时的校验参数，不是字段）——
    别把它写进快照，否则断言比的是「一个恒为 None 的假字段」。
    """
    return [
        (row.seq, row.type, dict(row.data or {}), row.trace, row.tool_call_id, row.attempt_id)
        for row in rows
    ]


async def _dangling_call(db, chat_id: int, call_id: str, tool: str = "knowledge_search") -> None:
    """造一个「有 call 无 result」的缺口（Agent 执行中崩溃后日志里就是这个样子）。"""
    await event_log.append_event(
        db,
        chat_id,
        "tool/call",
        {"tool_call_id": call_id, "tool": tool, "args": {}, "round": 1},
        turn=1,
        tool_call_id=call_id,
    )
    await db.commit()


# --------------------------------------------------------------------------
# 断言 2：情形 A
# --------------------------------------------------------------------------


async def test_case_a_synthesises_unknown_outcome_and_pairs_a_result(db):
    """情形 A：有 ``tool/call`` 没有 ``tool/result`` ⇒ ``TOOL_OUTCOME_UNKNOWN`` + 成对结果。"""
    chat = await _new_chat(db)
    await _add_message(db, chat.id, "user", "上次那个问题")
    await _dangling_call(db, chat.id, "call-aaa")

    synthesized = await recover.ensure_balanced(db, chat.id)
    await db.commit()

    assert [item.kind for item in synthesized] == [recover.TOOL_OUTCOME_UNKNOWN]
    assert synthesized[0].tool_call_id == "call-aaa"
    assert synthesized[0].tool == "knowledge_search"
    assert synthesized[0].text == recover.outcome_unknown_text("knowledge_search")
    assert "结论未知" in synthesized[0].text, "文案语义必须保留「结论未知而不是没做」"

    rows = await _rows(db, chat.id)
    by_type: dict[str, list[SessionEvent]] = {}
    for row in rows:
        by_type.setdefault(row.type, []).append(row)
    assert [row.type for row in rows][-2:] == ["recovery/synthesized", "tool/result"]
    assert by_type["tool/result"][-1].data["ok"] is False
    assert by_type["tool/result"][-1].data["error"] == recover.TOOL_OUTCOME_UNKNOWN
    assert by_type["tool/result"][-1].data["content"] == synthesized[0].text


async def test_case_a_result_is_a_surface_event_with_synthesized_flag(db):
    """补的 ``tool/result`` 必须是**表层事件**（``surface_op`` 写死 ``"append"``），且三处载荷带 ``synthesized``。

    为什么必须是表层：只有表层事件会被投影进下一轮的模型历史——「模型知道上次那个工具没结果」
    这句话才落得了地（老基线 recover 的模块 docstring 第 2 条）。
    """
    chat = await _new_chat(db)
    await _add_message(db, chat.id, "user", "提问")
    await _dangling_call(db, chat.id, "call-surf")
    await recover.ensure_balanced(db, chat.id)
    await db.commit()

    rows = await _rows(db, chat.id)
    result = next(row for row in rows if row.type == "tool/result")
    # 表层事件的判据在事件层：能写进来就说明 surface_op 非空（否则 append_event 会抛）
    assert event_log.is_surface_event("tool/result") is True
    assert result.data["synthesized"] is True
    assert result.data["tool_call_id"] == "call-surf"


async def test_case_a_result_carries_append_surface_op(db, monkeypatch):
    """``surface_op`` **写死** ``"append"``（核验代理 B6：事件层只要求非空，不写死必然漂移）。"""
    chat = await _new_chat(db)
    await _add_message(db, chat.id, "user", "提问")
    await _dangling_call(db, chat.id, "call-op")

    seen: list[dict] = []
    original = event_log.safe_append_event

    async def _spy(*args, **kwargs):  # noqa: ANN002, ANN003
        seen.append({"type": args[2], "surface_op": kwargs.get("surface_op")})
        return await original(*args, **kwargs)

    monkeypatch.setattr(event_log, "safe_append_event", _spy)
    await recover.ensure_balanced(db, chat.id)
    await db.commit()

    ops = [item for item in seen if item["type"] == "tool/result"]
    assert ops and ops[0]["surface_op"] == "append", seen
    # ``recovery/synthesized`` 是**仅日志事件**：带 surface_op 会被事件层直接拒
    assert [item for item in seen if item["type"] == "recovery/synthesized"][0]["surface_op"] is None


# --------------------------------------------------------------------------
# 断言 3：情形 B
# --------------------------------------------------------------------------


async def test_case_b_synthesises_not_started_plus_a_synthetic_call(db):
    """情形 B：assistant 声明要用工具、日志里没有 ``tool/call`` ⇒ ``TOOL_NOT_STARTED``。

    并且**必须补一条合成的 ``tool/call``**——不补就是「有 result 没有 call」的倒挂，
    正是本条要消灭的东西。
    """
    chat = await _new_chat(db)
    await _add_message(db, chat.id, "user", "提问")
    assistant = await _add_message(
        db,
        chat.id,
        "assistant",
        "我来查一下",
        agent_trace={
            "mode": "react",
            "steps": [
                {"kind": "tool", "title": "检索", "tool": "knowledge_search", "tool_call_id": "call-bbb"},
                # 没有 id 的步骤必须跳过（老基线 docstring 第 4 条：无 id 一律跳过）
                {"kind": "tool", "title": "检索", "tool": "knowledge_search", "tool_call_id": None},
                {"kind": "note", "title": "备注"},
            ],
            "events_incomplete": False,
        },
    )

    synthesized = await recover.ensure_balanced(db, chat.id)
    await db.commit()

    assert [item.kind for item in synthesized] == [recover.TOOL_NOT_STARTED]
    assert synthesized[0].tool_call_id == "call-bbb"
    assert synthesized[0].message_id == int(assistant.id), "情形 B 的结论必须挂回那条 assistant 消息"

    rows = await _rows(db, chat.id)
    types = [row.type for row in rows]
    assert types[-3:] == ["recovery/synthesized", "tool/call", "tool/result"]
    call = next(row for row in rows if row.type == "tool/call")
    assert call.data["synthesized"] is True
    assert call.data["tool_call_id"] == "call-bbb"
    assert call.tool_call_id == "call-bbb", "物化列与载荷必须同值（I4 的配对键）"


# --------------------------------------------------------------------------
# 断言 1 / 4：幂等 + 绝不改已提交行
# --------------------------------------------------------------------------


async def test_ensure_balanced_is_idempotent_and_never_rewrites_history(db):
    """连跑两次：第二次**零新增**，且既有行**逐字不变**（只追加，不 UPDATE / DELETE）。"""
    chat = await _new_chat(db)
    await _add_message(db, chat.id, "user", "提问")
    await _dangling_call(db, chat.id, "call-idem")

    first = await recover.ensure_balanced(db, chat.id)
    await db.commit()
    assert len(first) == 1

    before = _snapshot(await _rows(db, chat.id))
    second = await recover.ensure_balanced(db, chat.id)
    await db.commit()

    assert second == [], "第二次不该再合成任何东西"
    assert _snapshot(await _rows(db, chat.id)) == before, "既有事件行必须逐字不变"


async def test_empty_chat_id_is_a_no_op(db):
    """``chat_id`` 为空 ⇒ 直接返回空表，**零 DB 触碰**（既有调用方兼容）。"""
    assert await recover.ensure_balanced(db, None) == []
    assert await recover.recovered_message_ids(db, None) == set()


async def test_clean_chat_produces_nothing(db):
    """干净会话（配对本来完整）⇒ 一条都不合成。"""
    chat = await _new_chat(db, "干净会话")
    await _add_message(db, chat.id, "user", "提问")
    await _add_message(db, chat.id, "assistant", "回答")
    await event_log.append_event(
        db, chat.id, "tool/call", {"tool_call_id": "call-ok", "tool": "t", "args": {}, "round": 1},
        tool_call_id="call-ok",
    )
    await event_log.append_event(
        db, chat.id, "tool/result", {"tool_call_id": "call-ok", "tool": "t", "ok": True},
        surface_op="append", tool_call_id="call-ok",
    )
    await db.commit()

    assert await recover.ensure_balanced(db, chat.id) == []


# --------------------------------------------------------------------------
# 断言 5：载荷键集
# --------------------------------------------------------------------------


async def test_recovery_payload_has_the_five_keys_plus_synthesized(db):
    """``recovery/synthesized`` 载荷五键齐（``tool_call_id``/``kind``/``text``/``tool``/``message_id``）+ ``synthesized``。"""
    chat = await _new_chat(db)
    await _add_message(db, chat.id, "user", "提问")
    await _dangling_call(db, chat.id, "call-keys")
    await recover.ensure_balanced(db, chat.id)
    await db.commit()

    rows = await _rows(db, chat.id)
    payload = next(row for row in rows if row.type == "recovery/synthesized").data
    for key in ("tool_call_id", "kind", "text", "tool", "message_id"):
        assert key in payload, f"缺少 {key}：{payload}"
    assert payload["synthesized"] is True
    assert isinstance(payload["message_id"], int), "界面要按它标「上次工具结果不明」"


# --------------------------------------------------------------------------
# 断言 6 / 7：``recovered`` 由事件现算
# --------------------------------------------------------------------------


async def test_recovered_ids_are_computed_from_events_not_from_the_return_value(db):
    """恢复痕迹**现算**：先跑一轮、再单独查，仍然为真（老基线注释：否则提示一闪就没）。"""
    chat = await _new_chat(db)
    user = await _add_message(db, chat.id, "user", "提问（也是崩溃轮的挂载点）")
    await _dangling_call(db, chat.id, "call-live")

    synthesized = await recover.ensure_balanced(db, chat.id)
    await db.commit()
    assert synthesized[0].message_id == int(user.id), "N2′：扫不到归属 assistant 消息 ⇒ 退化到最后一条消息"

    # 第二次进入同一会话：ensure_balanced 的**返回值是空的**，但现算仍然为真
    again = await recover.ensure_balanced(db, chat.id)
    await db.commit()
    assert again == []
    assert await recover.recovered_message_ids(db, chat.id) == {int(user.id)}


# --------------------------------------------------------------------------
# N2′：挂载点退化的另一半——能定位到就挂回真正发起调用的那条 assistant 消息
# --------------------------------------------------------------------------


async def test_mount_point_prefers_the_owning_assistant_message(db):
    """``step_owner.get(call_id) or last_message_id`` 的**前半**：能定位就挂回那条 assistant 消息。

    后半（退化到最后一条消息）见上一条；两半合起来才等于老基线 ``recover.py:216/236`` 的口径。
    """
    chat = await _new_chat(db)
    await _add_message(db, chat.id, "user", "提问")
    owner = await _add_message(
        db,
        chat.id,
        "assistant",
        "我调了工具",
        agent_trace={
            "mode": "react",
            "steps": [{"kind": "tool", "title": "检索", "tool": "knowledge_search", "tool_call_id": "call-owner"}],
            "events_incomplete": False,
        },
    )
    # 崩溃发生在工具执行中：assistant 消息**已经**落库（本行），call 也写了，但 results 没有
    await _dangling_call(db, chat.id, "call-owner")
    await _add_message(db, chat.id, "user", "这是最后一条消息（不该被选中）")

    synthesized = await recover.ensure_balanced(db, chat.id)
    await db.commit()

    assert [item.message_id for item in synthesized] == [int(owner.id)]
    assert await recover.recovered_message_ids(db, chat.id) == {int(owner.id)}
