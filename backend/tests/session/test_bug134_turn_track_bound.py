"""``回归修复`` 判据载体：``session/events.py`` 三个按轮分桶 dict 的**上限与淘汰**。

**缺陷**：``_TURN_FAILURES`` / ``_TURN_INCOMPLETE`` / ``_TURN_REASON`` 三个按 ``(chat_id, turn)`` 分桶的
进程内 dict **只增不减**（生产路径零 ``pop``，清理口只有测试用的 reset/restore）。
当 ``CHAT_EVENT_LOG_ENABLE=false`` 时 :func:`safe_append_event` **每次调用**都会走
``_note_incomplete(turn_key, INCOMPLETE_DISABLED)`` ⇒ **每个轮都在 ``_TURN_REASON`` 留一条永不删除的
痕迹**（每轮 ≈8 个事件）⇒ 键数量随轮数无界增长；写失败 / 取消轮另在另两个桶留条目。

**修法**：三个桶共用**同一个上限** :data:`TURN_TRACK_MAX_KEYS`（512，与同仓
``BREAKER_MAX_TRACKED`` / ``_TURN_LEDGER_MAX_CHATS`` 同值），超限按**最近写入序**淘汰最旧的键
（``dict`` 保序 ⇒ O(1)；一处实现 ``_evict_oldest_turn_keys``）。

⚠️ **为什么不是纯插入序**（本文件第 4 条腿就钉这一点）：纯插入序会把「开得早、**还在写**」的
在途轮挤出去 ⇒ 那一轮的 ``failures_since`` 从 True 回落 False（漏报）。⇒ 每次带归属的记账都把
该键**刷到末尾**（``_mark_turn_recent``），淘汰序 = 最近写入序。

**六条腿**：
1. ``test_three_tracks_share_one_bound`` —— 三桶**同款上限**（同一常量、都收到 512）；
2. ``test_over_bound_evicts_the_oldest_and_keeps_the_newest`` —— 淘汰最旧、新键在、证据完整；
3. ``test_under_bound_semantics_are_unchanged`` —— 🟢 **负控**：未超限时逐字等价
  （按轮归属 / 全局增量 / 无归属不建键 / 计数累加 / 原因不降级）；
4. ``test_in_flight_turn_is_not_evicted`` —— **在途轮不被淘汰**（这条腿分辨「最近写入序」与「纯插入序」）；
5. ``test_disabled_switch_keys_stay_bounded`` —— 缺陷**原始场景**（开关关闭逐轮灌 ``_TURN_REASON``）；
6. ``test_real_write_failure_entry_is_bounded`` —— 真实入口（``safe_append_event`` + 必炸的假 db）。

⚠️ **登记的口径与残余窗口**：淘汰是**漏报**方向的风险（不是误报）—— 某个轮**最后一次写之后**
又发生 512 个其它键的写入时，它的键会被淘汰；若该轮此前的失败也已被淘汰且之后不再写失败，
``failures_since`` 会回落 False。彻底消除要引入在途登记表，**超出本条范围** ⇒ 不得写「已彻底消除」。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.session import events as event_log

#: 上限（从被测模块取，避免载体自己写死一份而与实现漂移）。
#:
#: ⚠️ 刻意用 :func:`getattr` + 默认值：回退击杀实验会把该常量整个摘掉 ⇒ 直接取属性会让本文件
#: **败在收集期**（AttributeError）、逐腿读数全丢。改成防御式取之后，回退时由
#: ``test_bound_constant_is_registered`` **第一条红**（钉存在 + 512），其余各腿各自给红。
BOUND = getattr(event_log, "TURN_TRACK_MAX_KEYS", 512)


@pytest.fixture(autouse=True)
def isolate_turn_tracks():
    """本文件会把三个桶灌到上限 ⇒ 先清零取确定性、结束后用**公开还原口**收拾干净。

    照 ``tests/session/test_event_attribution_4_3.py:58-75`` 的先例（进程级状态泄漏实测踩过）。
    """
    snapshot = event_log.failure_snapshot()
    event_log.reset_event_write_failures()
    yield
    event_log.restore_event_write_failures(snapshot)


def _fail(chat_id: int, turn: int, *, times: int = 1) -> None:
    """走**公开记账口**制造事件写失败（不碰 DB；``note_event_write_failure`` 是记账的唯一实现）。"""
    for _ in range(times):
        event_log.note_event_write_failure(
            chat_id, "turn/end", RuntimeError("boom"), turn_key=(chat_id, turn)
        )


class _BreakingSession:
    """一个「一碰就炸」的假 db：``safe_append_event`` 必走 ``except`` ⇒ 走真实记账口。"""

    def begin_nested(self):
        raise RuntimeError("boom")

    def add(self, _row):
        raise RuntimeError("boom")

    async def flush(self):  # pragma: no cover - begin_nested 先炸，走不到这里
        raise RuntimeError("boom")

    async def execute(self, *_args, **_kwargs):
        raise RuntimeError("boom")


# ── 0 · 常量登记腿（回退击杀时**第一条红**） ─────────────────────────────────


def test_bound_constant_is_registered():
    """上限常量必须**存在**且取 **512**（取值依据 = 同仓另两条先例同值；回退时本腿先红）。"""
    assert hasattr(event_log, "TURN_TRACK_MAX_KEYS"), "上限常量不存在 ⇒ 三个桶仍是只增不减"
    assert event_log.TURN_TRACK_MAX_KEYS == 512


# ── 1 · 三桶同款上限 ──────────────────────────────────────────────────────────


def test_three_tracks_share_one_bound():
    """三个桶共用同一个常量、同一个淘汰实现 ⇒ 灌爆之后三者的规模**同时**收到上限。"""
    assert BOUND == 512, "取值依据 = 同仓 BREAKER_MAX_TRACKED / _TURN_LEDGER_MAX_CHATS 的先例（都 512）"
    for turn in range(BOUND + 5):
        _fail(1, turn)

    snap = event_log.failure_snapshot()
    assert len(snap.turns) == BOUND
    assert len(snap.incomplete) == BOUND
    assert len(snap.reasons) == BOUND


# ── 2 · 淘汰最旧、新键仍在 ───────────────────────────────────────────────────


def test_over_bound_evicts_the_oldest_and_keeps_the_newest():
    """超限时淘汰**最旧**的键；新键与其三桶证据完整；旧键三桶一起走。"""
    for turn in range(BOUND):
        _fail(7, turn)
    assert event_log.turn_write_failures((7, 0)) == 1

    _fail(7, BOUND)  # 第 BOUND+1 个键 ⇒ 触发一次淘汰

    snap = event_log.failure_snapshot()
    assert len(snap.turns) == BOUND and len(snap.incomplete) == BOUND and len(snap.reasons) == BOUND
    # 最旧的那个键在三个桶里都不在了
    assert event_log.turn_write_failures((7, 0)) == 0
    assert event_log.turn_event_incomplete((7, 0)) == (0, event_log.INCOMPLETE_NONE)
    assert (7, 0) not in snap.reasons
    # 新键在，三桶证据完整
    assert event_log.turn_write_failures((7, BOUND)) == 1
    assert event_log.turn_event_incomplete((7, BOUND)) == (1, event_log.INCOMPLETE_WRITE_FAILED)


# ── 3 · 🟢 负控：未超限时逐字等价 ────────────────────────────────────────────


def test_under_bound_semantics_are_unchanged():
    """🟢 **负控腿（回退击杀时本腿必须仍绿）**：没超限时，``回归修复`` 那一套口径一条都不许变。

    钉五条：按轮归属不串轮 / 全局增量旧口径 / 无归属不建任何键 / 同轮计数累加 / 原因只升不降。
    """
    before = event_log.failure_snapshot()
    assert event_log.failures_since(before, turn_key=(9, 1)) is False

    _fail(9, 1)
    assert event_log.failures_since(before, turn_key=(9, 1)) is True       # 本轮自己失败 ⇒ 报
    assert event_log.failures_since(before, turn_key=(9, 2)) is False      # 别的轮不牵连（033 的修法）
    assert event_log.failures_since(before) is True                        # 全局增量旧口径不变
    assert event_log.turn_write_failures((9, 1)) == 1
    assert event_log.turn_event_incomplete((9, 1)) == (1, event_log.INCOMPLETE_WRITE_FAILED)

    # 不带归属 ⇒ 三个桶一个键都不许建（既有口径：宁可少报也不假阳性）
    before_counts = (
        len(event_log.failure_snapshot().turns),
        len(event_log.failure_snapshot().incomplete),
        len(event_log.failure_snapshot().reasons),
    )
    event_log.note_event_write_failure(9, "turn/end", RuntimeError("boom"))
    after_counts = (
        len(event_log.failure_snapshot().turns),
        len(event_log.failure_snapshot().incomplete),
        len(event_log.failure_snapshot().reasons),
    )
    assert after_counts == before_counts

    # 同轮再失败 ⇒ 计数累加；原因不被低优先级的覆盖（优先级口径不动）
    _fail(9, 1)
    assert event_log.turn_write_failures((9, 1)) == 2
    assert event_log.turn_event_incomplete((9, 1)) == (2, event_log.INCOMPLETE_WRITE_FAILED)


# ── 4 · 在途轮不被淘汰 ───────────────────────────────────────────────────────


def test_in_flight_turn_is_not_evicted():
    """🔴 **本腿分辨「最近写入序」与「纯插入序」**：持续写入的在途轮必须活下来。

    纯插入序（不做「写即刷新」）在这里会红：~(5, 0)~ 的键位置最靠前，之后每插一个新键都会被挤掉。
    """
    _fail(5, 0)  # 开得最早的那一轮
    for turn in range(1, BOUND):
        _fail(5, turn)
    assert len(event_log.failure_snapshot().turns) == BOUND

    _fail(5, 0)  # 在途轮又写了一次 ⇒ 刷到「最近写入」末尾
    assert event_log.turn_write_failures((5, 0)) == 2

    for turn in range(BOUND, BOUND + 3):  # 再插 3 个新轮 ⇒ 会淘汰 3 个最旧的键
        _fail(5, turn)

    assert event_log.turn_write_failures((5, 0)) == 2, "持续写入的在途轮不得被淘汰"
    assert event_log.turn_event_incomplete((5, 0)) == (2, event_log.INCOMPLETE_WRITE_FAILED)
    # 被淘汰的正是「最久没写」的那三个
    for turn in (1, 2, 3):
        assert event_log.turn_write_failures((5, turn)) == 0
    assert event_log.turn_write_failures((5, BOUND + 2)) == 1


# ── 5 · 缺陷原始场景：开关关闭 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_switch_keys_stay_bounded(monkeypatch):
    """``CHAT_EVENT_LOG_ENABLE=false``：逐轮记账**仍然有界**（这正是缺陷报告里那条无界路径）。"""
    monkeypatch.setattr(settings, "CHAT_EVENT_LOG_ENABLE", False)
    assert event_log.event_log_enabled() is False

    before = event_log.failure_snapshot()
    for turn in range(BOUND + 10):
        await event_log.safe_append_event(
            SimpleNamespace(), 987654, "turn/start", {}, turn_key=(987654, turn)
        )

    after = event_log.failure_snapshot()
    assert len(after.reasons) <= BOUND, "开关关闭这条路径以前是无界的（每轮一个永不删除的键）"
    assert len(after.incomplete) == len(before.incomplete)   # disabled 不加缺行计数（口径未变）
    assert len(after.turns) == len(before.turns)             # 也不动失败计数桶
    assert event_log.turn_event_incomplete((987654, BOUND + 9)) == (0, event_log.INCOMPLETE_DISABLED)
    assert (987654, 0) not in after.reasons                  # 最旧的那批已被淘汰


# ── 6 · 真实入口 ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_real_write_failure_entry_is_bounded(monkeypatch):
    """真实入口（``safe_append_event`` 隔离写失败）同样受上限约束。"""
    monkeypatch.setattr(settings, "CHAT_EVENT_LOG_ENABLE", True)
    for turn in range(BOUND + 4):
        await event_log.safe_append_event(_BreakingSession(), 11, "turn/end", {}, turn_key=(11, turn))

    snap = event_log.failure_snapshot()
    assert len(snap.turns) == BOUND
    assert len(snap.incomplete) == BOUND
    assert len(snap.reasons) == BOUND
    assert event_log.turn_write_failures((11, BOUND + 3)) == 1
    assert event_log.turn_write_failures((11, 0)) == 0
