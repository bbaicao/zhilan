"""`回归修复` 笔2「接线」载体（#24 的**专属**判据载体 + #29）。

🔴 口径（逐字）

· 键 = `tool_result_channel`（既有十键之一）。出口 = `GET /api/agent/audit` 的
  `counters.derived.tool_result_channel` ⇒ **机器可读 + 人工 curl 的可见面**；
  ⛔ 不写「用户可见 / 前端可见」——现有前端不消费该端点。
· **#24 专属载体**（`runner._finish` 的工具结果通道**回落**路径）：在进程内直接调 `_finish`，
  不经过 HTTP/SSE ⇒ 断言的是「通道构造抛错 ⇒ 回落空通道 + 恰一次计数」这条**回落本身**，
  与既有 `tests/api/test_tool_context_extra_t0.py` 的 I5 腿（走 SSE 断「本轮不炸穿」）互补：
  那条断「回答还能不能出」，这条断「回落发生过且被记了一次」。
· **#29**（`runner._file_change_step` 的「写通道三键不全」降级）：三键缺件 ⇒ 不产出
  `file_change` 步骤 + 恰一次计数；三键齐全、以及更早的早退（工具不在 `WRITE_TOOLS` /
  `meta` 为空）**零计数** —— 只钉降级那一条路径。
"""

from __future__ import annotations

import pytest

from app.agent import runner as runner_mod
from app.session import events as event_log
from tests.api.test_chat_contract import client  # noqa: F401 —— client 是夹具

KEY = "tool_result_channel"
OTHER_KEY = "trace_persist"
COMPLETE_META = {"write_target": "/workspace/a.md", "write_byte_delta": 12, "write_snapshot_id": 3}


def _counts() -> dict:
    from app.core import derived_counters

    return derived_counters.ordered_counts()


@pytest.fixture(autouse=True)
def _reset_derived_counts():
    """每个用例前后清零派生计数（计数腿是「增量」判据，必须从 0 起跑）。"""
    from app.core import derived_counters

    derived_counters.reset_failure_counts()
    try:
        yield
    finally:
        derived_counters.reset_failure_counts()


def _finish_once(collector) -> runner_mod.AgentTurn:  # noqa: ANN001
    turn = runner_mod.AgentTurn()
    runner_mod._finish(turn, collector, event_log.failure_snapshot())
    return turn


def _exploding_collector(monkeypatch) -> runner_mod.ToolCollector:  # noqa: ANN001
    """通道构造必抛的收集器（只炸 `tool_context_items`，其余收口照常）。"""
    collector = runner_mod.ToolCollector()

    def _boom():
        raise RuntimeError("probe: 通道构造失败")

    monkeypatch.setattr(collector, "tool_context_items", _boom)
    return collector


def _step(turn, *, tool: str = "workspace_write", meta=None):  # noqa: ANN001
    return runner_mod._file_change_step(turn, tool=tool, tool_call_id="c13", meta=meta)


def test_zero_state_counts_are_zero():
    """零态：不触发任何降级 ⇒ 两键均为 0（防「恒非零」的假绿）。"""
    counts = _counts()
    assert counts[KEY] == 0 and counts[OTHER_KEY] == 0, counts


# --------------------------------------------------------------------------
# #24：工具结果通道回落（专属载体）
# --------------------------------------------------------------------------


def test_24_fallback_counts_once(monkeypatch):
    """#24：通道构造任一步抛错 ⇒ 回落空通道（I5）+ 恰 +1。"""
    collector = _exploding_collector(monkeypatch)

    turn = _finish_once(collector)

    assert turn.tool_context == [], "回落空通道（I5：绝不炸穿本轮）"
    assert turn.materials == collector.materials, "其余收口照常（只有通道回落）"
    counts = _counts()
    assert counts[KEY] == 1, counts
    assert counts[OTHER_KEY] == 0, "不得误记别的键"


def test_24_control_success_path_counts_zero():
    """#24 对照腿（防空转）：不打桩 ⇒ 正常收口且计数保持 0。"""
    turn = _finish_once(runner_mod.ToolCollector())

    assert turn.tool_context == [], "空收集器 ⇒ 空通道（这是正常态，不是回落）"
    assert _counts()[KEY] == 0, "成功路径零计数"


@pytest.mark.asyncio
async def test_24_fallback_is_visible_on_audit_endpoint(client, monkeypatch):
    """#24 的出口腿：真回落一次 ⇒ 审计端点 counters.derived 读到 1。"""
    _finish_once(_exploding_collector(monkeypatch))

    resp = await client.get("/api/agent/audit")

    assert resp.status_code == 200, resp.text
    derived = resp.json()["counters"]["derived"]
    assert derived[KEY] == 1, derived


# --------------------------------------------------------------------------
# #29：写通道三键不全 ⇒ 跳过 file_change 步骤
# --------------------------------------------------------------------------


def test_29_incomplete_write_meta_counts_once():
    """#29：三键缺件 ⇒ 不产出步骤 + 恰 +1。"""
    turn = runner_mod.AgentTurn()

    step = _step(turn, meta={"write_target": "/workspace/a.md"})

    assert step is None and turn.steps == [], "三键不全 ⇒ 不产出 file_change 步骤"
    assert _counts()[KEY] == 1, _counts()


def test_29_control_complete_meta_counts_zero():
    """#29 对照腿（防空转）：三键齐全 ⇒ 真产出一步且零计数。"""
    turn = runner_mod.AgentTurn()

    step = _step(turn, meta=dict(COMPLETE_META))

    assert step is not None and step["kind"] == "file_change"
    assert len(turn.steps) == 1
    assert _counts()[KEY] == 0, "三键齐全零计数"


def test_29_earlier_early_returns_count_zero():
    """更早的早退（工具不在写入名单 / `meta` 为空）**不**计数——只钉降级那一条路径。"""
    turn = runner_mod.AgentTurn()

    assert _step(turn, tool="workspace_read", meta=dict(COMPLETE_META)) is None
    assert _step(turn, meta={}) is None
    assert _step(turn, meta=None) is None

    assert turn.steps == []
    assert _counts()[KEY] == 0, _counts()
