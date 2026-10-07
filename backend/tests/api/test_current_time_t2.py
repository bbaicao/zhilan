"""专项 · 工具调用能力第 2 批 · **① current_time + 固定文案**（A1–A6）。

判据（开工包 §2.1 + §0.7 D-a/D-b/D7/D8）：
- 工具名 ``current_time``、零参数、观察文本单行模板、本机钟带 offset；
- 固定文案 ``时间依据：本机系统时钟（含时区偏移）。`` 只在
  ``kind ∈ {normal, direct}`` 且有 ``ok=True`` 的 ``current_time`` 条目时进**交付正文**；
- 失败不编造；不读无关系统信息；两只钟分列。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agent import runner
from app.agent.audit import db_clock_is_utc, db_clock_now
from app.agent.tools import current_time as current_time_mod
from app.api import chat_routes
from app.api.chat_routes import TIME_PREFIX
from app.models import Message
from tests.api.t0_wire import (  # noqa: F401 —— client 是夹具
    Wire,
    attach_wire,
    client,
    run_stream,
    wire_env,
    wire_sse_text,
    wire_sse_tool_call,
)

#: 用例另抄一份字面量（D8 先例）
_FROZEN_TIME_PREFIX = "时间依据：本机系统时钟（含时区偏移）。"
_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}")
_OBS_RE = re.compile(
    r"读取本机时间：(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})（本机系统时钟）",
    re.S,
)


def _fixed_t() -> datetime:
    """A3 用的固定时刻：offset 写死 +08:00（与库钟可区分）。"""
    return datetime(2026, 9, 22, 11, 52, 34, tzinfo=timezone(timedelta(hours=8)))


# --------------------------------------------------------------------------
# A1 · 进清单 + off 链不受影响
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a1_current_time_enters_react_checklist(client, wire_env, monkeypatch):
    """A1 正腿：react 轮决策请求体 ``tools[]`` 含 ``current_time``。"""
    # 桩条数补足（判据侧，非产品行为）：react 轮 = 1 次**决策**调用（带 tools[]）+
    # 1 次**回答**调用 ⇒ 队列须排 2 条。本条原绿依赖 回归修复 的静默吞错
    # （wire 队列耗尽 ⇒ stop_reason='error'），153 修复后如实发 error 帧 ⇒ 补足桩队列。
    # 靶点未漂：断言仍是「末帧 done + 决策请求体 tools[] 含 current_time」，
    # 且第二轮现在**真的完成一轮**（stream 正常收尾），不再靠异常吞掉收尾。
    wire = Wire(wire_sse_text("直接回答"), wire_sse_text("直接回答"))
    attach_wire(wire, monkeypatch)
    events = await run_stream(client, message="现在几点", agent_mode="react")
    assert events[-1]["type"] == "done"
    assert wire.decision_bodies, wire.call_kinds()
    names: set[str] = set()
    for body in wire.decision_bodies:
        for tool in body.get("tools") or []:
            names.add(str((tool or {}).get("function", {}).get("name", "")))
    assert "current_time" in names, names


@pytest.mark.asyncio
async def test_a1_off_chain_has_no_tools_and_empty_tool_context(
    client, wire_env, monkeypatch
):
    """A1 否定腿：off 轮 ⇒ 只有 answer 调用、无 ``tools[]``、``plan.tool_context == []``。"""
    wire = Wire(wire_sse_text("直接回答"))
    attach_wire(wire, monkeypatch)
    plans: list = []
    real = chat_routes.build_turn_plan

    async def spy(*args, **kwargs):
        plan = await real(*args, **kwargs)
        plans.append(plan)
        return plan

    monkeypatch.setattr(chat_routes, "build_turn_plan", spy)
    events = await run_stream(client, message="你好", agent_mode="off")
    assert events[-1]["type"] == "done"
    assert wire.call_kinds() == ["answer"], wire.call_kinds()
    assert not any((b.get("tools") or []) for b in wire.bodies)
    assert plans and list(plans[0].tool_context or []) == []


# --------------------------------------------------------------------------
# A2 · 真调用 + 结果到终答
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2_current_time_result_reaches_final_answer_body(
    client, wire_env, monkeypatch, db
):
    """A2：脚本化点一次 ``current_time`` ⇒ tool/result 观察匹配 stamp 正则 + 含「本机系统时钟」；
    wire：最终回答请求体含同一串。"""
    from sqlalchemy import select

    from app.models import SessionEvent

    wire = Wire()
    attach_wire(wire, monkeypatch)
    wire.queue = [
        wire_sse_tool_call("current_time", {}, call_id="call_ct_1"),
        wire_sse_text("可以作答了"),
        wire_sse_text("好的，已读取时间。"),
    ]
    events = await run_stream(client, message="现在几点")
    assert events[-1]["type"] == "done"

    rows = await db.execute(
        select(SessionEvent)
        .where(SessionEvent.type.in_(("tool/call", "tool/result")))
        .order_by(SessionEvent.seq)
    )
    pair = list(rows.scalars().all())
    assert [e.type for e in pair] == ["tool/call", "tool/result"]
    assert pair[0].tool_call_id == pair[1].tool_call_id == "call_ct_1"
    content = str(pair[1].data.get("content") or "")
    assert pair[1].data.get("ok") is True
    assert _STAMP_RE.search(content), content
    assert "本机系统时钟" in content

    answer_body = wire.answer_bodies[-1]
    body_text = str(wire.last_user_text(answer_body)) + str(answer_body)
    assert "本机系统时钟" in body_text
    assert _STAMP_RE.search(body_text), body_text
    assert wire.call_kinds()[-1] == "answer"


# --------------------------------------------------------------------------
# A3 · 两只钟分列（有鉴别力）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a3_local_clock_and_db_clock_are_distinct(client, wire_env, monkeypatch):
    """A3：``_now`` 钉固定 T ⇒ 观察逐字等于 T 渲染的模板；``collected_at`` 走库钟且 ≠ T。"""
    t = _fixed_t()
    monkeypatch.setattr(current_time_mod, "_now", lambda: t)
    expected = current_time_mod.OBSERVATION_TEMPLATE.format(
        stamp=current_time_mod.format_stamp(t)
    )

    obs = await current_time_mod.build_spec().handler(current_time_mod.CurrentTimeArgs())
    assert obs.ok is True
    assert obs.text == expected
    assert obs.text == "读取本机时间：2026-09-22T11:52:34+08:00（本机系统时钟）"

    # collected_at 走库钟（runner 侧）；这里直接对拍 db_clock_now
    db_now = db_clock_now(None)
    assert db_now != t
    #: 216：原式 ① `or True` ⇒ 恒真、什么都没验；② 对拍轴也错 —— `db_now` 是**库内时钟**
    #: （SQLite = UTC naive、MySQL = 服务器本地），右边恒是**本机墙上钟** ⇒ 桌面档
    #: （DB_BACKEND=sqlite）整差一个时区而假红。现在按**同一条口径取同一只钟的现在**
    #: （`db_clock_is_utc(None)` 与 `db_clock_now(None)` 的回落判据同源）。
    same_clock_now = (
        datetime.now(timezone.utc).replace(tzinfo=None) if db_clock_is_utc(None) else datetime.now()
    )
    assert abs((db_now - same_clock_now).total_seconds()) < 5, (db_now, same_clock_now)
    # 关键：库钟 naive、T 带 +08:00 —— 两轴不得混用
    assert t.tzinfo is not None


def test_a3_observation_template_fullmatch():
    """A6 的正则腿并入 A3 旁证：单行模板 ``fullmatch``（不得多第二行）。"""
    t = _fixed_t()
    text = current_time_mod.OBSERVATION_TEMPLATE.format(stamp=current_time_mod.format_stamp(t))
    assert re.fullmatch(
        r"读取本机时间：\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}（本机系统时钟）",
        text,
        re.S,
    )


# --------------------------------------------------------------------------
# A4 · 固定文案
# --------------------------------------------------------------------------


def _plan_with_kind_and_context(kind: str, items: list):
    """轻量 plan 孪生，补齐 OPT2 前缀闸读取的回答状态。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        kind=kind,
        tool_context=list(items),
        assume=False,
        materials_finalized=False,
        needs_model=True,
        generation_status="completed",
    )


def _item(tool: str, ok: bool):
    from app.agent.types import ToolContextItem

    return ToolContextItem(
        tool=tool, tool_call_id="c1", ok=ok, text="x", collected_at="2026-09-22 11:52:34"
    )


def test_a4_prefix_on_normal_and_direct_with_ok_current_time():
    """A4 ① 命中：``kind ∈ {normal, direct}`` 且有 ok 的 current_time ⇒ 前缀开头。"""
    for kind in ("normal", "direct"):
        plan = _plan_with_kind_and_context(kind, [_item("current_time", True)])
        prefix = chat_routes.get_turn_prefix(plan, "回答正文")
        assert prefix.startswith(_FROZEN_TIME_PREFIX), (kind, prefix)
        assert prefix == _FROZEN_TIME_PREFIX + "\n\n"


def test_a4_no_prefix_when_tool_failed_or_wrong_tool():
    """A4 ② 工具失败 / 非 current_time ⇒ 无前缀。"""
    failed = _plan_with_kind_and_context("direct", [_item("current_time", False)])
    assert not chat_routes.get_turn_prefix(failed, "回答正文").startswith(_FROZEN_TIME_PREFIX)
    other = _plan_with_kind_and_context("direct", [_item("knowledge_search", True)])
    assert not chat_routes.get_turn_prefix(other, "回答正文").startswith(_FROZEN_TIME_PREFIX)


def test_a4_no_prefix_on_refuse_or_clarify_kind():
    """A4 的 kind 闸：refuse / clarify 不带时间前缀。"""
    for kind in ("refuse", "clarify"):
        plan = _plan_with_kind_and_context(kind, [_item("current_time", True)])
        assert not chat_routes.get_turn_prefix(plan, "回答正文").startswith(_FROZEN_TIME_PREFIX)


def test_a4_single_definition_scan_in_app():
    """A4 ⑥ 单一定义扫描：范围 = ``backend/app/``。"""
    root = Path(__file__).resolve().parents[2] / "app"
    hits = [
        str(path)
        for path in root.rglob("*.py")
        if _FROZEN_TIME_PREFIX in path.read_text(encoding="utf-8")
    ]
    assert hits == [str(root / "api" / "chat_routes.py")], hits


@pytest.mark.asyncio
async def test_a4_constants_match_copied_literal():
    assert TIME_PREFIX == _FROZEN_TIME_PREFIX


# --------------------------------------------------------------------------
# A5 · 失败不编造
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a5_failure_does_not_fabricate_time(client, wire_env, monkeypatch):
    """A5：``_now`` 抛错 ⇒ ok=False + 中文原因；终答不出现时间串；交付正文无前缀。"""

    def boom():
        raise RuntimeError("clock broken")

    monkeypatch.setattr(current_time_mod, "_now", boom)
    obs = await current_time_mod.build_spec().handler(current_time_mod.CurrentTimeArgs())
    assert obs.ok is False
    # 回归修复：形状固定 + 异常原文不外泄
    assert obs.text == "读取本机时间 执行失败（RuntimeError）。"
    assert "clock broken" not in obs.text

    wire = Wire()
    attach_wire(wire, monkeypatch)
    wire.queue = [
        wire_sse_tool_call("current_time", {}, call_id="call_ct_fail"),
        wire_sse_text("可以作答了"),
        wire_sse_text("很抱歉，读取时间失败了。"),
    ]
    events = await run_stream(client, message="现在几点")
    body_text = ""
    if wire.answer_bodies:
        body_text = str(wire.last_user_text(wire.answer_bodies[-1])) + str(wire.answer_bodies[-1])
    # 回归修复：正文只带固定形状；改前这个「或」的右边正是被禁的异常原文
    assert "读取本机时间 执行失败（RuntimeError）。" in body_text
    assert "clock broken" not in body_text
    assert not _STAMP_RE.search(body_text), body_text

    tokens = "".join(e.get("text") or e.get("delta") or "" for e in events if e["type"] == "token")
    done = [e for e in events if e["type"] == "done"]
    delivered = tokens
    if done and isinstance(done[0].get("message"), str):
        delivered = done[0]["message"]
    assert not delivered.startswith(_FROZEN_TIME_PREFIX)


# --------------------------------------------------------------------------
# A6 · 不读无关系统信息
# --------------------------------------------------------------------------


def test_a6_source_scan_current_time_only():
    """A6：限定 ``current_time.py``，目标词不得出现。"""
    src = (Path(current_time_mod.__file__)).read_text(encoding="utf-8")
    for word in ("hostname", "getuser", "getenv", "environ", "platform."):
        assert word not in src, f"current_time.py 不该读取 {word}"
