"""`回归修复` 判据载体：**SSE done 帧补 `recovered`**（恢复标记免刷新可见）。

🔴 口径（逐字，防误读）

· 2026-09-27 拍板⑨「补」：done 帧新增第 10 键 `recovered`（bool）——该会话是否存在恢复痕迹
  （现算，与 ``MessageOut.recovered`` 同源 ``recover.recovered_message_ids``，读
  ``recovery/synthesized`` 事件）；02 §2/§7.1 v1.0.12 登记（done 十字段）。
· 前提变更登记在 02 §8.4：前端 2026-09-24 解冻 ⇒ 「SSE 链不通」的豁免（本账唯一一条
  遗留-用户豁免）就此解除——前端 `App.tsx` 消费 `ev.recovered` ⇒ true 时拉取会话消息。
· ⚠️ done 帧只**读**恢复痕迹，**不在**收尾跑 ``ensure_balanced``（补平仍归
  ``GET /chats/{id}`` 读路径，4.4 口径不变）——所以本载体用**直接落一条
  recovery/synthesized 事件**的方式造「已有恢复痕迹」的前提（等价于上次崩溃已被
  GET 补平过的会话）。
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.session import events as event_log
from app.session import recover as recover_mod
from tests.api.test_chat_contract import (  # noqa: F401 —— use_faux 是夹具（本仓既有做法：跨文件导入）
    client,
    parse_sse,
    use_faux,
)


async def _stream_once(client, db=None, message: str = "你好", chat_id: int | None = None) -> list[dict]:  # noqa: ANN001
    """发一轮流式（faux 模型由 use_faux 夹具钉；这里复用 client 的既有桩面）。"""
    payload: dict = {"message": message}
    if chat_id is not None:
        payload["chat_id"] = chat_id
    resp = await client.post("/api/chat/stream", json=payload)
    assert resp.status_code == 200, resp.text
    frames = parse_sse(resp.text)
    assert frames[-1]["type"] == "done"
    return frames


@pytest.mark.asyncio
async def test_done_frame_has_recovered_key_false_for_fresh_chat(client, use_faux):
    """腿①：无恢复痕迹的新会话 ⇒ done.recovered 恰为 False（键存在、布尔、False）。"""
    use_faux("好。")
    frames = await _stream_once(client)
    done = frames[-1]
    assert "recovered" in done
    assert done["recovered"] is False


@pytest.mark.asyncio
async def test_done_frame_recovered_true_after_recovery_event(client, use_faux, db):
    """腿②（主腿）：会话里有 recovery/synthesized 痕迹 ⇒ 下一轮 done.recovered == True。"""
    # 桩条数补足（判据侧，非产品行为）：本腿在同一会话连发**两轮**，而 Faux 的响应队列
    # 按调用顺序消费 ⇒ 须排 2 条。本条原绿依赖 回归修复 的静默吞错
    # （faux 队列耗尽 ⇒ stop_reason='error'），153 修复后如实发 error 帧 ⇒ 补足桩队列。
    # 靶点未漂：断言仍是「第二轮末帧 done 且 recovered 为 True」，
    # 且第二轮现在**真的完成一轮**（stream 正常收尾），不再靠异常吞掉收尾。
    use_faux("好。", extra_replies=["好的。"])
    frames = await _stream_once(client, db)
    chat_id = int(frames[0]["chat_id"])

    # 造「已有恢复痕迹」前提：直接落一条 recovery/synthesized（与 ensure_balanced 的产物同型）。
    # message_id 用该会话**真实存在**的首条消息（autoincrement 跨用例累进，不能硬编码 1）。
    from app.models import Message
    from sqlalchemy import select

    first_msg_id = int(
        (
            await db.execute(
                select(Message.id)
                .where(Message.chat_id == chat_id)
                .order_by(Message.id)
                .limit(1)
            )
        ).scalar_one()
    )
    await event_log.safe_append_event(
        db,
        chat_id,
        recover_mod.RECOVERY_EVENT,
        {
            "tool_call_id": "probe-tc-1",
            "kind": "TOOL_OUTCOME_UNKNOWN",
            "text": "载体检探：合成恢复痕迹",
            "tool": "knowledge_search",
            "message_id": first_msg_id,
            "synthesized": True,
        },
        turn=1,
    )
    await db.commit()

    frames2 = await _stream_once(client, db, "再问一句", chat_id=chat_id)
    done2 = frames2[-1]
    assert done2["recovered"] is True, f"恢复痕迹应被 done 帧带出：{done2}"

    # 与 GET /chats/{id} 的 MessageOut.recovered 同源交叉核对（首条消息那行应为真）。
    detail = await client.get(f"/api/chats/{chat_id}")
    assert detail.status_code == 200
    row = next(m for m in detail.json()["messages"] if m["id"] == first_msg_id)
    assert row["recovered"] is True


@pytest.mark.asyncio
async def test_done_recovered_does_not_add_another_balance_call(client, use_faux, db, monkeypatch):
    """腿③（口径钉）：done 收尾**不追加** ensure_balanced 调用（补平仍归 GET /chats/{id} 读路径）。

    ⓘ 既有事实：**消息发送路径**在本轮开始前本来就跑一次 ensure_balanced
    （``chat_routes.py`` 的发送入口「读之前先补平」）⇒ 本腿钉的是
    「加入 done.recovered 之后**没有**多出第二次调用」。
    """
    use_faux("好。")
    called: list[int] = []
    real = recover_mod.ensure_balanced

    async def spy(*args, **kwargs):  # noqa: ANN001, ANN002
        called.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(recover_mod, "ensure_balanced", spy)
    monkeypatch.setattr(settings, "AGENT_MODE", "off")
    frames = await _stream_once(client, db)
    assert frames[-1]["recovered"] is False
    assert len(called) == 1, f"应恰发送路径那一次补平，实得 {len(called)} 次"
