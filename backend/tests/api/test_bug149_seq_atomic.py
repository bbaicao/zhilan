"""回归修复 判据载体：``session_events`` 的 ``(chat_id, seq)`` 分配原子化。

## 缺陷本体（改前必红的那一半）

``events._next_seq`` 只读 ``MAX(seq) + 1``，``append_event`` 靠唯一约束
``uk_session_events_chat_seq`` 兜底、**只重试两次**。同一 ``chat_id`` 的两条并发轮各自读到
同一个 ``MAX(seq)`` ⇒ 同一个 seq ⇒ 唯一约束拒掉其中一条；重试仍读到同一个值
（MySQL REPEATABLE READ 下同一事务的一致读快照不变）⇒ 两次都撞 ⇒ ``raise EventError``
⇒ 被 ``safe_append_event`` 吞成一条 warning ⇒ **整轮事件全丢**。

阶段六执行期实测（同一现象也写在 ``tests/api/test_bug120_turn_atomic.py`` 的「执行期发现」段）：
库里只剩 ``turn=2`` 的行，``turn=1`` 的 ``turn/start`` / ``user/message`` /
``assistant/message`` / ``turn/end`` 一组全被吞。

## 修法（本笔）

照 ``回归修复`` 先例：``session/events.py`` 新增**进程内预留台账**
（``_SEQ_LEDGER`` 按 ``AsyncEngine`` 分表 / ``_seq_ledger_of`` / ``_reserve_seq`` 无
``await`` 的临界区，上限 512 + 代际安全阀），分配取
``max(库内读数, 上一次分配出去的 seq + 1)``；重试由 2 提到**有界 3 次**，每次重读库值并推进台账。
串行读数与改前**逐字同值**（腿 a1 / d3 是钉它的）。

## 腿构成（14 例）

末列 = **回退实验实测**（`events.py` 落回 `405bc17` 时的读数，**MySQL 测试档**；口径见下一节）。

| # | 腿 | 判别力 | 回退态实测 |
|---|---|---|---|
| a1 | 串行分配逐字等于「库内 MAX+1」 | 回归钉子（台账不许改串行读数） | 🟢 **两态都绿**（串行读数两态同值，此腿正是防过度修正） |
| a2 | 同一库值两路分配拿到不同 seq | 分配原子化的最小单元判据 | 🔴 红（**结构性**：直调 `_reserve_seq`，回退态无此名） |
| a3 | 库值回退（表被清）⇒ 重新起算 | 代际安全阀 | 🟢 **两态都绿**（清表后「库内 MAX+1」= 0，回落实现天然同值；本腿只钉台账**不许**把旧高水位带过清表） |
| a4 | 台账按引擎分表、跨引擎不共享 | **残余窗口登记腿 1**（多进程） | 🔴 红（**结构性**：`_seq_ledger_of` 不存在） |
| a5 | 上限 512：超限淘汰最早插入 | 界（照 134 / 120 先例） | 🔴 红（**结构性**：`_SEQ_LEDGER_MAX_CHATS` 不存在） |
| b1 | 真并发两路 ``append_event`` 两条都落库且 seq 互异（**三连读数**） | **核心判据**：改前必红 | 🔴 红（**行为性**：并发两路必有一条抛 `EventError('…连续两次冲突…')`） |
| c1 | 三次撞号后**有界放弃**并走记账出口 | 失败不许静默（改前是「两次就放弃且整轮吞掉」） | 🔴 红（**结构性**：腿体 monkeypatch `_seq_ledger_of` ⇒ `AttributeError` 先短路；「恰被调 3 次」在回退态**未实测**） |
| c2 | 分配路径抛异常仍被隔离 + 记账 | 新代码路径（``_next_seq``）不新增炸穿面 | 🟢 **两态都绿**（`safe_append_event` 的 except 覆盖面本笔未变） |
| d1 | 开关关闭 ⇒ ``None`` + 只记 ``disabled`` 不计数 | 契约 ``02:312`` | 🟢 **两态都绿**（本笔未改开关语义） |
| d2 | ``request_id`` 冲突**不重试**、不改文案 | ``回归修复/020`` 语义逐字保持 | 🟢 **两态都绿**（本笔未动 `RequestIdConflict` 分支） |
| d3 | 串行 seq 连续无空洞并真落库 | 既有不变量 | 🟢 **两态都绿**（串行连续性两态同值） |
| e1 | 同一会话两条并发 ``/chat/stream`` ⇒ 两组事件都在库里 | **端到端判据**（缺陷报告的原始症状） | 🔴 红（**行为性**：`assert {1} == {1, 2}` —— 整轮被吞） |
| f1 | 分配处不发锁语句、不额外往返 | 口径钉（不引入 ``FOR UPDATE``） | 🟢 **两态都绿**（判据是「不许变差」，回退态天然满足） |
| g1 | 台账取不到引擎 ⇒ 纯库内读数 `MAX+1`（与改前逐字同值）且冲突仍有界 3 次 | **回落支路**（Lead 批注 ② 指定的自有腿） | 🔴 红（**结构性**：腿体 monkeypatch `_seq_ledger_of` ⇒ `AttributeError`；本腿专考**新代码**的回落支路） |

## 回退态实测（真击杀 7 条 · 两态都绿 7 条）

口径 = patch 协议、**同一次锁持有内**闭环（`with_lock.ps1` 标签 `回归检查`）：
`git diff HEAD^ HEAD -- backend/app/session/events.py` = **9990 B** ⇒ `git apply -R` ⇒ 该文件 blob
落到 `405bc17` 的 `8b4c6505…`（SHA256 `C5CA5E1B…`）⇒ 跑本文件 ⇒ **7 failed / 7 passed**
⇒ `git apply` 还原 ⇒ blob 回 `64c50d08…`（SHA256 `DF8AEDD0…`），本文件字节数与 SHA256 未变。

- **真击杀 7 条** = a2 / a4 / a5 / b1 / c1 / e1 / g1；其中**行为性红只有 2 条** ——
  **b1**（并发两路必有一条抛 `EventError('…连续两次冲突…')`）与 **e1**（`assert {1} == {1, 2}`，整轮被吞），
  另 5 条是**结构性红**（腿面直接引用 / monkeypatch 新内部名 ⇒ `AttributeError` 先短路）。
  ⇒ 引用回退击杀的**判别力主证**请引 **b1 + e1**，其余 5 条只作结构面佐证。
- **两态都绿 7 条** = a1 / a3 / c2 / d1 / d2 / d3 / f1 ⇒ 它们是**防过度修正 / 回归护栏**，
  **不承担修复判别力**：a1 / a3 / d3 钉「串行读数与改前逐字同值」，c2 钉异常隔离覆盖面未变，
  d1 / d2 钉开关与 `request_id` 语义未动，f1 钉分配面不引入锁 / 不额外往返。
  ⛔ 统计击杀红点时必须把这 7 条**排除在红点之外**（非作者复核的 **回归检查 MINOR-2** 即指 a3 这一条；
  本列是照 Lead 裁决、按**实测**逐条补登，只动 docstring）。
- 覆盖边界：本列只覆盖 **MySQL 测试档**；SQLite 档的回退态未单独复量（常态两档各有独立读数）。

## 已登记残余窗口（🔴 不得含糊）

- **窗口 1（多进程 / 多 worker）**：台账是进程内的（按引擎分表）⇒ 同一库的两个后端进程
  仍可能同时算出同一个 seq。腿 a4 把这条**窗口本身**钉成可复跑的登记件；减轻项 = 重试会重读
  库值并推进台账 ⇒ 撞上的那一方通常能在 3 次内自愈，但「绝不丢」只在**同进程内**成立。
  跨进程闭合需要持久化计数器列 + 出迁移（本批 scope 外）。
- **窗口 2（seq 空洞）**：分配了但整轮不落库（收尾前断开 / 本地 abort，见 ``回归修复``）
  ⇒ 该 seq 白耗且**不可回退**。唯一性与单调性不受影响。
- ⛔ 本载体**不得**被读成「并发丢事件已全部消除」：同进程内闭合 + 残余窗口已登记 + 失败可见。

## 双方言

本笔零方言分支（不写 ``FOR UPDATE`` / 不用 ``BEGIN IMMEDIATE``）⇒ MySQL 测试档与
``DB_BACKEND=sqlite`` 桌面档跑同一条路径；两档各有独立读数（见交付）。
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event as sa_event
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Chat, SessionEvent
from app.session import events as event_log
from app.session import repository as repo
from tests.api.test_chat_contract import use_faux  # noqa: F401 —— 夹具（本仓既有做法：跨文件导入）

#: 一轮完整问答应有的四类事件（缺陷报告实测丢的就是这一组）。
TURN_TYPES = ("turn/start", "user/message", "assistant/message", "turn/end")

#: 并发窗口的等待上限：正常毫秒级返回；超时即「构造失效」，不该让用例挂死。
_WAIT_TIMEOUT = 10.0

#: 端到端腿的重复次数（判据纪律：给出重复跑的同读数）。
_E2E_ROUNDS = 3


@pytest.fixture
def event_log_on(monkeypatch):
    """把事件日志开关显式固定为开（避免受本机 .env 影响）。"""
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "CHAT_EVENT_LOG_ENABLE", True)


@pytest_asyncio.fixture
async def quiet_client(db, engine, monkeypatch) -> AsyncClient:
    """与 ``test_bug120_turn_atomic.py::quiet_client`` 同款的 ASGI 直连客户端。

    唯一差异 = ``raise_app_exceptions=False`：并发写把某一条请求打成 500 时，
    本载体要断言的仍是**事件落库面**，不该让传输层把应用异常抛出、把断言语句本身炸掉。
    """
    from app.core.config import settings
    from app.db.session import get_db
    from main import app

    monkeypatch.setattr(settings, "CHAT_DIRECT_ANSWER_ENABLE", True)
    monkeypatch.setattr(settings, "QUERY_CLARIFY_ENABLE", False)
    monkeypatch.setattr(settings, "ROUTING_MODE", "regex")

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _make_chat(db: AsyncSession) -> Chat:
    """建一个真会话（``session_events.chat_id`` 有外键 ⇒ 必须先有会话行）。"""
    chat = await repo.create_chat(db, title="seq 原子化载体")
    await db.commit()
    await db.refresh(chat)
    return chat


async def _other_engine(test_async_url: str):
    """另开一台引擎（≈另一个后端进程的视角）；SQLite 档复用产品同一份 PRAGMA。"""
    from app.core.config import settings
    from app.db.session import apply_sqlite_pragmas

    other = create_async_engine(test_async_url, pool_pre_ping=True)
    if settings.is_sqlite:
        apply_sqlite_pragmas(other.sync_engine)
    return other


async def _rows(db: AsyncSession, chat_id: int) -> list[tuple]:
    """该会话库内实况：``(seq, turn, type, data)`` 按 seq 升序。"""
    result = await db.execute(
        select(SessionEvent.seq, SessionEvent.turn, SessionEvent.type, SessionEvent.data)
        .where(SessionEvent.chat_id == chat_id)
        .order_by(SessionEvent.seq)
    )
    return list(result.all())


# --------------------------------------------------------------------------
# 腿 a：台账面（串行同值 / 分配原子化 / 代际安全阀 / 按引擎分表 / 上限）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a1_串行分配逐字等于库内最大值加一(db, fresh_db, event_log_on):
    """腿 a1（回归钉子）：串行 append 的 seq 与「插入前的库内 MAX(seq) + 1」**逐字同值**。

    台账只在并发时把在途那一格让开；串行必须退化成改前的纯库值读数（0/1/2/3）。
    """
    chat = await _make_chat(db)
    chat_id = int(chat.id)

    for index in range(4):
        rows_before = int(
            await db.scalar(
                select(func.count()).select_from(SessionEvent).where(SessionEvent.chat_id == chat_id)
            )
        )
        seq = await event_log.append_event(db, chat_id, "turn/start", {"n": index}, turn=index)
        await db.commit()
        assert seq == rows_before, (
            f"第 {index} 条串行事件的 seq 应为「库内 MAX(seq)+1」= 已有行数 {rows_before}，实得 {seq}"
            "（台账改了串行读数 = 把既有语义改坏）"
        )

    await fresh_db.rollback()
    rows = await _rows(fresh_db, chat_id)
    assert [int(row[0]) for row in rows] == [0, 1, 2, 3], f"库里应恰是 0..3 连续四行，实得 {rows}"


@pytest.mark.asyncio
async def test_a2_同一库值两路分配拿到不同seq(db, event_log_on):
    """腿 a2：两路分配**读到同一个库值**时，台账必须给出两个不同 seq（分配原子化的最小判据）。

    这正是竞态的形状：两条并发轮都在对方落库前读到 ``MAX(seq)+1 = 0``；改前两路都拿 0。
    """
    chat = await _make_chat(db)
    chat_id = int(chat.id)
    ledger = event_log._seq_ledger_of(db)
    assert ledger is not None, "本地引擎应能取到台账（取不到说明已回落纯 db 值，本腿前提不成立）"
    ledger.pop(chat_id, None)

    first = event_log._reserve_seq(ledger, chat_id, 0)
    second = event_log._reserve_seq(ledger, chat_id, 0)  # 第二路读到的库值仍是 0（对方未提交）

    assert (first, second) == (0, 1), (
        f"同一库值 0 的两路分配应为 0/1，实得 {first}/{second}"
        "（都等于 0 = 改前的纯 MAX+1；等于 0/2 = 把第一格吃掉了）"
    )


@pytest.mark.asyncio
async def test_a3_代际安全阀_库值回退后重新起算(db, test_async_url, event_log_on):
    """腿 a3：库内读数**比台账基准更小**（表被清 / SQLite rowid 复用）⇒ 重新起算，不被旧高水位顶号。

    构造：先在引擎甲上落 5 条（seq 0..4）；换引擎乙（台账为空）⇒ 首分配读库值 5 = seq 5，
    基准 = 5；再清表 ⇒ 库值回退到 0 < 基准 5 ⇒ 必须重新起算 0（而不是被旧高水位顶到 6）。
    """
    chat = await _make_chat(db)
    chat_id = int(chat.id)
    for index in range(5):
        await event_log.append_event(db, chat_id, "step/start", {"n": index})
    await db.commit()

    other = await _other_engine(test_async_url)
    try:
        factory = async_sessionmaker(other, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            first = await event_log.append_event(session, chat_id, "step/end", {"round": "first"})
            await session.commit()
            assert first == 5, f"引擎乙首分配应读库值得 5，实得 {first}"

            await session.execute(text("DELETE FROM session_events WHERE chat_id = :cid"), {"cid": chat_id})
            await session.commit()

            after = await event_log.append_event(session, chat_id, "step/end", {"round": "after-wipe"})
            await session.commit()
            assert after == 0, (
                f"库值回退到 0（< 台账基准 5）⇒ 必须重新起算 0，实得 {after}"
                " —— 上升 = 旧高水位把新会话的 seq 顶上去了（代际安全阀失效）"
            )
    finally:
        await other.dispose()


@pytest.mark.asyncio
async def test_a4_台账按引擎分表_跨引擎不共享_残余窗口登记(db, test_async_url, event_log_on):
    """腿 a4（**残余窗口登记腿 1**）：台账按引擎分表 ⇒ 另一台引擎看不到本引擎的预留。

    断言的是**边界本身**：两台引擎各自算出同一个 seq ⇒ **多进程 / 多 worker 部署下仍会撞号**
    （唯一约束 + 有界重试兜住，但「绝不丢」这条保证只在同进程内成立）。跨进程闭合需要给会话加
    持久化计数器列 + 出迁移（**本批 scope 外**）。若哪天真闭合了，本腿要随之改写。
    """
    ledger_a = event_log._seq_ledger_of(db)
    assert ledger_a is not None
    assert event_log._seq_ledger_of(db) is ledger_a, "同一引擎重复取必须是同一份台账（幂等）"

    other = await _other_engine(test_async_url)
    try:
        factory = async_sessionmaker(other, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            ledger_b = event_log._seq_ledger_of(session)
            assert ledger_b is not None
            assert ledger_b is not ledger_a, (
                "台账必须**按引擎分表**（合并成模块级全局 dict 会让单测跨用例泄漏，"
                "也会把「跨进程未共享」这条残余窗口藏起来）"
            )

            probe_chat = 424242
            ledger_a.pop(probe_chat, None)
            ledger_b.pop(probe_chat, None)
            seq_a = event_log._reserve_seq(ledger_a, probe_chat, 0)
            seq_b = event_log._reserve_seq(ledger_b, probe_chat, 0)
            assert (seq_a, seq_b) == (0, 0), (
                f"两台引擎在同一个库值 0 上应各得 0（= 跨进程撞号窗口存在），实得 {seq_a}/{seq_b}"
                " —— 若不再相等，说明跨进程闭合已落地，本登记腿应改写为「已闭合」"
            )
    finally:
        await other.dispose()


@pytest.mark.asyncio
async def test_a5_台账上限淘汰最早插入(db, event_log_on):
    """腿 a5：台账超过 ``_SEQ_LEDGER_MAX_CHATS`` 时淘汰**最早插入**的一条（界，照 134 / 120 先例）。"""
    limit = event_log._SEQ_LEDGER_MAX_CHATS
    assert limit == 512, f"上限应照同仓先例取 512，实得 {limit}"

    ledger: dict[int, tuple[int, int]] = {}
    for chat_id in range(limit + 1):
        event_log._reserve_seq(ledger, chat_id, 0)

    assert len(ledger) == limit, f"超限后应恰留 {limit} 条，实得 {len(ledger)}"
    assert 0 not in ledger, "最早插入的那条应被淘汰"
    assert limit in ledger, "刚写入的那条不许被自己淘汰"


# --------------------------------------------------------------------------
# 腿 b：真并发（核心判据）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b1_真并发两路append_两条都落库且seq互异_三连读数(db, engine, fresh_db, event_log_on, monkeypatch):
    """腿 b1（**核心**）：同一 chat_id、两个独立会话，``asyncio.gather`` 并发 append ⇒
    两条**都成功返回**、seq **互异**、库里**两行都在**（改前：一条 EventError / 整轮被吞）。

    「真并发」用**事件同步点**强制重叠（不靠调度巧合）：两路的 ``_next_seq`` 都先完成读库、
    再等对方也读完，然后才各自分配 —— 复现的正是「都读到同一个 ``MAX(seq)``」这一格。
    三连读数（每轮换一个新会话）用于证明可复跑。
    """
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    readings: list[dict] = []

    for round_no in range(1, 4):
        chat = await _make_chat(db)
        chat_id = int(chat.id)

        real_next = event_log._next_seq
        both_read = asyncio.Event()
        reads: list[int] = []

        async def gated(_db, _cid, _real=real_next, _reads=reads, _gate=both_read):
            value = await _real(_db, _cid)
            _reads.append(value)
            if len(_reads) == 1:
                await asyncio.wait_for(_gate.wait(), _WAIT_TIMEOUT)
            elif len(_reads) == 2:
                _gate.set()
            return value

        monkeypatch.setattr(event_log, "_next_seq", gated)

        async def one(label: str) -> int:
            async with factory() as session:
                seq = await event_log.append_event(session, chat_id, "turn/start", {"who": label})
                await session.commit()
                return int(seq)

        results = await asyncio.gather(one("甲"), one("乙"), return_exceptions=True)
        errors = [item for item in results if isinstance(item, BaseException)]
        assert not errors, (
            f"第 {round_no} 轮并发两路应都成功，实得异常 {errors}"
            "（改前：第二次撞 uk_session_events_chat_seq、重试仍撞 ⇒ EventError ⇒ safe_append_event 吞掉）"
        )
        assert reads[:2] == [0, 0], (
            f"第 {round_no} 轮两路必须读到**同一个库值 0**（否则没复现竞态，本腿无判别力）：{reads}"
        )
        assert sorted(int(item) for item in results) == [0, 1], (  # type: ignore[arg-type]
            f"第 {round_no} 轮两条应拿到互异的 0/1，实得 {sorted(results)}"
        )

        await fresh_db.rollback()
        rows = await _rows(fresh_db, chat_id)
        assert [int(row[0]) for row in rows] == [0, 1], (
            f"第 {round_no} 轮库里应真有那两行（seq 0 与 1），实得 {rows}"
            "（缺一行 = 整轮事件被吞，正是 回归修复 的原始症状）"
        )
        assert {row[3]["who"] for row in rows} == {"甲", "乙"}, f"两行的载荷应是甲乙各一：{rows}"

        readings.append({"round": round_no, "seqs": sorted(int(i) for i in results), "rows": len(rows)})  # type: ignore[arg-type]

    assert [item["rows"] for item in readings] == [2, 2, 2], f"三连读数应一致：{readings}"


# --------------------------------------------------------------------------
# 腿 c：负控 —— 失败仍要有出口，不许静默成功
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c1_三次撞号后有界放弃并走记账出口(db, event_log_on, monkeypatch):
    """腿 c1（**负控**）：台账不可用（回落纯 db 值）+ 库值恒定撞号 ⇒ **有界试 3 次**后放弃，
    经 ``safe_append_event`` 返回 ``None``，且全局计数 +1、本轮桶变 True、缺行原因 = ``write_failed``。

    ⛔ 不许「静默成功」：返回值 ``None`` 必须有配套的记账出口（契约 ``02:312`` 的可见性要求）。
    """
    chat = await _make_chat(db)
    chat_id = int(chat.id)
    await event_log.append_event(db, chat_id, "turn/start", {"n": 0})  # 占住 seq 0
    await db.commit()

    calls = {"n": 0}

    async def _always_conflicting(_db, _cid):
        calls["n"] += 1
        return 0  # 恒撞库里那条 seq=0

    monkeypatch.setattr(event_log, "_next_seq", _always_conflicting)
    monkeypatch.setattr(event_log, "_seq_ledger_of", lambda _db: None)  # 台账不可用 ⇒ 回落纯 db 值

    snapshot = event_log.failure_snapshot()
    turn_key = (chat_id, 7)
    try:
        result = await event_log.safe_append_event(
            db, chat_id, "retrieval/start", {"question": "q"}, turn_key=turn_key
        )

        assert result is None, "撞满重试上限应返回 None（直抛版才抛 EventError）"
        assert calls["n"] == 3, (
            f"有界退避应恰试 3 次，实得 {calls['n']} 次"
            "（2 次 = 还停在改前的上限；>3 = 无界重试，先挂死的会是用例）"
        )
        assert event_log.failures_since(snapshot, turn_key=turn_key) is True, "本轮必须被记为「有缺行」"
        count, reason = event_log.turn_event_incomplete(turn_key)
        assert (count, reason) == (1, event_log.INCOMPLETE_WRITE_FAILED), (
            f"缺行出口应为 (1, write_failed)，实得 {(count, reason)}"
        )
        assert event_log.failure_snapshot().total == snapshot.total + 1

        rows = await _rows(db, chat_id)
        assert [int(row[0]) for row in rows] == [0], f"三条重试都不该落库，实得 {rows}"
    finally:
        event_log.restore_event_write_failures(snapshot)
        await db.rollback()


@pytest.mark.asyncio
async def test_c2_分配路径异常被隔离并记账(db, event_log_on, monkeypatch):
    """腿 c2：**新引入的分配路径**（``_next_seq`` / ``_reserve_seq``）抛异常时同样被隔离 + 记账。

    防的是「修并发把新的炸穿面引进来」：任何异常都只留 warning + 计数，绝不向调用方抛。
    """
    chat = await _make_chat(db)
    chat_id = int(chat.id)

    async def _boom(_db, _cid):
        raise RuntimeError("分配段炸了")

    monkeypatch.setattr(event_log, "_next_seq", _boom)

    snapshot = event_log.failure_snapshot()
    turn_key = (chat_id, 8)
    try:
        result = await event_log.safe_append_event(db, chat_id, "turn/start", {}, turn_key=turn_key)
        assert result is None, "分配段异常必须被隔离（返回 None），不许炸穿主流程"
        assert event_log.failures_since(snapshot, turn_key=turn_key) is True
        assert event_log.turn_event_incomplete(turn_key)[1] == event_log.INCOMPLETE_WRITE_FAILED
    finally:
        event_log.restore_event_write_failures(snapshot)


# --------------------------------------------------------------------------
# 腿 d：回归钉子（开关 / request_id 冲突 / 串行不变量）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d1_开关关闭返回None且只记disabled不计数(db, monkeypatch):
    """腿 d1：``CHAT_EVENT_LOG_ENABLE=false`` ⇒ 返回 ``None``、不动台账、**不计数**、只记 ``disabled``。

    契约 ``02:312`` 原文：「日志开关关闭时不计数」。开关关掉是**预期**，不是写失败。
    """
    from app.core import config as config_mod

    chat = await _make_chat(db)
    chat_id = int(chat.id)
    monkeypatch.setattr(config_mod.settings, "CHAT_EVENT_LOG_ENABLE", False)

    snapshot = event_log.failure_snapshot()
    turn_key = (chat_id, 3)
    result = await event_log.safe_append_event(db, chat_id, "turn/start", {}, turn_key=turn_key)

    assert result is None
    assert event_log.turn_event_incomplete(turn_key) == (0, event_log.INCOMPLETE_DISABLED)
    assert event_log.failures_since(snapshot, turn_key=turn_key) is False, "开关关闭不该计入写失败"
    assert event_log.failure_snapshot().total == snapshot.total
    assert await _rows(db, chat_id) == [], "开关关闭 ⇒ 事件表零行"

    event_log.restore_event_write_failures(snapshot)


@pytest.mark.asyncio
async def test_d2_request_id冲突不重试且抛RequestIdConflict(db, event_log_on, monkeypatch):
    """腿 d2：``(chat_id, request_id)`` 冲突**不重试**、抛 :class:`RequestIdConflict``（语义逐字保持）。

    判据 = 分配器只被调用**一次**（不白重试、也不推台账），且文案说的是 ``request_id`` 不是 ``seq``。
    """
    chat = await _make_chat(db)
    chat_id = int(chat.id)
    await event_log.append_event(
        db, chat_id, "routing/decided", {"source_type": "external", "source": "regex"}, request_id="dup-149"
    )
    await db.commit()

    calls = {"n": 0}
    real_next = event_log._next_seq

    async def counting(_db, _cid):
        calls["n"] += 1
        return await real_next(_db, _cid)

    monkeypatch.setattr(event_log, "_next_seq", counting)
    snapshot = event_log.failure_snapshot()

    with pytest.raises(event_log.RequestIdConflict) as excinfo:
        await event_log.append_event(db, chat_id, "retrieval/start", {"question": "q"}, request_id="dup-149")

    assert calls["n"] == 1, f"request_id 冲突不重试（分配器应只被调用 1 次），实得 {calls['n']} 次"
    message = str(excinfo.value)
    assert "request_id" in message and "seq" not in message, f"原因必须归到 request_id：{message}"
    assert event_log.failure_snapshot().total == snapshot.total, "append_event 直抛，不经过隔离计数"
    assert [row[2] for row in await _rows(db, chat_id)] == ["routing/decided"], "撞约束的那条确实没落库"


@pytest.mark.asyncio
async def test_d3_串行seq连续无空洞并真落库(db, fresh_db, event_log_on):
    """腿 d3（**对照腿**）：串行 append 5 条 ⇒ seq ``0..4`` 连续无空洞，且换连接真读得到。

    台账的高水位只影响**分配**，不许在库里留洞（与 c1 的「撞号不落库」一起，把空洞边界写清）。
    """
    chat = await _make_chat(db)
    chat_id = int(chat.id)

    seqs = [
        await event_log.append_event(db, chat_id, "step/start", {"n": index}) for index in range(5)
    ]
    await db.commit()
    assert seqs == [0, 1, 2, 3, 4], f"串行五条应为 0..4，实得 {seqs}"

    await fresh_db.rollback()
    rows = await _rows(fresh_db, chat_id)
    assert [int(row[0]) for row in rows] == [0, 1, 2, 3, 4], f"换连接应读到同样五行：{rows}"
    assert int(await db.scalar(select(func.max(SessionEvent.seq)).where(SessionEvent.chat_id == chat_id))) == 4


# --------------------------------------------------------------------------
# 腿 e：端到端（缺陷报告的原始症状面）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e1_并发两条流两轮事件都落库(quiet_client, use_faux, db, fresh_db, event_log_on):
    """腿 e1（**端到端**）：同一 ``chat_id`` 的两条并发 ``/chat/stream`` ⇒ 两条都 200，
    且库里**两组** ``(chat_id, turn)`` 的事件都在（每组都含 ``turn/start`` / ``user/message`` /
    ``assistant/message`` / ``turn/end``）。

    改前：库里只剩其中一组（另一组整轮被 ``safe_append_event`` 吞掉）⇒ 必红。
    """
    use_faux("好。", extra_replies=["好。", "好。"] * _E2E_ROUNDS)
    chat = await _make_chat(db)
    chat_id = int(chat.id)

    responses = await asyncio.gather(
        quiet_client.post("/api/chat/stream", json={"chat_id": chat_id, "message": "并发提问甲"}),
        quiet_client.post("/api/chat/stream", json={"chat_id": chat_id, "message": "并发提问乙"}),
    )
    assert [resp.status_code for resp in responses] == [200, 200], (
        f"两条并发流都应正常收尾，实得 {[resp.status_code for resp in responses]}"
    )

    await fresh_db.rollback()
    rows = await _rows(fresh_db, chat_id)
    by_turn: dict[int, set[str]] = {}
    for row in rows:
        if row[1] is not None:
            by_turn.setdefault(int(row[1]), set()).add(str(row[2]))

    assert set(by_turn) == {1, 2}, (
        f"两轮都应有事件（turn=1/2），实得桶 {sorted(by_turn)}"
        f"（缺一个桶 = 整轮事件被吞，正是 回归修复）；全量行：{rows}"
    )
    for turn, types in sorted(by_turn.items()):
        missing = set(TURN_TYPES) - types
        assert not missing, (
            f"turn={turn} 缺 {sorted(missing)}（整轮丢事件），该轮实得 {sorted(types)}；全量行：{rows}"
        )


# --------------------------------------------------------------------------
# 腿 g：台账回落支路（取不到引擎 ⇒ 纯库内读数；Lead 批注 ② 指定的自有腿）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_g1_台账回落支路_纯库内读数且三次撞号后抛EventError(db, event_log_on, monkeypatch):
    """腿 g1（**回落支路**）：``_seq_ledger_of`` 取不到引擎 ⇒ 分配值 = 纯库内读数
    ``MAX(seq) + 1``（与改前**逐字同值**），且此时撞号仍按**有界 3 次**重试后抛 ``EventError``。

    「回落支路」不能只由被适配的那条既有用例（``tests/routing/test_decision_log.py``）覆盖
    ⇒ 本腿与它并列：① 分配读数（串行同值）；② 有界 3 次 + 文案归因（说 ``seq`` 不说 ``request_id``）。
    """
    chat = await _make_chat(db)
    chat_id = int(chat.id)

    # ① 回落 = 纯库内读数：串行两条与改前逐字同值
    monkeypatch.setattr(event_log, "_seq_ledger_of", lambda _db: None)
    first = await event_log.append_event(db, chat_id, "step/start", {"n": 0})
    second = await event_log.append_event(db, chat_id, "step/start", {"n": 1})
    await db.commit()
    assert (first, second) == (0, 1), (
        f"回落支路下串行分配应与库内读数一致（0/1），实得 {(first, second)}"
    )

    # ② 冲突：有界 3 次后抛 EventError（文案说的是 seq，不是 request_id）
    calls = {"n": 0}

    async def _always_same_seq(_db, _cid):
        calls["n"] += 1
        return 0  # 恒撞库里那条 seq=0

    monkeypatch.setattr(event_log, "_next_seq", _always_same_seq)

    with pytest.raises(event_log.EventError) as excinfo:
        await event_log.append_event(db, chat_id, "retrieval/start", {"question": "q"})

    assert calls["n"] == 3, f"回落支路下撞号应有界重试 3 次，实得 {calls['n']} 次"
    message = str(excinfo.value)
    assert "seq" in message and "request_id" not in message, f"原因必须归到 seq：{message}"


# --------------------------------------------------------------------------
# 腿 f：口径钉（不引入行锁 / 不额外往返）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f1_分配处不发锁语句且不额外往返(db, engine, event_log_on):
    """腿 f1：一次 ``append_event`` 的分配面只有**一条无锁 SELECT**，语句里没有
    ``FOR UPDATE`` / ``LOCK IN SHARE MODE``（行锁会跨整个请求持有，且 SQLite 档不支持）。"""
    chat = await _make_chat(db)
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(str(statement))

    sa_event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        seq = await event_log.append_event(db, chat.id, "turn/start", {"n": 1})
    finally:
        sa_event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert seq == 0, f"空会话首条应为 0，实得 {seq}"
    selects = [item for item in statements if item.lstrip().lower().startswith("select")]
    upper = " ".join(statements).upper()
    assert "FOR UPDATE" not in upper and "LOCK IN SHARE MODE" not in upper, (
        f"分配处不该出现任何行锁语句：{statements}"
    )
    assert len(selects) == 1, f"一次 append 应恰发一条 SELECT（分配不额外往返），实得 {statements}"
