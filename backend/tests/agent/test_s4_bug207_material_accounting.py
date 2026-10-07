"""S4 · 回归修复 判据载体：满桶后新片段要么进快照、要么显式告警；materials_hit 必须同源。

## 规格（交接件 §2 · 207）

- FUSE_LIMIT 满了之后，新检到的片段**要么进快照、要么显式告警**（collector.warnings 非空）；
- materials_hit（落库 tool/result 载荷）与**实际进快照/进上下文的条目**同源 ——
  不得把「被丢掉的片段」也算成命中；
- 反向保护：未满桶时的记账不得变（命中数 == 材料条数，且不告警）。

## 缺陷面（runner.py ToolCollector.absorb 分支①）

混合批（一部分 key 已在快照、一部分是新 key）走的是「有可引用材料」分支：
新 key 在 len(self.materials) >= FUSE_LIMIT 处 break 掉、且**不告警**（静默丢）；
同时 materials_hit 取 len(observation.materials)，把没进快照的新 key 也算进去（不同源）。
⇒ 本件把两条分别钉住（两条腿各自独立，RED 原因各自可读）：
  ① 丢就要说（warnings）；② 说到的就是做到的（materials_hit 同源）。

## 读点

materials_hit / warnings 的可见面在 session_events 的 tool/result 载荷里
（runner._write_tool_events -> events.safe_append_event），不在内存对象上 ——
所以本件经**真落库**读回，而不是读 collector 的内部字段。

## 判别力标注（HEAD 实测）

RED：混合批 warnings == []（静默丢）；混合批 materials_hit == 3（应为 1）；
纯新批 materials_hit == 2（应为 0）。反向腿全绿。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agent import runner
from app.agent.runner import ToolCollector
from app.agent.types import ToolObservation
from app.models import Chat, SessionEvent
from app.retrieval.types import FUSE_LIMIT
from tests.agent.test_runner import material


def _fill_bucket(collector: ToolCollector) -> None:
    collector.absorb(
        "s4-207-fill",
        ToolObservation(text="填桶", materials=[material(i) for i in range(1, FUSE_LIMIT + 1)]),
    )
    assert len(collector.materials) == FUSE_LIMIT, "前提：桶必须真被填满"


def _mixed_batch(collector: ToolCollector) -> ToolObservation:
    """1 条已在快照 + 2 条新 key 的混合批（新 key 应被丢弃 ⇒ 必须告警）。"""
    carried = collector.materials[0]
    return ToolObservation(
        text="混合观察", materials=[carried, material(101), material(102)]
    )


async def _record_and_read(db, collector: ToolCollector, call_id: str) -> dict:
    """把一次工具结果落库，读回 tool/result 载荷（materials_hit / warnings 的可见面）。"""
    chat = Chat(title="S4-207 载体")
    db.add(chat)
    await db.flush()
    end = SimpleNamespace(
        type="tool_execution_end",
        tool_call_id=call_id,
        tool_name="knowledge_search",
        is_error=False,
    )
    await runner._write_tool_events(
        [end],
        db=db,
        chat_id=int(chat.id),
        turn_no=1,
        round_no=1,
        trace_id=None,
        collector=collector,
        fallback_id="s4-207-fallback",
    )
    rows = await db.execute(
        select(SessionEvent)
        .where(
            SessionEvent.chat_id == int(chat.id),
            SessionEvent.type == "tool/result",
        )
        .order_by(SessionEvent.seq)
    )
    events = list(rows.scalars().all())
    assert len(events) == 1, "本腿只落一条 tool/result"
    return dict(events[0].data or {})


# ── 正向 discriminator ①：丢就要说 ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_bug207_mixed_batch_warns(db):
    collector = ToolCollector()
    _fill_bucket(collector)

    collector.absorb("s4-207-mixed", _mixed_batch(collector))

    assert collector.warnings, "满桶后新检到的片段被静默丢弃 —— 必须显式告警"
    assert len(collector.materials) == FUSE_LIMIT


@pytest.mark.asyncio
async def test_bug207_mixed_batch_hit_matches_snapshot(db):
    collector = ToolCollector()
    _fill_bucket(collector)

    collector.absorb("s4-207-mixed", _mixed_batch(collector))

    payload = await _record_and_read(db, collector, "s4-207-mixed")
    assert payload["materials_hit"] == 1, (
        "materials_hit 必须与实际进快照的条目同源，实得 " + str(payload["materials_hit"])
    )
    assert payload["warnings"] >= 1, "告警必须一路记到落库载荷"


# ── 正向 discriminator ②：纯新批（既有告警腿 + 同源腿） ──────────────────────

@pytest.mark.asyncio
async def test_bug207_full_bucket_new_keys_warn_and_hit_is_zero(db):
    collector = ToolCollector()
    _fill_bucket(collector)

    collector.absorb(
        "s4-207-new",
        ToolObservation(text="新增观察", materials=[material(201), material(202)]),
    )

    assert collector.rendered["s4-207-new"] == runner.SNAPSHOT_FULL_TEXT
    assert collector.warnings, "满桶纯新片段必须显式告警"
    payload = await _record_and_read(db, collector, "s4-207-new")
    assert payload["materials_hit"] == 0, "一条都没进快照，命中数必须是 0"


# ── 反向保护腿：未满桶口径不得破 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bug207_reverse_within_capacity_hit_is_still_the_entry_count(db):
    collector = ToolCollector()

    collector.absorb(
        "s4-207-ok",
        ToolObservation(text="正常观察", materials=[material(1), material(2), material(3)]),
    )

    assert collector.warnings == [], "未满桶不得凭空告警"
    assert len(collector.materials) == 3
    payload = await _record_and_read(db, collector, "s4-207-ok")
    assert payload["materials_hit"] == 3
    assert payload["warnings"] == 0


@pytest.mark.asyncio
async def test_bug207_reverse_no_material_observation_is_not_flagged(db):
    collector = ToolCollector()

    collector.absorb("s4-207-none", ToolObservation(text="无材料的观察", materials=[]))

    assert collector.warnings == []
    payload = await _record_and_read(db, collector, "s4-207-none")
    assert payload["materials_hit"] == 0
    assert payload["content"]
