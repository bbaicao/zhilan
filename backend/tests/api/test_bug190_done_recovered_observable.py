"""回归修复 判据载体：done 帧 `recovered` 读数失败必须**可区分于真空结果**。

口径（逐字，防误读）

· 靶点 = 流式收尾算 `chat_recovered` 的那一处：
  try: chat_recovered = bool(await recover_mod.recovered_message_ids(db, plan.chat.id))
  except Exception: chat_recovered = False      # ← 修前：静默吞（与「真无痕迹」逐字同形）
  修后：同一分支留一条 `logger.warning`（含异常类名与原因），字段仍是 bool False。
· ⛔ 契约面**不变**：02 v1.0.12 起 done 帧 = **恰十键**且 `recovered: bool`
  （`接口说明/02-API契约冻结清单.md` 变更记录 v1.0.12 + §2 done 行）——本卡**不改字段类型语义**
  （I1 只加不减），故「可区分」由**告警**承载；I5（观测项不炸收尾）仍成立：帧照发、值照旧 False。
· 腿①（主腿，修前红）= 注入读数异常 ⇒ done 帧照发 + `recovered is False` + **有 WARNING**（指名异常）；
  腿②（keep）= 真无痕迹 ⇒ `recovered is False` 且**无告警**（⇒ 与腿①可区分）；
  腿③（keep）= 读数为真 ⇒ `recovered is True` 且无告警（真值通路未被这段改动碰坏）；
  腿④（I1 守卫）= done 帧键集恰十键（02 v1.0.12 口径）。
"""

from __future__ import annotations

import logging

import pytest

from app.session import recover as recover_mod
from tests.api.test_chat_contract import (  # noqa: F401 —— use_faux/client 是本仓既有夹具（跨文件导入是既有做法）
    client,
    parse_sse,
    use_faux,
)

#: 02 §2 v1.0.12 起 done 帧 = 恰十键（type + 九个业务字段 + recovered）。
DONE_KEYS = {
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

#: 修后告警里必须出现的固定中文片段（判据锚在**语义**上，不绑行号）。
FAILURE_MARKER = "恢复痕迹读数失败"


async def _stream_once(client, message: str = "你好") -> list[dict]:
    """发一轮流式（faux 模型由 use_faux 夹具钉），返回帧列表（末帧必须是 done）。"""
    resp = await client.post("/api/chat/stream", json={"message": message})
    assert resp.status_code == 200, resp.text
    frames = parse_sse(resp.text)
    assert frames[-1]["type"] == "done", frames[-1]
    return frames


def _recovery_warnings(caplog) -> list[str]:  # noqa: ANN001
    """本模块里与「恢复痕迹读数失败」有关的告警原文（空列表 ⇒ 没有留痕）。"""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.api.chat_routes" and FAILURE_MARKER in record.getMessage()
    ]


@pytest.mark.asyncio
async def test_recovery_read_failure_is_observable_and_done_frame_survives(
    client, use_faux, monkeypatch, caplog  # noqa: ANN001
):
    """腿①（主腿）：注入读数异常 ⇒ 帧照发、值仍 False，但**必须留告警**（修前为红）。"""
    use_faux("好。")

    async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("probe: db down while reading recovery marks")

    monkeypatch.setattr(recover_mod, "recovered_message_ids", _boom)
    caplog.set_level(logging.WARNING, logger="app.api.chat_routes")

    frames = await _stream_once(client)
    done = frames[-1]
    # I5：观测项失败**不炸收尾** —— done 帧照发、键集不变、字段仍是 bool。
    assert set(done) == DONE_KEYS, done
    assert done["recovered"] is False
    # 本卡主张：失败必须**可观测**（否则与「真无痕迹」逐字同形，运维面看不出库坏了）。
    warnings = _recovery_warnings(caplog)
    assert warnings, "读数失败必须留一条告警，否则与真空结果不可区分"
    assert any("RuntimeError" in text for text in warnings), warnings
    assert any("probe: db down" in text for text in warnings), warnings


@pytest.mark.asyncio
async def test_vacuum_recovery_read_stays_silent(client, use_faux, caplog):  # noqa: ANN001
    """腿②（keep）：真无痕迹 ⇒ recovered False 且**无告警** ⇒ 与腿①可区分。"""
    use_faux("好。")
    caplog.set_level(logging.WARNING, logger="app.api.chat_routes")

    frames = await _stream_once(client)
    assert frames[-1]["recovered"] is False
    assert _recovery_warnings(caplog) == [], "真空结果不该留告警"


@pytest.mark.asyncio
async def test_recovered_true_path_unchanged(client, use_faux, monkeypatch, caplog):  # noqa: ANN001
    """腿③（keep）：读数为真 ⇒ recovered True 且无告警（真值通路未被碰坏）。"""
    use_faux("好。")

    async def _ids(db, chat_id):  # noqa: ANN001
        return {1}

    monkeypatch.setattr(recover_mod, "recovered_message_ids", _ids)
    caplog.set_level(logging.WARNING, logger="app.api.chat_routes")

    frames = await _stream_once(client)
    assert frames[-1]["recovered"] is True
    assert _recovery_warnings(caplog) == []


@pytest.mark.asyncio
async def test_done_frame_key_set_is_still_ten(client, use_faux):  # noqa: ANN001
    """腿④（I1 守卫）：done 帧键集**恰十键** —— 本卡不得增键 / 改键义。"""
    use_faux("好。")
    frames = await _stream_once(client)
    assert set(frames[-1]) == DONE_KEYS, frames[-1]

