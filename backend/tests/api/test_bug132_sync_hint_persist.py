"""回归修复 载体：同步 ``/chat`` 对固定中文提示族的落库语义向流式对齐（拍板单 B）。

缺陷（修复前）：同一请求打两个端点——流式 ``hint_for_error`` 命中 ⇒
``persist_user_turn(note=hint)``（只落用户消息，不落 assistant）；同步端点 ``persist_turn``
**无条件**落库 ⇒ 把提示当**正常 assistant 消息**落库 ⇒ 下一轮 ``build_model_history``
把「模型响应超时…」当助手历史**回灌给模型**，且两端点落库语义相反。

OPT2：失败由上游错误状态判定，正常正文里的提示词不是失败。真实超时/鉴权同步返回
504/401，通用失败仍 200 + 固定文案；两路失败均只留 user、attempt aborted、turn/end error。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.api import chat_routes
from app.core.errors import AUTH_HINT, TIMEOUT_HINT
from app.models import Message
from app.session import attempt as attempt_mod
from app.session import events as event_log
from tests.api.test_chat_contract import client, parse_sse  # noqa: F401
from tests.api.test_bug153_stream_error_semantics import _FakeStream, _FailingModel as StreamFailure
from tests.api.test_bug154_sync_error_semantics import (
    _FailingModel as SyncFailure, _error_result, _install as install_failure,
)


async def _messages(db, chat_id: int) -> list[Message]:
    # 先结束本会话可能持有的读快照（MySQL REPEATABLE READ：不 rollback 会读到旧 snapshot，
    # 看不到后续请求新提交的行——照 test_bug101 同场景口径）
    await db.rollback()
    return list(
        (
            await db.execute(select(Message).where(Message.chat_id == chat_id).order_by(Message.id))
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type,detail,code,hint", [
    ("ProviderTimeout", TIMEOUT_HINT, 504, TIMEOUT_HINT),
    ("ProviderAuthError", AUTH_HINT, 401, AUTH_HINT),
    ("ProviderUnavailable", "ProviderUnavailable: Connection error.", 200,
     "回答生成失败，请稍后重试。（ProviderUnavailable: Connection error.）"),
])
async def test_sync_hint_round_matches_streaming_persist_semantics(
    client, db, monkeypatch, error_type, detail, code, hint,
):
    """缺陷最小复现形状：同一会话先同步后流式各一轮 hint ⇒ 两轮落库形状一致。"""
    final = _error_result(error_type=error_type, error_message=detail)
    install_failure(monkeypatch, SyncFailure(final=final))

    sync_resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert sync_resp.status_code == code
    sync_body = sync_resp.json()
    if code == 200:
        assert sync_body["message"] == hint
        chat_id = sync_body["chat_id"]
    else:
        assert sync_body == {"detail": hint}
        chats = (await client.get("/api/chats")).json()
        chat_id = chats[0]["id"]

    # 修复前：同步这轮多出一条 assistant 消息（提示被当回答落库）
    rows = await _messages(db, chat_id)
    assert [row.role for row in rows] == ["user"]
    assert rows[0].content == "向量库用什么存储？"

    # 事件面与流式同构：turn/end(kind=error) + attempt(aborted)
    await db.rollback()
    turn_end = await event_log.list_events_by_type(db, chat_id, "turn/end")
    assert turn_end[-1].data["kind"] == "error"
    attempts = await event_log.list_events_by_type(
        db, chat_id, attempt_mod.ATTEMPT_EVENT
    )
    assert attempts[-1].data["outcome"] == attempt_mod.ATTEMPT_ABORTED

    # 流式孪生：同参数打流式端点，同样只留用户消息（两路径语义一致）
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: StreamFailure(_FakeStream([], final)))
    async with client.stream(
        "POST", "/api/chat/stream", json={"message": "再来一问。", "chat_id": chat_id}
    ) as resp:
        raw = (await resp.aread()).decode("utf-8")
    events = parse_sse(raw)
    assert events[-1]["type"] == "error"
    assert events[-1] == {"type": "error", "detail": f"生成失败，请稍后重试。（{error_type}）"}
    assert not [frame for frame in events if frame["type"] in {"done", "token"}]

    rows = await _messages(db, chat_id)
    assert [row.role for row in rows] == ["user", "user"]
    ended = await event_log.list_events_by_type(db, chat_id, "turn/end")
    attempts = await event_log.list_events_by_type(db, chat_id, attempt_mod.ATTEMPT_EVENT)
    assert [row.data["kind"] for row in ended] == ["error", "error"]
    assert [row.data["outcome"] for row in attempts] == [attempt_mod.ATTEMPT_ABORTED] * 2
