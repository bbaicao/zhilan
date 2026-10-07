"""轮号分配与事件归属键（阶段 1 · 条目 1.5 的从属件；**回归修复 切分 S1**）。

本模块 = app/api/chat_routes.py 的**低风险切分**产物（087 · S1）：把「轮号分配」
（next_turn_no）与「事件归属键」（turn_key_of）连同 回归修复 的**进程内预留台账**
整段搬到此处；chat_routes 保留**同名 re-export** ⇒ 三条外部面（具名 import、
chat_routes.next_turn_no(...) 属性访问、monkeypatch.setattr(chat_routes, "next_turn_no", …)
栅栏）逐字不变。

**本迁移只搬家、不改逻辑**（⛔ 没有任何行为变更）：
- 台账 _TURN_LEDGER 是**按 AsyncEngine 分表**的模块级单例（WeakKeyDictionary[AsyncEngine,
  dict[int, tuple[int, int]]]）：生产态全进程一台引擎 ⇒ 等价于「按进程共享」；单测里每个用例
  一台临时引擎（SQLite 档 rowid 复用会让 chat_id 从 1 重来）⇒ 台账**不跨用例泄漏**。
  此语义随迁，⛔ 不得改成模块级裸 dict。
- _reserve_turn 内**无 await**（= 分配的全部临界区）；next_turn_no 每次分配只发一条
  SELECT COUNT(*)、**不用 FOR UPDATE**。
- 回归修复 的两条**残余窗口**（① 多进程 / 多 worker 仍可能撞号；② 分配了没落库 ⇒
  轮号空洞且**不可回退**）照原样登记在 next_turn_no 的 docstring 里，⛔ 不得据此写
  「并发丢事件已全部消除 / 多进程也安全」。

分包边界：api → … → session（本模块在下层，**不 import app.api 包**，无反向依赖）。
"""

from __future__ import annotations

from weakref import WeakKeyDictionary

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import Message


def turn_key_of(chat_id: int | None, turn: int | None) -> tuple[int | None, int | None]:
    """本轮事件写入的**归属键** ``(chat_id, turn)``。

    统一成一个函数的原因：``turn_key`` 决定「本轮日志有没有缺行」这个信号归谁
    （``回归修复`` 的按轮归属），散落的字面量迟早有一处写成 ``(turn, chat_id)``
    或漏传——漏传就是「信号说假话」。
    """
    return (chat_id, turn)


# ---------------------------------------------------------------------------
# 回归修复：轮号分配的**进程内预留台账**（同会话并发提问不再撞号）
# ---------------------------------------------------------------------------


#: 轮号预留台账：键 = 该会话所在的 ``AsyncEngine``，值 = ``{chat_id: (基准计数, 已分配最大轮号)}``。
#:
#: **为什么按引擎分表**（而不是一个模块级全局 dict）：① 生产态（``app/db/session.py::SessionLocal``）
#: 全进程只有一台引擎 ⇒ 等价于「按进程共享」，同会话的并发请求看得到彼此的预留；
#: ② 单测里 ``tests/conftest.py::engine`` 是**每个用例一台临时引擎**（SQLite 档还会因
#: rowid 复用让 ``chat_id`` 从 1 重新开始）⇒ 按引擎分表让台账**不跨用例泄漏**，
#: 既有「库内计数 + 1」读数一条都不受影响。
_TURN_LEDGER: "WeakKeyDictionary[AsyncEngine, dict[int, tuple[int, int]]]" = WeakKeyDictionary()

#: 台账最多跟踪多少个会话；超限淘汰**最早插入**的一条（``dict`` 保序 ⇒ O(1)）。
#: 取值依据照同仓 ``session/compaction.py::BREAKER_MAX_TRACKED`` = 512 的先例：单进程
#: 「同时有在途轮」的会话数远小于它 ⇒ 被淘汰的只可能是早已落库完成的旧条目。
_TURN_LEDGER_MAX_CHATS = 512


def _turn_ledger_of(db: AsyncSession) -> dict[int, tuple[int, int]] | None:
    """取（或建）``db`` 所属引擎的台账；取不到引擎返回 ``None``（调用方回落纯计数）。"""
    bind = getattr(db, "bind", None)
    if not isinstance(bind, AsyncEngine):
        try:
            bind = db.get_bind()
        except Exception:  # noqa: BLE001 —— 取不到就当没有（调用方回落），绝不因为台账炸穿分配
            return None
    if not isinstance(bind, AsyncEngine):
        return None
    ledger = _TURN_LEDGER.get(bind)
    if ledger is None:
        ledger = {}
        _TURN_LEDGER[bind] = ledger
    return ledger


def _reserve_turn(ledger: dict[int, tuple[int, int]], chat_id: int, total: int) -> int:
    """算轮号 + 记账，**本函数无 ``await``**（= 本条目的全部临界区）。

    事件循环只在 ``await`` 处切换协程 ⇒ 这段同步代码单进程内不可能被打断；
    **释放点 = 函数返回**（不持数据库行锁、不跨请求存活；``next_turn_no`` 之外零状态）。

    轮号 = ``max(库内 user 计数 + 1, 上一次分配出去的轮号 + 1)``：串行场景退化成
    「库内计数 + 1」（与改前逐字同值），并发场景把在途轮让开。

    ``(基准计数, 最大轮号)`` 里的**基准计数**用来判「条目是否还属于当前这代人」：
    库内计数比基准更小 ⇒ 表被清过 / 行号被复用 ⇒ 重新起算（否则旧高水位会把新会话
    的轮号往上顶）。**产线上不构成路径**（``messages`` 只增不删），是为 SQLite 桌面档的
    rowid 复用与测试清表留的安全阀。
    """
    entry = ledger.pop(chat_id, None)
    if entry is None or total < entry[0]:
        base, turn = total, total + 1
    else:
        base, turn = entry[0], max(total + 1, entry[1] + 1)
    ledger[chat_id] = (base, turn)
    while len(ledger) > _TURN_LEDGER_MAX_CHATS:
        # 淘汰最早插入的一条（本函数刚写的那条在尾部 ⇒ 不会被自己淘汰）
        del ledger[next(iter(ledger))]
    return turn


async def next_turn_no(db: AsyncSession, chat_id: int | None) -> int:
    """本轮轮号 = **库内已有的用户消息条数 + 1**（⚠️ **与压缩无关**，这是本函数存在的全部理由）。

    `5.7` 的 33 轮真机读数实测复现过这条缺陷：**33 轮只落出 13 个不同 `turn`**，
    `turn=1..13` 每个被 7 条事件共用。成因是初版写成「数**发给模型的 history** 里的 user 条数」，
    而长会话压缩一旦发生，历史开头会被 ``compaction.summary_pair`` 换成
    「摘要 user + assistant **一对**」、早期 user 行被**替换**掉 ⇒ 那个数会**回跳**。

    为什么必须修（影响面不是「日志好看」）：

    - ``(chat_id, turn)`` 是 ``4.4`` 的 ``events_incomplete`` **归属桶**
      （``app/session/events.py`` 的 ``_TURN_FAILURES``）⇒ 桶串轮 = 「本轮缺行」信号说假话；
    - ``agent_runs.meta.turn`` 也取它 ⇒ 审计面的轮号跟着错。

    ⚠️ ``阶段4/证据/回归检查3安全审计压缩.md`` 的 **B7** 早已登记过同一缺陷并声称「已修 + 已加回归钉」，
    但**实际代码与用例都不存在**（`git log` 里没有任何一笔改过它）⇒ 本条把它真正落地，
    并把「读数」与「旧写法确实回跳」两件事一起钉进
    ``tests/api/test_turn_no_after_compaction_5_7.py``（红→绿自证）。

    代价：每轮多一次 ``COUNT(*)``（``chat_id`` 上有索引，量级可忽略）。

    ---

    **回归修复（2026-09-28）**：同一会话的**并发提问**原先会撞号——两个 ``/chat/stream``
    都在对方 ``persist_turn`` 提交前 COUNT ⇒ 同 ``turn`` ⇒ 两轮事件混进同一个
    ``(chat_id, turn)`` 桶（``events_incomplete`` 说假话）、``agent_runs.meta.turn`` 也跟着串。

    修法 = **分配处串到一条无 await 的临界区**（:func:`_reserve_turn`）：库内计数只提供
    「基线」，「上一次分配出去的轮号」提供「在途避让」。串行时读数与改前**逐字同值**；并发时
    第二个请求拿 ``count + 2``（不是重复的 ``count + 1``）。**临界区范围 = 一次 dict 读改写**
    （``_reserve_turn`` 内无 await ⇒ 单事件循环内不可能被切走），**释放点 = 该函数返回**：
    ``next_turn_no`` 不持任何数据库行锁、返回后不残留任何「本轮持锁」状态。

    ⚠️ **为什么不是 ``SELECT … FOR UPDATE``**（计划原稿的写法）：轮号在**落库之前很久**就要用
    （``build_turn_plan`` 定的 ``turn`` 要喂给本轮全部事件），而 ``persist_turn`` 在全轮**收尾**才提交
    ⇒ 若用请求会话的行锁，锁必须**跨整个请求**持有（把并发轮串行化，正是本条目要点 ② 禁止的），
    且 SQLite 桌面档根本不支持 ``FOR UPDATE``。要「短临界区 + 跨进程持久预留」，只能给 ``chats``
    加一列计数器并出迁移——**scope 外**，已登记为残余窗口（见下条）。

    🔴 **已登记残余窗口 1（不得含糊）**：本条的预留台账是**进程内**的（按 ``AsyncEngine`` 分表）
    ⇒ **多进程 / 多 worker 部署**（同一库、两个后端进程）下两个进程仍可能拿到同一轮号；
    **桌面档（SQLite）与 dev 单进程 uvicorn 内已闭合**；跨进程闭合需要持久化计数器列 + 迁移。

    🔴 **已登记残余窗口 2：分配了但没落库 ⇒ 轮号出现空洞，且不可回退**（审计发现 120-1）。
    轮号在 :func:`build_turn_plan` 就分配，而用户消息在**全轮收尾**才落库（`回归修复` 已登记
    「收尾前断开 ⇒ 后端什么都不落库」）⇒ 客户端收尾前断开 / 本地 abort / 分配后走到不落库的出口，
    都会**白耗一个号**：库内计数恒为 0 时连做三次分配得 `1/2/3`，此后该会话的轮号 =
    「库内 user 计数 + 1 + 已空洞数」，直到进程重启或该条目被 `_TURN_LEDGER_MAX_CHATS` 淘汰。
    **影响面**：`(chat_id, turn)` 的**唯一性不受影响**（本条目的的目的已达成）；受影响的是
    「轮号 == 用户消息序号 + 1」这条既有可读契约，`agent_runs.meta.turn` 与事件分桶里会出现
    从未落库的号。**为什么不做回收**（高水位只在落库成功后推进 / 按出口释放）：那需要一张
    「在途号集合」+ **每个出口**（成功 / 停止 / 错误 / 断开 / 同步 / 澄清 / 拒答）都释放，
    漏一个出口就是永久多占（与现状同级），**错放一个出口就把本条目刚闭合的唯一性窗口重新打开**
    ⇒ 本轮取「如实登记 + 判据钉住」，不拿唯一性换好看（见载体腿⑨）。
    """
    if chat_id is None:
        return 1
    total = await db.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.chat_id == chat_id, Message.role == "user")
    )
    count = int(total or 0)
    ledger = _turn_ledger_of(db)
    if ledger is None:
        # 取不到引擎（非常规会话）⇒ 退回纯计数：与改前逐字同行为，绝不因为台账炸穿分配
        return count + 1
    return _reserve_turn(ledger, chat_id, count)
