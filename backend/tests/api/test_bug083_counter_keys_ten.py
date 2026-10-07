"""`回归修复` 批次三判据载体：**派生计数器键集「八键 → 十键」的独立锚 + 语义腿**。

🔴 **口径（逐字，防误读）**

· 本文件钉的是「**键集只增不改义**」+「**被吞掉的失败真的会动**」，**不是**「派生失败已被消除」；
· 可见面 = `GET /api/agent/audit` 的 **`counters.derived`** —— 该出口**前端零消费**
  （`grep -rn counters frontend/src` = 0 命中）⇒ 正确措辞 = 「**机器可读 + 人工 curl 的可见面**」；
  ⛔ 不得写「用户已能看见派生失败」，⛔ 也不得把可见面写成**日志**
  （冻结态应用层日志不进控制台 = 本账本 `回归修复`）；
· ⛔ 本笔**只改键集口径与判据**（`app/core/derived_counters.py` + 既有钉键集的测试同笔跟随），
  **不接线任何吞点**——接线在 task-13 / task-14 做。

本文件为什么单独存在（**独立锚**纪律）
--------------------------------------
`COUNTER_KEYS` 的键集已经**只增**过三轮（五 → 七 → 八 → 十），每轮都要求「改实现必须显式跟随
钉键集的测试」。`tests/api/test_bug083_s10_breaker.py::EXPECTED_DERIVED_KEYS` 是第一处独立锚；
本文件 `TEN_KEYS` 是**第二处**，且**逐字写死**（故意不从实现里取）：
· 只锚前八键 ⇒ 抓不到「新键被悄悄塞进**中间**」（顺序漂移）；
· 只锚后两键 ⇒ 抓不到「既有八键被改名/删除」。
⇒ 本锚把「**恰十键 + 前八键逐字不动 + 两新键在尾部**」一次性钉死。

腿表
----
| 用例 | 钉什么 |
|---|---|
| `test_key_set_is_exactly_ten_and_first_eight_verbatim` | 恰十键、无重复、前八键**逐字/逐序**不动、两新键在尾部 |
| `test_zero_state_has_all_ten_keys_and_all_zero` | 零态：`ordered_counts()` = 十键全 0；`safe_counters()["derived"]` 同集（读侧形状也稳） |
| `test_two_new_keys_bump_independently` | 两新键**互不串号**：单独 bump 一个，另一个与既有八键都不动 |
| `test_two_new_keys_are_visible_on_the_audit_endpoint` | 两新键经 `GET /api/agent/audit` 的 `counters.derived` **逐值可见** |
| `test_keys_are_additive_only_no_semantics_change` | 十键是**超集**：已有八键的值仍是原值（只增不改义） |
| `test_unknown_key_is_recorded_and_passed_through` | 未知键**照记**且 `ordered_counts()` 透传（不丢信息、也不污染十键） |
| `test_bump_never_raises_and_bad_amount_skips_whole_bump` | `bump` **绝不抛**；坏 `amount` ⇒ **整次跳过**（计数逐字不变） |
| `test_failure_counts_returns_a_copy` | `failure_counts()` 返回**副本**（调用方改不动内部状态） |
| `test_audit_static_fallback_table_matches_keys_and_order` | `audit.COUNTER_KEYS["derived"]` 与权威表**同集同序**（读失败时的形状不塌缩） |
| `test_audit_read_failure_falls_back_to_ten_keys_without_raising` | 读侧**真炸**时回落**十键全 0**且不抛（`safe_counters` 的 I5 同型兜底） |
"""

from __future__ import annotations

import pytest

from app.core import derived_counters
from tests.api.test_chat_contract import client  # noqa: F401


#: ① **独立锚（逐字写死，不从实现取）**：本批键集必须**恰**是这十个，且顺序即此序。
TEN_KEYS = (
    "note_links",
    "doc_links",
    "compaction_summary",
    "usage_events",
    "graph_view_edges",
    "pdf_ocr_fallback",
    "web_fetch_degrade",
    "stale_vector_points",
    "trace_persist",
    "tool_result_channel",
)

#: ② 批次三**之前**的八键（逐字，用于钉「只增不改义」）。
BEFORE_BATCH3_KEYS = TEN_KEYS[:8]

#: ③ 本批新增两键（语义 = Lead 定稿键法）。
NEW_KEYS = ("trace_persist", "tool_result_channel")


@pytest.fixture(autouse=True)
def _reset_derived_counters():
    """进程全局计数跨用例泄漏是本仓踩过的坑 ⇒ 每例前后清零。"""
    derived_counters.reset_failure_counts()
    yield
    derived_counters.reset_failure_counts()


# --------------------------------------------------------------------------
# 键集（独立锚）
# --------------------------------------------------------------------------


def test_key_set_is_exactly_ten_and_first_eight_verbatim():
    """① 键集：**恰十键**、无重复、前八键逐字逐序不动、两新键在**尾部**。

    ⚠️ 这条腿的判别力：删键 / 改名 / 插到中间 / 多塞一个 —— 四种漂移全部当场红。
    """
    keys = tuple(derived_counters.COUNTER_KEYS)
    assert keys == TEN_KEYS, f"键集漂移：实现={keys} 独立锚={TEN_KEYS}"
    assert len(keys) == 10, f"本批键集必须恰十键，现读 {len(keys)}"
    assert len(set(keys)) == 10, f"键集有重复：{keys}"
    assert keys[:8] == BEFORE_BATCH3_KEYS, (
        "既有八键的**名/义/顺序**必须逐字不动（只增不改义）："
        f"{keys[:8]} vs {BEFORE_BATCH3_KEYS}"
    )
    assert keys[8:] == NEW_KEYS, f"两新键必须追加在尾部：{keys[8:]} vs {NEW_KEYS}"


def test_zero_state_has_all_ten_keys_and_all_zero():
    """② 零态：`ordered_counts()` = 十键全 0；`safe_counters()["derived"]` **同集**。"""
    from app.agent.audit import safe_counters

    counts = derived_counters.ordered_counts()
    assert tuple(counts) == TEN_KEYS, f"键序/键集漂移：{tuple(counts)}"
    assert counts == dict.fromkeys(TEN_KEYS, 0), counts

    derived = safe_counters()["derived"]
    assert set(derived) == set(TEN_KEYS), f"读侧形状与权威键集不同集：{sorted(derived)}"
    assert all(value == 0 for value in derived.values()), derived


# --------------------------------------------------------------------------
# 语义（两新键）
# --------------------------------------------------------------------------


def test_two_new_keys_bump_independently():
    """③ 两新键**互不串号**：单独 bump 一个 ⇒ 另一个与既有八键**都不动**。"""
    derived_counters.bump("trace_persist")
    counts = derived_counters.ordered_counts()
    assert counts["trace_persist"] == 1, counts
    assert counts["tool_result_channel"] == 0, "bump 一个键不该带动另一个键"
    assert all(counts[key] == 0 for key in BEFORE_BATCH3_KEYS), counts

    derived_counters.bump("tool_result_channel", 2)
    counts = derived_counters.ordered_counts()
    assert counts["tool_result_channel"] == 2, counts
    assert counts["trace_persist"] == 1, "bump 另一个键不该回写先前那个键"


@pytest.mark.asyncio
async def test_two_new_keys_are_visible_on_the_audit_endpoint(client, db):
    """④ 进可见面：两新键在 `GET /api/agent/audit` 的 `counters.derived` 里**逐值可见**。

    可见面口径 = **机器可读 + 人工 curl**（前端零消费）⇒ 本腿只钉接口读数，⛔ 不代表用户可见。
    """
    derived_counters.bump("trace_persist", 3)
    derived_counters.bump("tool_result_channel", 1)

    resp = await client.get("/api/agent/audit")
    assert resp.status_code == 200, resp.text
    derived = (resp.json().get("counters") or {}).get("derived") or {}
    assert set(derived) == set(TEN_KEYS), f"counters.derived 形状漂移：{sorted(derived)}"
    assert derived["trace_persist"] == 3, derived
    assert derived["tool_result_channel"] == 1, derived
    assert all(derived[key] == 0 for key in BEFORE_BATCH3_KEYS), derived


def test_keys_are_additive_only_no_semantics_change():
    """⑤ 只增不改义：已有八键的读数**原样**保留（新键不是它们的别名/替代）。"""
    derived_counters.bump("note_links", 5)
    derived_counters.bump("doc_links", 7)
    counts = derived_counters.ordered_counts()
    assert counts["note_links"] == 5 and counts["doc_links"] == 7, counts
    assert counts["trace_persist"] == 0 and counts["tool_result_channel"] == 0, (
        "既有键计数不该被算到新键头上（真子集链上只增不改义）"
    )


def test_unknown_key_is_recorded_and_passed_through():
    """⑥ 未知键**照记**（不丢信息），且 `ordered_counts()` 透传时**不污染**十键。"""
    derived_counters.bump("not_a_declared_key", 4)
    raw = derived_counters.failure_counts()
    assert raw.get("not_a_declared_key") == 4, raw

    counts = derived_counters.ordered_counts()
    assert counts["not_a_declared_key"] == 4, counts
    assert all(counts[key] == 0 for key in TEN_KEYS), counts


def test_bump_never_raises_and_bad_amount_skips_whole_bump():
    """⑦ `bump` **绝不抛**（观测项不该炸穿被观测的调用）；坏 `amount` ⇒ **整次跳过**。"""
    derived_counters.bump("trace_persist", 2)
    before = derived_counters.failure_counts().copy()

    derived_counters.bump("trace_persist", object())  # type: ignore[arg-type]  # int() 必炸 ⇒ 整次跳过
    derived_counters.bump("tool_result_channel", "not-a-number")  # type: ignore[arg-type]

    after = derived_counters.failure_counts()
    assert after == before, f"坏 amount 不该改动任何计数：{before} → {after}"


def test_failure_counts_returns_a_copy():
    """⑧ `failure_counts()` 返回**副本**：调用方改它不改内部状态（与既有三个计数器同型）。"""
    derived_counters.bump("trace_persist")
    snapshot = derived_counters.failure_counts()
    snapshot["trace_persist"] = 999
    snapshot["tool_result_channel"] = 999
    assert derived_counters.failure_counts()["trace_persist"] == 1
    assert derived_counters.failure_counts().get("tool_result_channel", 0) == 0


# --------------------------------------------------------------------------
# 读侧回退（audit 静态表 + 读失败兜底）
# --------------------------------------------------------------------------


def test_audit_static_fallback_table_matches_keys_and_order():
    """⑨ `audit.COUNTER_KEYS["derived"]` 与权威表**同集同序**。

    为什么还要一条（既有 `test_bug107...::test_audit_derived_fallback_matches_authoritative_keys`
    只断言**同集**）：`safe_counters()` 读失败时是按**这张表**补 0 ⇒ 顺序不一致会让
    `counters.derived` 的**键序**在「读成功」与「读失败」两种路径上不同（同接口两种形状）。
    """
    from app.agent import audit as agent_audit

    fallback = tuple(agent_audit.COUNTER_KEYS["derived"])
    assert fallback == TEN_KEYS, f"audit 回退表漂移：{fallback} vs {TEN_KEYS}"
    assert fallback == tuple(derived_counters.COUNTER_KEYS), (
        "audit 回退表与权威表必须同集**同序**（补键时两处同笔跟随）"
    )


def test_audit_read_failure_falls_back_to_ten_keys_without_raising(monkeypatch):
    """⑩ 读侧**真炸**时：回落**十键全 0**且**绝不抛**（I5 同型兜底）。

    这条腿的判别力：只补权威表、忘了补回退表 ⇒ 这里读到的是**八键**（形状塌缩），当场红。
    """
    from app.agent import audit as agent_audit

    def _boom() -> dict:
        raise RuntimeError("注入的计数读取失败（本用例故意）")

    monkeypatch.setattr(agent_audit, "_read_derived_counters", _boom)
    derived = agent_audit.safe_counters()["derived"]
    assert tuple(derived) == TEN_KEYS, f"读失败时形状塌缩：{tuple(derived)}"
    assert all(value == 0 for value in derived.values()), derived
