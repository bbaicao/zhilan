"""回归修复 独立对抗验证载体（非作者 · fix-contract · task-16）。

## 为什么另写一件（不抄实施者写法）

实施者载体（tests/api/test_bug149_seq_atomic.py）证明的是「他写的那套判据在他改的代码上绿」。
本件的任务相反：先证明缺陷真的可被复现（在改好的代码上把分配压回改前形态），
再用独立构造（不共享他的同步手法、不 import 他的夹具）钉住修复面，
最后给出「判别力来自重叠」的反证腿。

## 腿构成（13 例）

| # | 腿 | 判别力 / 性质 |
|---|---|---|
| 1 | 强制陈旧读（台账回落 + 库值恒定）=> 第二/三条事件被 safe_append_event 吞 | 改前态复现（改前/改后都必须绿 —— 它证明缺陷形态可判，**不承担修复面判别力**） |
| 2 | 同 chat 真并发两轮（甲整组 flush 未提交时乙读并写）=> 两组各 3 条齐 + 两轮 seq 交集空 | 改前 / 台账被绕 => 乙组整组丢（红） |
| 3 | 并发后 seq 无空洞且与库内 MAX 自洽（下一条 = 行数） | 台账「既给号也不复用」要求 |
| 4 | 回落支路（_seq_ledger_of -> None）串行读数与台账态逐字相同、且原样返回库值 | 修复面判据（结构腿）：台账在时回落 == 纯库值；回退态红（无钩子） |
| 5 | 台账代际安全阀：库值 < 基准 => 重新起算（不把旧高水位顶上来） | 结构腿（清表 / SQLite rowid 复用场景） |
| 6 | 跨引擎不共用台账（两台引擎各算同号）+ _SEQ_LEDGER 按引擎分表 | 残余窗口登记腿 1（多进程仍可能撞号） |
| 7 | 重试恰 3 次有界（spy 计数）且文案说的是三次冲突 | 防「无限重试」与「次数漂移」 |
| 8 | 分配面异常仍走记账出口：全局计数 + 轮桶 + write_failed + 邻轮不受污染 | 失败出口负控 |
| 9 | 顺序 await 两轮 6 条事件全落库、零失败 | 对照腿：旧实现下同样绿 => 2 的红来自「重叠」而非环境 |
| 10 | _next_seq 常量化但台账在时不再撞号（回头证明 test_decision_log 那处适配是必需的） | 修复面判据（结构腿）：适配必要性反证；回退态红（无钩子） |
| 11 | 台账临界区无 await（inspect.iscoroutinefunction 为假） | 口径钉：单事件循环内不可被切走 |
| 12 | 分配只发一条 SELECT、不含任何行锁语句、不写库 | 口径钉：不引入行锁 / 不跨请求持锁 |
| 13 | 台账上限与先例同值（512）+ 超限淘汰最旧、刚写的那条不被自己淘汰 | 口径钉（与 120/134 同口径） |

## 与「改前」的关系（不得含糊）

回退态（旧实现 = 无 _seq_ledger_of/_reserve_seq/_SEQ_LEDGER 钩子）实测 11 failed / 2 passed：
**唯一在修好态与回退态都绿的是腿 1（强制陈旧读复现缺陷形态）与腿 9（顺序 await 对照腿）**。
腿 1 证明的是「缺陷形态可判」——它**不承担「修复已完成」的判别力**（修好后它同样绿，这正是假绿尝试②），
腿 9 证明「腿 2 的红来自重叠而非环境」。修复面判据 = 腿 2/3/4/5/6/7/8/10/11/12/13；
回退态下红的是 2（乙组整组丢）、3（seq 依赖台账）、4/10（台账钩子不存在，腿的前提无法求值）、
5/6/7/8/11/12/13（结构钩子不存在）。回退击杀读数见交付报告，本文件只负责「红在哪、为什么红」。

## 环境

所有 python 进程必须走 _bugfix8-scratch/with_lock.ps1（backend/tests/conftest.py::_schema
是 session 级 drop_all + create_all，两个 pytest 并发会互删表）。双方言各跑一遍：
默认 MySQL 档 + DB_BACKEND=sqlite（桌面档）。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models import Chat, SessionEvent
from app.session import events as event_log

#: 并发腿的墙钟上限（正常实现毫秒级完成；超时 = 卡在未提交唯一键/写锁上 => 红而不是挂死）。
_WAIT_TIMEOUT = 30.0

#: MySQL 会话级锁等待上限（对抗跑测专用）：把「撞未提交唯一键」的阻塞上限压到 3s，
#: 否则回退态 / 变异态在 MySQL 默认 50s 的 innodb_lock_wait_timeout 下会把整轮跑测拖死。
_MYSQL_LOCK_WAIT_SECONDS = 3


@pytest.fixture(autouse=True)
def _event_log_on(monkeypatch):
    """事件开关钉开（默认即开，钉住是为了不受机器 .env 影响）+ 每例前后清账。"""
    monkeypatch.setattr(settings, "CHAT_EVENT_LOG_ENABLE", True)
    event_log.reset_event_write_failures()
    try:
        yield
    finally:
        event_log.reset_event_write_failures()


def _session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _mysql_short_lock_wait(session: AsyncSession) -> None:
    """MySQL 档把本会话的锁等待压到 3s（SQLite 档不适用，静默跳过）。"""
    if settings.is_sqlite:
        return
    try:
        await session.execute(text("SET SESSION innodb_lock_wait_timeout = " + str(_MYSQL_LOCK_WAIT_SECONDS)))
    except Exception:  # noqa: BLE001 —— 压不住就退回默认，绝不因为对抗跑测的设置炸掉用例
        pass


async def _make_chat(db: AsyncSession) -> tuple[Chat, int]:
    chat = Chat(title="149 对抗验证")
    db.add(chat)
    await db.commit()
    await db.refresh(chat)
    return chat, int(chat.id)  # 主键先取出：_rows() 里的 rollback 会让 ORM 对象过期


async def _rows(db: AsyncSession, chat_id: int) -> list[tuple[int, int | None, str]]:
    """换一个干净快照读库（并发写发生在别的连接上 => 必须先结束本会话的在途事务）。"""
    await db.rollback()
    result = await db.execute(
        select(SessionEvent.seq, SessionEvent.turn, SessionEvent.type)
        .where(SessionEvent.chat_id == chat_id)
        .order_by(SessionEvent.seq)
    )
    return [(int(seq), turn, str(kind)) for seq, turn, kind in result.all()]


def _require_ledger_hooks() -> None:
    """实施面钩子必须在位；不在位就响亮地红（而不是静默退化成空跑）。"""
    missing = [name for name in ("_seq_ledger_of", "_reserve_seq", "_SEQ_LEDGER") if not hasattr(event_log, name)]
    if missing:
        cands = sorted(
            name
            for name in dir(event_log)
            if ("seq" in name.lower() or "ledger" in name.lower()) and not name.startswith("__")
        )
        raise AssertionError(
            "回归修复 的实现钩子缺失：%r；模块内 seq/ledger 相关名字 = %r"
            "（要么实现换了名字 => 本载体必须跟着改写；要么当前是回退态 => 修复面腿本就该红）" % (missing, cands)
        )


def _force_stale_read(monkeypatch, *, stale: int = 7) -> dict[str, str]:
    """把 seq 分配压成改前形态：台账回落（_seq_ledger_of -> None）+ 库内读数恒定。

    这不靠调度巧合：改前的缺陷就是「两个并发轮各自读到同一个陈旧 MAX(seq)」，
    把那个读数钉成常量等价于把竞态窗口的条件固定下来。台账缺失（= 回退态旧代码）时本函数
    同样成立（只钉 _next_seq），所以腿 1 在改前改后都可复跑。
    """
    applied: dict[str, str] = {}
    if hasattr(event_log, "_seq_ledger_of"):
        monkeypatch.setattr(event_log, "_seq_ledger_of", lambda _db: None)
        applied["_seq_ledger_of"] = "-> None（回落支路 = 纯库值）"
    else:
        applied["_seq_ledger_of"] = "不存在（回退态 = 改前代码）"

    async def _stale_next_seq(_db, _chat_id):  # noqa: ANN001 —— 等价于并发轮读到的同一个陈旧值
        return stale

    monkeypatch.setattr(event_log, "_next_seq", _stale_next_seq)
    applied["_next_seq"] = "-> 常量 " + str(stale)
    return applied


async def _append_group(
    session: AsyncSession, chat_id: int, turn: int, *, swallow: bool
) -> list[int | None]:
    """一轮的 3 条事件（turn/start + user/message + turn/end）；swallow 决定走隔离版。"""
    calls = [
        ("turn/start", {"turn": turn}, None),
        ("user/message", {"text": "第%d轮提问" % turn}, "append"),
        ("turn/end", {"turn": turn}, None),
    ]
    out: list[int | None] = []
    for event_type, data, surface_op in calls:
        kwargs: dict[str, Any] = {"turn": turn}
        if surface_op is not None:
            kwargs["surface_op"] = surface_op
        if swallow:
            kwargs["turn_key"] = (chat_id, turn)
            out.append(await event_log.safe_append_event(session, chat_id, event_type, data, **kwargs))
        else:
            out.append(await event_log.append_event(session, chat_id, event_type, data, **kwargs))
    return out


async def _two_turns_overlapped(engine: AsyncEngine, chat_id: int, monkeypatch) -> dict[str, Any]:
    """确定性重叠构造：甲把整组 3 条 flush 完（事务未提交） -> 乙完成首次库内读数 -> 甲才提交。

    - 重叠不靠 sleep：甲卡在 b_read_done 上等乙的信号；
    - 乙的读发生在甲的未提交窗口内（真窗口，不是「串行跑两遍」）；
    - 乙用 safe_append_event（= 生产调用形态，失败会被吞），这样「丢没丢」看返回值与行数。
    """
    factory = _session_factory(engine)
    a_group_written = asyncio.Event()
    b_read_done = asyncio.Event()
    trace: dict[str, Any] = {"a_committed_at": None, "b_read_before_a_commit": None}

    real_next_seq = event_log._next_seq

    async def _next_seq_spy(db: AsyncSession, cid: int) -> int:
        value = await real_next_seq(db, cid)
        task = asyncio.current_task()
        if task is not None and task.get_name() == "adv-B" and not b_read_done.is_set():
            trace["b_read_before_a_commit"] = trace["a_committed_at"] is None
            b_read_done.set()
        return int(value)

    monkeypatch.setattr(event_log, "_next_seq", _next_seq_spy)

    async def turn_a() -> list[int | None]:
        async with factory() as session:
            await _mysql_short_lock_wait(session)
            seqs = await _append_group(session, chat_id, 1, swallow=False)
            a_group_written.set()
            await asyncio.wait_for(b_read_done.wait(), _WAIT_TIMEOUT)
            await session.commit()
            trace["a_committed_at"] = time.monotonic()
            return seqs

    async def turn_b() -> list[int | None]:
        await asyncio.wait_for(a_group_written.wait(), _WAIT_TIMEOUT)
        try:
            async with factory() as session:
                await _mysql_short_lock_wait(session)
                seqs = await _append_group(session, chat_id, 2, swallow=True)
                await session.commit()
                return seqs
        finally:
            b_read_done.set()  # 甲不许被乙的任何出口挂住

    a_task = asyncio.create_task(turn_a(), name="adv-A")
    b_task = asyncio.create_task(turn_b(), name="adv-B")
    try:
        a_seqs, b_seqs = await asyncio.wait_for(asyncio.gather(a_task, b_task), _WAIT_TIMEOUT)
    except asyncio.TimeoutError:
        a_task.cancel()
        b_task.cancel()
        a_group_written.set()
        b_read_done.set()
        raise AssertionError(
            "并发两轮在 %.0fs 内没有收尾 => 卡在未提交唯一键/写锁上"
            "（时序：甲整组未提交 + 乙写同 chat）；trace=%r" % (_WAIT_TIMEOUT, trace)
        ) from None
    return {"a": a_seqs, "b": b_seqs, "trace": trace}


# ==========================================================================
# 1 改前态复现：强制陈旧读 => 整组事件被吞
# ==========================================================================


@pytest.mark.asyncio
async def test_强制陈旧读复现整组事件被吞(db, engine, monkeypatch):
    """腿 1（改前态复现，改前/改后都必须绿）：把分配压回「同一陈旧库值」后，
    同一 chat 的第二/三条事件的 INSERT 连续 3 次撞同一 seq => EventError =>
    safe_append_event 吞成 warning 并返回 None => 整组只剩第一条 —— 正是缺陷报告的原始形态。

    判别力：若哪天 safe_append_event 不再吞 / 记账口被摘掉，本腿会红。
    """
    chat, chat_id = await _make_chat(db)
    applied = _force_stale_read(monkeypatch, stale=7)
    assert "_next_seq" in applied, "强制陈旧读没有生效（钩子没打上）"

    before = event_log.failure_snapshot()
    factory = _session_factory(engine)
    async with factory() as session:
        seqs = await _append_group(session, chat_id, 1, swallow=True)
        await session.commit()

    assert seqs[0] == 7, f"第一条应拿到被钉死的陈旧值 7，实得 {seqs[0]}"
    assert seqs[1:] == [None, None], f"第二/三条必须被 safe_append_event 吞成 None（改前形态），实得 {seqs}"

    rows = await _rows(db, int(chat_id))
    assert len(rows) == 1, f"库里应只剩第一条事件，实得 {len(rows)} 行：{rows}"
    assert event_log.event_write_failures() == before.total + 2, "两次失败必须进全局计数"

    key = (int(chat_id), 1)
    assert event_log.failures_since(before, turn_key=key) is True, "轮桶必须记到本轮缺行"
    assert event_log.turn_write_failures(key) == 2, "两条被吞的事件各记一次"
    assert event_log.turn_event_incomplete(key) == (2, event_log.INCOMPLETE_WRITE_FAILED), (
        "缺行原因必须是 write_failed（不许静默 ok）"
    )


# ==========================================================================
# 2 修复面主腿：真并发两轮，两组事件齐 + 两轮 seq 交集空
# ==========================================================================


@pytest.mark.asyncio
async def test_真并发两轮两组事件齐且seq交集空(db, engine, monkeypatch):
    """腿 2（主腿）：甲整组未提交时乙读并写 => 乙不撞甲的 seq 档位（台账在分配阶段就让开）。

    断言三件：1) 两轮各 3 条事件都在库里；2) 两轮 seq 集合交集为空；3) 重叠是真的
    （trace 证明乙的读数发生在甲提交之前）。
    台账被绕（回退态 / 把临界区去掉）=> 乙组整组被吞 => 本腿红。
    """
    _require_ledger_hooks()
    chat, chat_id = await _make_chat(db)
    before = event_log.failure_snapshot()

    result = await _two_turns_overlapped(engine, int(chat_id), monkeypatch)
    a_seqs = [seq for seq in result["a"] if seq is not None]
    b_seqs = [seq for seq in result["b"] if seq is not None]

    trace = result["trace"]
    assert trace["b_read_before_a_commit"] is True, (
        f"乙的库内读数不在甲的未提交窗口内 => 本腿退化成串行，判据无效。trace={trace}"
    )

    assert len(a_seqs) == 3 and len(b_seqs) == 3, (
        f"两轮各 3 条事件都必须成功（None = 被 safe_append_event 吞）：甲={result['a']} 乙={result['b']}"
    )
    assert not (set(a_seqs) & set(b_seqs)), f"两轮 seq 撞车：甲={a_seqs} 乙={b_seqs}"

    rows = await _rows(db, int(chat_id))
    assert len(rows) == 6, f"库里应有 6 行（两轮各 3），实得 {len(rows)}：{rows}"
    by_turn: dict[int | None, list[str]] = {}
    for _seq, turn, kind in rows:
        by_turn.setdefault(turn, []).append(kind)
    assert sorted(by_turn.get(1, [])) == ["turn/end", "turn/start", "user/message"], by_turn
    assert sorted(by_turn.get(2, [])) == ["turn/end", "turn/start", "user/message"], by_turn
    assert event_log.failures_since(before) is False, "并发两轮不该有任何事件写失败"
    assert event_log.turn_write_failures((int(chat_id), 1)) == 0
    assert event_log.turn_write_failures((int(chat_id), 2)) == 0


# ==========================================================================
# 3 seq 无空洞 + 与库内 MAX 自洽
# ==========================================================================


@pytest.mark.asyncio
async def test_两轮seq无空洞且与库内MAX自洽(db, engine, monkeypatch):
    """腿 3：并发两轮之后 6 个 seq 恰是 0..5（无空洞、无复用），且下一条分配 = 行数。"""
    _require_ledger_hooks()
    chat, chat_id = await _make_chat(db)
    result = await _two_turns_overlapped(engine, int(chat_id), monkeypatch)
    all_seqs = sorted(int(seq) for seq in (list(result["a"]) + list(result["b"])) if seq is not None)

    assert all_seqs == list(range(6)), f"6 条事件的 seq 应恰为 0..5（无空洞），实得 {all_seqs}"

    rows = await _rows(db, int(chat_id))
    assert [seq for seq, _turn, _kind in rows] == list(range(6)), rows

    factory = _session_factory(engine)
    async with factory() as session:
        nxt = await event_log.append_event(session, chat_id, "retrieval/start", {"question": "下一轮"}, turn=3)
        await session.commit()
    assert nxt == 6, f"库里 6 行 => 下一条应得 6（与 MAX 自洽），实得 {nxt}"


# ==========================================================================
# 4 回落支路等价改前行为
# ==========================================================================


@pytest.mark.asyncio
async def test_回落支路串行读数与台账态逐字相同(db, engine, monkeypatch):
    """腿 4：_seq_ledger_of 回落（None）时串行读数与台账态逐字相同，
    且 _reserve_seq(None, ...) 原样返回库值 => 取不到引擎的会话与改前行为完全一致。"""
    _require_ledger_hooks()
    chat, chat_id = await _make_chat(db)
    factory = _session_factory(engine)

    async with factory() as session:
        on_seqs = await _append_group(session, chat_id, 1, swallow=False)
        await session.commit()

    monkeypatch.setattr(event_log, "_seq_ledger_of", lambda _db: None)
    async with factory() as session:
        off_seqs = await _append_group(session, chat_id, 2, swallow=False)
        await session.commit()

    assert on_seqs == [0, 1, 2], f"台账态串行读数应为 0/1/2，实得 {on_seqs}"
    assert off_seqs == [3, 4, 5], f"回落态应等价改前的 MAX+1（3/4/5），实得 {off_seqs}"

    assert event_log._reserve_seq(None, int(chat_id), 11) == 11, "回落支路必须原样返回库值"
    assert event_log._reserve_seq({}, int(chat_id), 11) == 11, "空台账首答也必须原样返回库值"


# ==========================================================================
# 5 台账代际安全阀
# ==========================================================================


@pytest.mark.asyncio
async def test_台账代际安全阀_库值小于基准时重新起算(db, engine):
    """腿 5：库内读数比基准更小（清表 / SQLite 档 rowid 复用）=> 重新起算，
    不让旧高水位把新会话的 seq 顶上去。"""
    _require_ledger_hooks()
    chat, chat_id = await _make_chat(db)
    factory = _session_factory(engine)

    for seq in range(5):  # 先让库里已有 0..4（模拟「本代人」既有事件）
        db.add(SessionEvent(chat_id=chat_id, seq=seq, turn=0, type="turn/start", data={"seed": seq}))
    await db.commit()

    async with factory() as session:
        first = await event_log.append_event(session, chat_id, "turn/start", {"turn": 9}, turn=9)
        await session.commit()
    assert first == 5, f"库里已有 0..4 => 首个分配应为 5，实得 {first}"

    # 模拟「表被清过 / rowid 复用」：把该会话的行删空（事件行只随会话级联删，测试里显式删）
    await db.execute(delete(SessionEvent).where(SessionEvent.chat_id == chat_id))
    await db.commit()

    async with factory() as session:
        restarted = await event_log.append_event(session, chat_id, "turn/start", {"turn": 10}, turn=10)
        await session.commit()
    assert restarted == 0, (
        f"库值(0) < 基准(5) => 安全阀必须重新起算得 0，实得 {restarted}"
        "（实得 6 = 旧高水位把新会话顶上去，安全阀失效）"
    )


# ==========================================================================
# 6 残余窗口登记腿 1：跨引擎（约等于跨进程）不共用台账
# ==========================================================================


@pytest.mark.asyncio
async def test_跨引擎不共用台账_跨进程残余窗口登记(db, engine, test_async_url):
    """腿 6（登记腿）：台账按 AsyncEngine 分表 => 另一台引擎（约等于另一个后端进程）
    看不到本进程的预留，两台引擎对同一 chat 算出同一个 seq => 多进程部署仍可能撞号。

    本腿绿 = 该残余窗口确实存在且被写明（跨进程闭合需要持久化计数器列 + 迁移，本批 scope 外）。
    """
    _require_ledger_hooks()
    chat, chat_id = await _make_chat(db)

    other = create_async_engine(test_async_url, pool_pre_ping=True)
    if settings.is_sqlite:
        from app.db.session import apply_sqlite_pragmas

        apply_sqlite_pragmas(other.sync_engine)
    try:
        mine = event_log._seq_ledger_of(db)
        async with _session_factory(other)() as other_session:
            theirs = event_log._seq_ledger_of(other_session)
        assert mine is not None and theirs is not None, "两台引擎都应取到各自台账"
        assert mine is not theirs, "跨引擎共用同一份台账 => 单测会跨用例泄漏，且残余窗口口径失真"

        assert event_log._reserve_seq(mine, chat_id, 0) == 0, "本引擎先占 0"
        assert event_log._reserve_seq(theirs, chat_id, 0) == 0, (
            "另一台引擎应看不到本引擎的预留（=> 跨进程仍撞号，本腿钉的就是这条窗口）"
        )
        assert event_log._reserve_seq(mine, chat_id, 0) == 1, "本引擎的第二次分配必须避开已占的 0"
        assert len(event_log._SEQ_LEDGER) >= 2, "台账必须按引擎分表（至少两把键）"
    finally:
        await other.dispose()


# ==========================================================================
# 7 有界性：重试恰 3 次
# ==========================================================================


@pytest.mark.asyncio
async def test_重试恰三次有界且文案说是三次冲突(db, engine, monkeypatch):
    """腿 7：连续撞同一 seq => 恰 3 次分配尝试后抛 EventError（不许无限重试）。

    构造：库里已提交 seq=0 的行 + 强制陈旧读（台账回落 + 库值恒定 0）=> 每一次尝试都撞 0。
    判别力：for attempt in (1,2,3) 被改成 2 次 / while True（超时）都会红。
    """
    _require_ledger_hooks()
    chat, chat_id = await _make_chat(db)
    factory = _session_factory(engine)

    async with factory() as session:
        await event_log.append_event(session, chat_id, "turn/start", {"turn": 0}, turn=0)
        await session.commit()

    attempts: list[int] = []

    def _spy_reserve(_ledger, _chat_id, db_next):  # noqa: ANN001 —— 计数 + 钉同一档位
        attempts.append(int(db_next))
        return 0

    monkeypatch.setattr(event_log, "_seq_ledger_of", lambda _db: None)
    monkeypatch.setattr(event_log, "_reserve_seq", _spy_reserve)

    async with factory() as session:
        with pytest.raises(event_log.EventError) as excinfo:
            await event_log.append_event(session, chat_id, "retrieval/start", {"question": "q"})
        await session.rollback()

    message = str(excinfo.value)
    assert len(attempts) == 3, f"重试必须恰 3 次（有界），实得 {len(attempts)} 次：{attempts}"
    assert "三次冲突" in message, f"文案必须说清是有界的三次冲突，实得：{message!r}"
    assert "request_id" not in message, "seq 冲突不该被说成 request_id 冲突"

    rows = await _rows(db, int(chat_id))
    assert [seq for seq, _t, _k in rows] == [0], f"撞约束的那条确实没落库，实得 {rows}"


# ==========================================================================
# 8 失败出口负控：分配面异常仍走记账出口
# ==========================================================================


@pytest.mark.asyncio
async def test_分配面异常仍走记账出口且邻轮不受污染(db, engine, monkeypatch):
    """腿 8（负控）：分配面抛异常（非 IntegrityError）时 safe_append_event 必须
    返回 None 并记账（全局计数 + 本轮的 write_failed），邻轮的桶不许被污染。"""
    _require_ledger_hooks()
    chat, chat_id = await _make_chat(db)
    factory = _session_factory(engine)

    async def _boom(_db, _chat_id):
        raise RuntimeError("对抗探针：分配面炸了")

    monkeypatch.setattr(event_log, "_next_seq", _boom)
    before = event_log.failure_snapshot()
    key = (int(chat_id), 1)
    other_key = (int(chat_id), 2)

    async with factory() as session:
        result = await event_log.safe_append_event(
            session, chat_id, "turn/start", {"turn": 1}, turn=1, turn_key=key
        )
        await session.rollback()

    assert result is None, "分配面异常必须被隔离成 None（I5 不炸穿），而不是抛出去"
    assert event_log.event_write_failures() == before.total + 1, "全局计数必须 +1"
    assert event_log.failures_since(before) is True, "全局增量必须看得见这次失败"
    assert event_log.failures_since(before, turn_key=key) is True, "本轮桶必须记到"
    assert event_log.turn_write_failures(key) == 1, "本轮桶计数必须恰 1"
    assert event_log.turn_event_incomplete(key) == (1, event_log.INCOMPLETE_WRITE_FAILED)
    assert event_log.turn_write_failures(other_key) == 0, "邻轮桶不许被污染（按轮归属是硬要求）"
    assert event_log.failures_since(before, turn_key=other_key) is False
    assert await _rows(db, int(chat_id)) == [], "失败的那条不该落库（缺行）"


# ==========================================================================
# 9 对照腿：顺序 await 两轮 —— 旧实现下同样绿
# ==========================================================================


@pytest.mark.asyncio
async def test_顺序两轮在旧实现下同样绿_判别力来自重叠(db, engine):
    """腿 9（对照腿）：不并发、不 patch 任何东西，顺序写两轮 6 条 => 全落库、零失败。

    本腿在回退态（旧实现）下同样绿 => 腿 2 的红只可能来自「重叠」这一条件，不是环境噪声；
    它同时是「把并发腿写成串行 = 假绿」的反例（串行跑永远测不出本条缺陷）。
    """
    chat, chat_id = await _make_chat(db)
    before = event_log.failure_snapshot()
    factory = _session_factory(engine)

    for turn in (1, 2):
        async with factory() as session:
            seqs = await _append_group(session, chat_id, turn, swallow=False)
            await session.commit()
        assert seqs == [3 * (turn - 1), 3 * (turn - 1) + 1, 3 * (turn - 1) + 2], seqs

    rows = await _rows(db, int(chat_id))
    assert len(rows) == 6, f"顺序两轮应 6 行全落库，实得 {len(rows)}：{rows}"
    assert [seq for seq, _t, _k in rows] == list(range(6)), rows
    assert event_log.failures_since(before) is False, "顺序场景不该有任何写失败"


# ==========================================================================
# 10 适配必要性反证（对 test_decision_log 那处 _seq_ledger_of -> None 的独立判断）
# ==========================================================================


@pytest.mark.asyncio
async def test_next_seq常量化但台账在时不再撞号_适配必要性反证(db, engine, monkeypatch):
    """腿 10：既有用例 tests/routing/test_decision_log.py::test_seq_conflict_still_retries_and_message_says_seq
    把 _next_seq 钉成常量来造 seq 冲突。台账上线后这种钉法不再能造出冲突
    （台账把后续分配抬到下一档）=> 该用例若不补 _seq_ledger_of -> None 必然红。

    本腿两段：1) 台账在 + _next_seq 钉常量 => 不抛、拿到下一档 seq（证明补丁是必需的）；
    2) 再回落台账 => 同一场景抛 EventError（证明补丁没有削弱原判别力，冲突路径仍被真走到）。
    """
    _require_ledger_hooks()
    chat, chat_id = await _make_chat(db)
    factory = _session_factory(engine)

    async with factory() as session:
        await event_log.append_event(session, chat_id, "turn/start", {"turn": 0}, turn=0)
        await session.commit()

    async def _always_zero(_db, _chat_id):
        return 0

    monkeypatch.setattr(event_log, "_next_seq", _always_zero)

    async with factory() as session:
        seq = await event_log.append_event(session, chat_id, "retrieval/start", {"question": "台账在"}, turn=1)
        await session.commit()
    assert seq == 1, f"台账在 => 同一个库值下应抬到 1（不再撞号），实得 {seq}"

    monkeypatch.setattr(event_log, "_seq_ledger_of", lambda _db: None)
    async with factory() as session:
        with pytest.raises(event_log.EventError) as excinfo:
            await event_log.append_event(session, chat_id, "retrieval/start", {"question": "回落态"}, turn=2)
        await session.rollback()
    assert "seq" in str(excinfo.value) and "request_id" not in str(excinfo.value)


# ==========================================================================
# 11 口径钉：临界区无 await / 12 分配不写库 / 13 台账有界
# ==========================================================================


@pytest.mark.asyncio
async def test_台账临界区无await(db):
    """腿 11：_reserve_seq / _seq_ledger_of 都是同步函数 => 单事件循环内不可被切走。

    （对照：append_event 是 async —— 证明本条断言不是「函数都是同步的」这种恒真式。）
    """
    _require_ledger_hooks()
    assert inspect.iscoroutinefunction(event_log.append_event) is True, "对照：append_event 本应是 async"
    assert inspect.iscoroutinefunction(event_log._reserve_seq) is False, (
        "临界区必须是同步函数（一旦改成 async，await 处就可能被切走 => 分配不再原子）"
    )
    assert inspect.iscoroutinefunction(event_log._seq_ledger_of) is False, "_seq_ledger_of 也必须是同步取表"


@pytest.mark.asyncio
async def test_分配只发一条SELECT且不含行锁(db, engine):
    """腿 12：分配面只发一条 SELECT、语句里没有 FOR UPDATE / LOCK IN SHARE MODE，
    且不写库（监听器上没有 INSERT/UPDATE）—— 这样才不会跨请求持锁。"""
    _require_ledger_hooks()
    from sqlalchemy import event as sa_event

    chat, chat_id = await _make_chat(db)
    statements: list[str] = []

    def _record(_conn, _cursor, statement, _params, _context, _many):  # noqa: ANN001
        statements.append(str(statement))

    sa_event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        ledger = event_log._seq_ledger_of(db)
        db_next = await event_log._next_seq(db, chat_id)
        seq = event_log._reserve_seq(ledger, int(chat_id), db_next)
    finally:
        sa_event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert seq == 0, f"空会话首个分配应为 0，实得 {seq}"
    selects = [item for item in statements if item.strip().lower().startswith("select")]
    assert len(selects) == 1, f"分配面应恰发一条 SELECT，实得 {len(selects)} 条：{statements}"
    upper = " ".join(statements).upper()
    assert "FOR UPDATE" not in upper and "LOCK IN SHARE MODE" not in upper, (
        f"分配面不许出现行锁语句（会跨请求持有）：{statements}"
    )
    writes = [
        item for item in statements if item.strip().lower().startswith(("insert", "update", "delete"))
    ]
    assert not writes, f"分配面不许写库：{statements}"


@pytest.mark.asyncio
async def test_台账有界超限淘汰最旧(db):
    """腿 13：台账上限 = 512（与 chat_routes._TURN_LEDGER_MAX_CHATS / BREAKER_MAX_TRACKED 同值），
    超限按最早插入淘汰，且刚写的那条不会被自己淘汰。"""
    _require_ledger_hooks()
    assert event_log._SEQ_LEDGER_MAX_CHATS == 512, (
        f"上限应与两条先例同值 512，实得 {event_log._SEQ_LEDGER_MAX_CHATS}"
    )

    ledger: dict[int, tuple[int, int]] = {}
    for chat_id in range(513):
        event_log._reserve_seq(ledger, chat_id, 0)
    assert len(ledger) == 512, f"超限后应恰为 512 条，实得 {len(ledger)}"
    assert 0 not in ledger, "应淘汰最早插入的那条（chat 0）"
    assert 512 in ledger, "刚写的那条不许被自己淘汰"
    assert ledger[512] == (0, 0), f"新条目应是 (0, 0)，实得 {ledger[512]}"
