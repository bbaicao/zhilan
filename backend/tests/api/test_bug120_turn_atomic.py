"""回归修复 判据载体：同一会话并发提问的**轮号分配原子化**（进程内预留台账）。

## 缺陷本体（改前必红的那一半）

``chat_routes.next_turn_no`` 原先只有「``SELECT COUNT(*) WHERE role='user'`` + 1」：
同一 ``chat_id`` 的两个并发 ``/chat/stream`` 都在对方 ``persist_turn`` 提交前 COUNT
⇒ **同一个轮号** ⇒ 两轮事件混进同一个 ``(chat_id, turn)`` 桶（``events_incomplete``
说假话）、``agent_runs.meta.turn`` 跟着串。腿①（分配面）与腿②（事件桶症状面）钉的就是它。

## 修法（本笔）

``next_turn_no`` 在库内计数之外再叠一条**进程内预留高水位**（``_reserve_turn``：无 await 的
同步临界区，**释放点 = 该函数返回**）：串行时读数与改前**逐字同值**，并发时第二个请求
拿 ``count + 2``。**不持数据库行锁**（不整请求持锁，也不写 ``FOR UPDATE``）。

## 腿构成（9 例）

| # | 腿 | 判别力 |
|---|---|---|
| ① | 甲分配后被卡住（事务在途）时乙开跑 ⇒ 两个不同轮号 | 改前 ``[1, 1]`` 必红 |
| ② | 两轮的**归属键** ``(chat_id, turn)`` 不合并（症状面） | 改前 ``{(cid, 1)}`` 必红 |
| ③ | 六路并发调分配器 ⇒ 六个不同轮号 | 改前 ``{2}`` 必红 |
| ④ | 甲在途（卡在分配点）时，乙的分配**立刻**返回（计时） | **负控**：整请求持行锁的实现会超时 |
| ⑤ | 分配只发一条 SELECT 且不含 FOR UPDATE | 口径钉（不许引入行锁） |
| ⑥ | 串行场景轮号仍是「库内计数 + 1」 | **对照腿**：防把串行语义改坏 |
| ⑦ | ``chat_id=None`` 恒回 1（且不写台账） | 分支钉 |
| ⑧ | 跨引擎（≈跨进程）预留不共享 | **残余窗口登记腿 1** |
| ⑨ | 分配了但彻底不落库 ⇒ 轮号**空洞且不可回退** | **残余窗口登记腿 2**（审计发现 120-1） |

## 为什么栅栏开在 next_turn_no 上（而不是 build_turn_plan 上）

执行期实测（2026-09-28，走锁探针）：两条同会话并发轮在 ``build_turn_plan`` 里**本来就会
被产品自身串行化一段**——``session_events`` 的 ``seq`` 分配是「读改写」，第二条的
``routing/decided`` INSERT 会等第一条的事务结束。把栅栏放在 ``build_turn_plan`` 外层
（要求两个请求同时停在分配之后）会因此超时，**测不出轮号竞态**。
⇒ 本载体的构造是：**甲在 ``next_turn_no`` 返回之后卡住**（=「已分配、尚未落库」这个
真实窗口），乙在此窗口内开跑。这正是缺陷报告描述的交错，且**确定性**（不靠调度巧合）。

## 双方言

本笔**零方言分支**（不写 ``FOR UPDATE``、不用 ``BEGIN IMMEDIATE``）⇒ MySQL 测试档与
``DB_BACKEND=sqlite`` 桌面档跑的是同一条路径；两档各有独立读数（见交付）。

## 执行期发现（**不在本笔 scope**，已报 Lead 登记）

实测（2026-09-28，走锁跑腿①②）：``session_events`` 的 ``(chat_id, seq)`` 是**读改写**
（唯一约束 ``uk_session_events_chat_seq``，``events.append_event`` 里 ``seq = MAX(seq)+1``、
失败只重试 2 次）⇒ **同会话并发轮会整轮丢事件**：本次实测库里只剩 ``turn=2`` 的行，
``turn=1`` 的 ``turn/start``/``user/message``/``assistant/message``/``turn/end`` 全被
``safe_append_event`` 吞成 warning。⇒ 腿②的判据因此落在**归属键**（产品分桶用的那个键）上，
而不是库内行集合；丢事件这条同族缺陷属 ``session/events.py``（本笔 scope 外）。

## 已登记残余窗口（🔴 不得含糊）

**窗口 1**：预留台账是**进程内**的（按 ``AsyncEngine`` 分表）：多进程 / 多 worker 部署下，
同一库的两个后端进程仍可能拿到同一轮号 —— 腿⑧把这条窗口钉成可复跑的登记件。
跨进程闭合需要给 ``chats`` 加持久化计数器列 + 出迁移（**scope 外**，已移交）。

**窗口 2（审计发现 120-1）**：**分配了但没落库 ⇒ 该号不回收、轮号出现空洞且不可回退**。
轮号在 ``build_turn_plan`` 就分配、用户消息在全轮收尾才落库（``回归修复`` 已登记
「收尾前断开 ⇒ 后端什么都不落库」）⇒ 断开 / 本地 abort / 分配后走到不落库的出口都会白耗一个号；
库内计数恒为 0 时连做三次分配得 ``1/2/3``，此后该会话轮号 = 「库内计数 + 1 + 空洞数」，
直到进程重启或该条目被上限淘汰。**唯一性不受影响**（腿①③已证）；受影响的是
「轮号 == 用户消息序号 + 1」这条既有可读契约（``agent_runs.meta.turn`` / 事件分桶会出现
从未落库的号）。``⛔`` **不做回收**：那需要「在途号集合 + 每个出口都释放」，漏一个出口是永久
多占（与现状同级），错放一个出口就把唯一性窗口重新打开 ⇒ 取「登记 + 判据钉住」。
腿⑨把这条行为钉成可复跑的登记件。
"""

from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import chat_routes
from app.models import Chat, Message, SessionEvent
from app.session import events as event_log
from app.session import repository
from tests.api.test_chat_contract import use_faux  # noqa: F401 —— 夹具（本仓既有做法：跨文件导入）

#: 分配/等待上限：正常实现毫秒级返回；超时即「被整请求串行化」或「被行锁挂住」。
_WAIT_TIMEOUT = 8.0


@pytest_asyncio.fixture
async def quiet_client(db, engine, monkeypatch) -> AsyncClient:
    """与 ``tests/api/test_chat_contract.py::client`` 同款的 ASGI 直连客户端。

    唯一差异 = ``raise_app_exceptions=False``：并发写把某一条请求打成 500 时，
    本载体要断言的仍是**分配面**（轮号），不该让传输层把应用异常抛出把断言语句本身炸掉。
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


async def _make_chat(db: AsyncSession, *, users: int = 0) -> Chat:
    """建一个会话，并可预置 ``users`` 条用户消息（轮号基线 = 用户消息条数 + 1）。"""
    chat = Chat(title="轮号原子化载体")
    db.add(chat)
    await db.commit()
    await db.refresh(chat)
    for index in range(users):
        await repository.add_message(
            db, chat_id=chat.id, role="user", content=f"种子提问 {index + 1}"
        )
    if users:
        await db.commit()
    return chat


class _AllocWindow:
    """「甲在途（已分配、未落库）」窗口的确定性构造器（见模块 docstring）。"""

    def __init__(self, monkeypatch, chat_id: int) -> None:
        self.turns: list[int] = []
        self.first_allocated = asyncio.Event()
        self.second_allocated = asyncio.Event()
        self.gate = asyncio.Event()
        self.second_latency = float("nan")
        self._seen = 0
        self._chat_id = chat_id
        real = chat_routes.next_turn_no

        async def spy(db: AsyncSession, cid: int | None) -> int:
            turn = await real(db, cid)
            self._seen += 1
            self.turns.append(int(turn))
            if self._seen == 1:
                # 甲：分配已完成，卡在这里 = 「本进程已记账、库里还没有本轮消息」
                self.first_allocated.set()
                await self.gate.wait()
            elif self._seen == 2:
                self.second_allocated.set()
            return int(turn)

        monkeypatch.setattr(chat_routes, "next_turn_no", spy)

    async def run(self, client: AsyncClient):
        """跑「甲在途 + 乙并发」这一对请求，返回 ``(overlapped, second_wait, responses)``。

        ``overlapped`` = 乙在甲仍卡着的时候**也拿到了轮号**（= 临界区没跨请求）；
        任一环节超时 ⇒ 该布尔为 ``False``（腿红），而不是让整条用例挂死。
        """
        task_a = asyncio.create_task(
            client.post(
                "/api/chat/stream", json={"chat_id": self._chat_id, "message": "并发提问甲"}
            )
        )
        await asyncio.wait_for(self.first_allocated.wait(), _WAIT_TIMEOUT)

        started = time.monotonic()
        task_b = asyncio.create_task(
            client.post(
                "/api/chat/stream", json={"chat_id": self._chat_id, "message": "并发提问乙"}
            )
        )
        overlapped = False
        try:
            await asyncio.wait_for(self.second_allocated.wait(), _WAIT_TIMEOUT)
            self.second_latency = time.monotonic() - started
            overlapped = True
        except asyncio.TimeoutError:
            self.second_latency = time.monotonic() - started
        finally:
            self.gate.set()

        responses = await asyncio.gather(task_a, task_b, return_exceptions=True)
        return overlapped, self.second_latency, responses


def _codes(responses) -> list:
    return [getattr(item, "status_code", repr(item)) for item in responses]


# --------------------------------------------------------------------------
# ① 分配面：并发两请求不撞号
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_并发两请求各得不同轮号(quiet_client, use_faux, monkeypatch, db):
    """腿①（主腿）：甲在途时乙开跑 ⇒ 两个请求拿到**两个不同**轮号。

    改前必红：两条都算出 ``user 条数 + 1 = 1`` ⇒ ``sorted(turns) == [1, 1]``。
    """
    use_faux("好。", extra_replies=["好。", "好。"])
    chat = await _make_chat(db)
    window = _AllocWindow(monkeypatch, chat.id)

    overlapped, _latency, responses = await window.run(quiet_client)

    assert overlapped, "乙在甲在途期间没拿到轮号（超时）⇒ 分配处被整请求串行化了"
    assert sorted(window.turns) == [1, 2], (
        f"并发两轮应分别拿到 1 与 2，实得 {sorted(window.turns)}"
        "（都等于 1 = 改前的 COUNT+1 竞态；都等于 2 = 台账把第一个号吃掉了）"
    )
    codes = _codes(responses)
    assert all(code == 200 for code in codes), f"两条流式请求应都正常收尾，实得 {codes}"


# --------------------------------------------------------------------------
# ② 症状面：两轮事件不许落在同一个归属桶
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_并发两轮事件落进不同归属桶(quiet_client, use_faux, monkeypatch, db):
    """腿②（症状面）：真跑这一对并发流之后，库里 ``(chat_id, turn)`` 的 turn 集合是 ``{1, 2}``。

    这条腿打的是缺陷报告里的原话——「事件表出现两套同 turn 号的完整事件组」：
    改前两条流写入的归属键全部是 ``(chat_id, 1)`` ⇒ 集合只有一个键 ⇒ 必红。
    """
    use_faux("好。", extra_replies=["好。", "好。"])
    chat = await _make_chat(db)
    window = _AllocWindow(monkeypatch, chat.id)

    keys: set[tuple] = set()
    real_append = event_log.safe_append_event

    async def spy_append(db_, chat_id_, event_type, data, *, turn_key=None, **kwargs):  # noqa: ANN001
        if turn_key is not None:
            keys.add((turn_key[0], turn_key[1]))
        return await real_append(db_, chat_id_, event_type, data, turn_key=turn_key, **kwargs)

    monkeypatch.setattr(event_log, "safe_append_event", spy_append)

    overlapped, _latency, responses = await window.run(quiet_client)
    assert overlapped, "乙在甲在途期间没拿到轮号（超时）⇒ 本腿场景不成立"
    codes = _codes(responses)
    assert all(code == 200 for code in codes), f"两条流式请求应都正常收尾，实得 {codes}"

    assert keys == {(chat.id, 1), (chat.id, 2)}, (
        f"两轮事件的归属键应为 {{({chat.id}, 1), ({chat.id}, 2)}}，实得 {keys}"
        "（只有一个键 = 两轮混进同一个桶，正是 回归修复 的症状）"
    )

    # 附：库内出现的桶必须 ⊆ {1, 2}（不得串到第三个号）。
    # ⚠️ 执行期实测（2026-09-28）：``session_events`` 的 (chat_id, seq) 是「读改写」，
    #    同会话并发轮会**整轮丢事件**（本次实测库里只剩 {2}）⇒ 这里只断言上界，
    #    不断言「两个桶都在库里」——那是另一条同族缺陷（scope 外，已报 Lead 登记）。
    chat_id = int(chat.id)
    await db.rollback()  # 换一个干净快照再读（并发写发生在别的连接上）
    rows = (
        (await db.execute(select(SessionEvent.turn).where(SessionEvent.chat_id == chat_id)))
        .scalars()
        .all()
    )
    bucket_turns = {int(item) for item in rows if item is not None}
    assert bucket_turns <= {1, 2}, f"库内出现了第三个桶：{bucket_turns - {1, 2}} ⇒ 轮号串号"


# --------------------------------------------------------------------------
# ③ 分配器本身的并发判别力（不经过 HTTP）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_分配器六路并发六个轮号互不相同(db, engine):
    """腿③：六路并发直接调 ``next_turn_no``（各自独立会话）⇒ 恰得 ``2..7`` 六个号。

    不经过 HTTP ⇒ 不依赖端点装配，直接钉分配语义。改前必红：六个都是 ``2``。
    """
    chat = await _make_chat(db, users=1)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def one() -> int:
        async with factory() as session:
            return await chat_routes.next_turn_no(session, chat.id)

    got = await asyncio.gather(*[one() for _ in range(6)])

    assert sorted(got) == list(range(2, 8)), (
        f"六路并发应恰得 2..7，实得 {sorted(got)}"
        "（六个 2 = 改前的纯计数；缺号 = 台账把号吞了）"
    )


# --------------------------------------------------------------------------
# ④ 负控：临界区只在分配处，不跨请求持锁
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_甲在途时乙的分配立刻返回(quiet_client, use_faux, monkeypatch, db):
    """腿④（**负控**）：甲卡在「已分配、未落库」时，乙的分配必须**立刻**返回。

    判别力指向计划原稿的写法（``SELECT … FOR UPDATE`` 锁 ``chats`` 行再 COUNT）：
    那种实现会把行锁持到甲的全轮收尾 ⇒ 乙的分配在这里超时 ⇒ 本腿红。
    本笔不取任何行锁 ⇒ 本腿绿。**若把分配改成整请求持锁，本腿是唯一会红的腿。**
    """
    use_faux("好。", extra_replies=["好。", "好。"])
    chat = await _make_chat(db)
    window = _AllocWindow(monkeypatch, chat.id)

    overlapped, latency, responses = await window.run(quiet_client)

    assert overlapped, (
        f"乙的分配在 {latency:.1f}s 内没返回 ⇒ 分配处持有跨请求的锁"
        "（正是本条目要点 ② 禁止的形态）"
    )
    assert latency < 2.0, f"乙的分配耗时 {latency:.2f}s，远超一次 COUNT 的量级"
    assert window.turns[1] != window.turns[0], f"甲乙拿到了同一个号：{window.turns}"
    codes = _codes(responses)
    assert all(code == 200 for code in codes), f"两条流式请求应都正常收尾，实得 {codes}"


# --------------------------------------------------------------------------
# ⑤ 口径钉：分配处不引入行锁、不额外往返
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_分配只发一条无锁SELECT(db, engine):
    """腿⑤：``next_turn_no`` 期间只发一条 SELECT，且语句里没有 ``FOR UPDATE``。"""
    from sqlalchemy import event

    chat = await _make_chat(db, users=1)
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(str(statement))

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        turn = await chat_routes.next_turn_no(db, chat.id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    selects = [item for item in statements if "select" in item.lower()]
    assert turn == 2, f"库内 1 条用户消息 ⇒ 应为 2，实得 {turn}"
    assert len(selects) == 1, f"分配处应恰发一条 SELECT，实得 {len(selects)} 条：{statements}"
    upper = " ".join(statements).upper()
    assert "FOR UPDATE" not in upper and "LOCK IN SHARE MODE" not in upper, (
        f"分配处不该出现任何行锁语句（会跨请求持有）：{statements}"
    )


# --------------------------------------------------------------------------
# ⑥ 对照腿：串行语义逐字不变
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_串行场景轮号仍是库内计数加一(db):
    """腿⑥（**对照腿**）：串行调用时轮号仍是「库内 user 条数 + 1」（改前改后同值）。

    本条防的是「修并发把串行读坏了」：台账只在并发时把在途轮让开，串行必须退化回原式
    （``tests/api/test_turn_no_after_compaction_5_7.py`` 的 5 条回归钉是同一口径的接线层判据）。
    """
    chat = await _make_chat(db)
    assert await chat_routes.next_turn_no(db, chat.id) == 1
    await repository.add_message(db, chat_id=chat.id, role="user", content="第一问")
    await db.commit()
    assert await chat_routes.next_turn_no(db, chat.id) == 2
    await repository.add_message(db, chat_id=chat.id, role="user", content="第二问")
    await db.commit()
    assert await chat_routes.next_turn_no(db, chat.id) == 3


# --------------------------------------------------------------------------
# ⑦ 分支钉：没有会话时恒回第一轮
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_id为None时恒回第一轮(db):
    """腿⑦：``chat_id is None`` 仍然直接返回 1（既不查库、也不进台账）。"""
    assert await chat_routes.next_turn_no(db, None) == 1
    assert await chat_routes.next_turn_no(db, None) == 1, "重复调用也必须恒为 1"


# --------------------------------------------------------------------------
# ⑧ 残余窗口登记腿（跨引擎 ≈ 跨进程）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_跨引擎预留不共享_跨进程残余窗口登记(db, test_async_url):
    """腿⑧（**登记腿**）：换一台引擎（≈另一个后端进程的视角）看不到本进程的预留。

    断言的是**边界本身**：两台引擎各自算出同一个「下一个号」⇒ **多进程部署下仍会撞号**。
    本笔的台账是进程内的（按 ``AsyncEngine`` 分表），跨进程闭合需要给 ``chats`` 加
    持久化计数器列 + 出迁移（**scope 外**，已登记移交）。本腿绿 = 该窗口确实存在且被写明。
    """
    from app.core.config import settings
    from app.db.session import apply_sqlite_pragmas

    chat = await _make_chat(db, users=1)
    mine = await chat_routes.next_turn_no(db, chat.id)

    other = create_async_engine(test_async_url, pool_pre_ping=True)
    if settings.is_sqlite:
        apply_sqlite_pragmas(other.sync_engine)
    try:
        factory = async_sessionmaker(other, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            theirs = await chat_routes.next_turn_no(session, chat.id)
    finally:
        await other.dispose()

    assert mine == 2, f"库内 1 条用户消息 ⇒ 本引擎应为 2，实得 {mine}"
    assert theirs == 2, (
        f"另一台引擎实得 {theirs}；本腿断言的就是「跨引擎/跨进程仍会撞同一个号」这条"
        "残余窗口——若哪天它变成 3，说明跨进程闭合已经落地，本登记腿要随之改写"
    )


# --------------------------------------------------------------------------
# ⑨ 登记腿（审计发现 120-1）：分配了但彻底不落库 ⇒ 轮号空洞、不可回退
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_分配后彻底不落库_轮号空洞不可回退(db):
    """腿⑨（**登记腿 2**，审计发现 120-1）：分配过但从未落库的轮号**不回收** ⇒ 后续轮号有空洞。

    🔴 本腿钉的是**既定行为**（登记，不是修复）。触发路径：轮号在 ``build_turn_plan`` 就分配，
    用户消息在全轮**收尾**才落库（``回归修复`` 已登记「收尾前断开 ⇒ 后端什么都不落库」）
    ⇒ 客户端收尾前断开 / 本地 abort / 分配后走到不落库的出口，都会**白耗一个号**，且不可回退：
    库内计数恒为 0 时连做三次分配得 ``1/2/3``（= 2 个空洞），此后该会话轮号 =
    「库内 user 计数 + 1 + 空洞数」，直到进程重启或该条目被 ``_TURN_LEDGER_MAX_CHATS`` 淘汰。

    **为什么不做回收**（高水位只在落库成功后推进 / 按出口释放）：那需要「在途号集合 + 每个出口
    都释放」，漏一个出口是永久多占（与现状同级），**错放一个出口就把唯一性窗口重新打开**
    ⇒ 唯一性优先（那是 120 的全部目的），本腿负责让这条空洞是**被登记的行为**而非隐藏行为。
    """
    chat = await _make_chat(db)
    # 三次「分配后彻底不落库」（模拟：收尾前断开 / 本地 abort）
    allocated = [await chat_routes.next_turn_no(db, chat.id) for _ in range(3)]

    assert allocated == [1, 2, 3], f"三次分配应为 1/2/3（都不落库），实得 {allocated}"

    total = await db.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.chat_id == chat.id, Message.role == "user")
    )
    count = int(total or 0)
    assert count == 0, "本腿的前提：一条都没落库"
    assert allocated[-1] > count + 1, (
        f"第三次分配（{allocated[-1]}）应大于「库内计数 + 1」（{count + 1}）"
        " ⇒ 空洞确实存在且不回退；若相等说明台账把已分配的号回收了（与登记口径不符）"
    )

    # 空洞不可回退：事后补一条用户消息，下一个号仍不回落到被跳过的号上
    await repository.add_message(db, chat_id=chat.id, role="user", content="补一条用户消息")
    await db.commit()
    fourth = await chat_routes.next_turn_no(db, chat.id)
    assert fourth == 4, (
        f"补落一条（库内计数 = 1）之后应得 4（空洞不可回退），实得 {fourth}"
        " —— 该会话的轮号从此 = 库内计数 + 1 + 已空洞数"
    )
