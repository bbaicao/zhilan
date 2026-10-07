"""条目 4.4 · 配对不变量（I4/I5）与事件层出口的用例。

**为什么单列一个文件**：这一批断言全部落在「事件层的可见性」上——配对差集、词表漂移、
缺行出口、压缩事件绕不绕过计数。它们的失败模式是**假绿**（计数报 0 而事件真丢了），
所以必须表驱动 + 逐条钉，而不是靠人工比对日志。

| 断言（指令包 §4 编号） | 覆盖点 |
|---|---|
| 16 | **I4 差分**：``tool/call`` 与 ``tool/result`` 条数差 ∈ {0,1}（含恢复补平后） |
| 11–13 | ``attempt_id`` 载荷的**纯逻辑**（四键 / 哈希形状 / 三种终态 / 非法值抛） |
| 14 | 取消注入 ⇒ ``event_write_failures`` 增量 0、``event_incomplete`` 增量 ≥1、``reason == "cancelled"`` |
| 15 | 写失败注入 ⇒ ``event_write_failures`` 增量 ≥1、``reason == "write_failed"`` |
| 18 | ``KNOWN_EVENT_TYPES == SURFACE ∪ LOG_ONLY``（表驱动，防类型漂移） |
| 19 | **N1 生效**：压缩事件写失败 ⇒ ``event_write_failures`` 真的增（修前恒 0） |
| D7 | 「已声明未实装」清单：已有写入点与剩余 5 类**归属明确** |
| 附加 | 开关关着 ⇒ 事件表零行、``reason == "disabled"``（把假绿拆成看得见的三种情况） |
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import SessionEvent
from app.session import attempt as attempt_mod
from app.session import events as event_log


REPO_ROOT = Path(__file__).resolve().parents[3]
APP_DIR = REPO_ROOT / "backend" / "app"


# --------------------------------------------------------------------------
# 夹具：把**进程内**的两套计数在本用例前后还原（测试间的全局状态泄漏，4.3 已踩过）
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_counters():
    from app.agent import audit as agent_audit

    events_snapshot = event_log.failure_snapshot()
    audit_before = agent_audit.audit_write_failures()
    yield
    event_log.restore_event_write_failures(events_snapshot)
    agent_audit.restore_audit_write_failures(audit_before)


async def _chat(db, title: str = "配对不变量"):
    from app.session import repository as repo

    chat = await repo.create_chat(db, title=title)
    await db.commit()
    return chat


def _breaking_db(message: str = "boom"):
    """一个「一碰就炸」的假 db：``append_event`` 里必然走到 ``except Exception``。"""

    class _BreakingSession:
        def begin_nested(self):
            raise RuntimeError(message)

        def add(self, _row):
            raise RuntimeError(message)

        async def execute(self, *_args, **_kwargs):
            raise RuntimeError(message)

    return _BreakingSession()


def _cancelling_db():
    """一个「写入时抛 ``CancelledError``」的假 db（取消注入，不是写失败）。"""

    class _CancellingSession:
        def begin_nested(self):
            raise asyncio.CancelledError()

        def add(self, _row):  # pragma: no cover - begin_nested 先抛
            raise asyncio.CancelledError()

        async def execute(self, *_args, **_kwargs):
            raise asyncio.CancelledError()

    return _CancellingSession()


# --------------------------------------------------------------------------
# 断言 16：I4 差分
# --------------------------------------------------------------------------


async def _type_counts(db, chat_id: int) -> dict[str, int]:
    rows = await _rows(db, chat_id)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.type] = counts.get(row.type, 0) + 1
    return counts


async def _rows(db, chat_id: int) -> list[SessionEvent]:
    result = await db.execute(
        select(SessionEvent).where(SessionEvent.chat_id == chat_id).order_by(SessionEvent.seq)
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_i4_delta_is_zero_or_one_before_and_after_recovery(db):
    """I4：``tool/call`` 与 ``tool/result`` 的条数差**恒在 {0,1}**，恢复补平后回到相等。

    「差 1」是被允许的**在途态**（工具正在跑、结果还没落）——那是 I4 的原始口径；
    恢复的职责是把它**收口**（补一条合成的结果），不是让这个数字永远为 0。
    """
    from app.session import recover

    chat = await _chat(db, "I4 差分")
    for index in range(3):
        await event_log.append_event(
            db,
            chat.id,
            "tool/call",
            {"tool_call_id": f"call-{index}", "tool": "knowledge_search", "args": {}, "round": 1},
            turn=1,
            tool_call_id=f"call-{index}",
        )
    await event_log.append_event(
        db,
        chat.id,
        "tool/result",
        {"tool_call_id": "call-0", "tool": "knowledge_search", "ok": True},
        surface_op="append",
        turn=1,
        tool_call_id="call-0",
    )
    await db.commit()

    counts = await _type_counts(db, chat.id)
    assert counts["tool/call"] - counts["tool/result"] == 2
    # 恢复把两个缺口都补平 ⇒ 差集回到 0
    await recover.ensure_balanced(db, chat.id)
    await db.commit()

    counts = await _type_counts(db, chat.id)
    assert counts["tool/call"] == counts["tool/result"] == 3
    # 每一对都共用同一个 tool_call_id（I4 的配对键就是它）
    call_ids = {row.tool_call_id for row in await _rows(db, chat.id) if row.type == "tool/call"}
    result_ids = {row.tool_call_id for row in await _rows(db, chat.id) if row.type == "tool/result"}
    assert call_ids == result_ids


@pytest.mark.asyncio
async def test_recovery_does_not_invent_a_call_for_an_orphan_result(db):
    """反方向：孤立的 ``tool/result``（无 call）**不被恢复凭空改写**（老基线同口径）。

    恢复只补「缺的东西」，不许删/改已提交行——倒挂只能靠**不让它发生**（情形 B 的合成
    ``tool/call`` 就是为它准备的）。I4 的 {0,1} 界在「恢复可达的那条路径」上由
    ``test_i4_delta_is_zero_or_one_before_and_after_recovery`` 钉住。
    """
    from app.session import recover

    chat = await _chat(db, "倒挂不补")
    await event_log.append_event(
        db,
        chat.id,
        "tool/result",
        {"tool_call_id": "orphan", "tool": "t", "ok": False},
        surface_op="append",
        turn=1,
        tool_call_id="orphan",
    )
    await db.commit()

    assert await recover.ensure_balanced(db, chat.id) == []
    counts = await _type_counts(db, chat.id)
    assert counts.get("tool/call", 0) == 0
    assert counts["tool/result"] == 1, "已提交行一个字都不许动"


# --------------------------------------------------------------------------
# 断言 11–13：attempt 载荷的纯逻辑（不碰 db）
# --------------------------------------------------------------------------


def test_new_attempt_id_shape_and_uniqueness():
    """``uuid4().hex[:12]``：短、够唯一、不带时间戳（同毫秒并发会撞）。"""
    ids = {attempt_mod.new_attempt_id() for _ in range(300)}
    assert len(ids) == 300
    assert all(len(value) == attempt_mod.ATTEMPT_ID_LEN for value in ids)
    assert all(value.isalnum() and value == value.lower() for value in ids)


def test_attempt_payload_has_four_keys_and_rejects_unknown_outcome():
    """断言 13：键 ``{attempt_id, provider, model, outcome}``；非法 ``outcome`` **直接抛**。"""
    payload = attempt_mod.attempt_payload(
        "abc123", provider="openai", model="gpt-x", outcome=attempt_mod.ATTEMPT_COMMITTED
    )
    assert set(payload) == {"attempt_id", "provider", "model", "outcome"}
    assert payload["outcome"] == "committed"
    # 取不到型号时给空串（绝不因为读不到型号而写不出载荷）
    empty = attempt_mod.attempt_payload("abc123", outcome=attempt_mod.ATTEMPT_ABORTED)
    assert empty["provider"] == "" and empty["model"] == ""

    with pytest.raises(ValueError):
        attempt_mod.attempt_payload("abc123", outcome="unknown")
    assert set(attempt_mod.ATTEMPT_OUTCOMES) == {"committed", "interrupted", "aborted"}


def test_interrupted_payload_has_four_keys_and_a_content_hash():
    """断言 12：四键齐；``delivered_text_hash`` 以 ``sha256:`` 开头且长度 = 前缀 + 16。"""
    payload = attempt_mod.interrupted_payload("abc123", "已经交付的一半", reason="stopped")
    assert set(payload) == {
        "attempt_id",
        "reason",
        "delivered_chars",
        "delivered_text_hash",
    }
    assert payload["delivered_chars"] == len("已经交付的一半")
    assert payload["delivered_text_hash"].startswith(attempt_mod.HASH_PREFIX)
    assert len(payload["delivered_text_hash"]) == len(attempt_mod.HASH_PREFIX) + 16
    # 空文本**也给哈希**（None 与「没记录」分不开，而对账要的是「记录了什么」）
    empty = attempt_mod.delivered_fingerprint("")
    assert empty["delivered_chars"] == 0
    assert empty["delivered_text_hash"] is not None
    assert empty["delivered_text_hash"].startswith(attempt_mod.HASH_PREFIX)


def test_delivered_fingerprint_is_content_bound_not_length_bound():
    """字符数能对上、内容被改写的情况必须能被判出来（否则这条凭证没有意义）。"""
    same_len_a = attempt_mod.delivered_fingerprint("abcd")
    same_len_b = attempt_mod.delivered_fingerprint("abce")
    assert same_len_a["delivered_chars"] == same_len_b["delivered_chars"]
    assert same_len_a["delivered_text_hash"] != same_len_b["delivered_text_hash"]


# --------------------------------------------------------------------------
# 断言 14 / 15：两条独立计数 + 封闭 reason（按轮归属）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_counts_as_incomplete_but_not_as_write_failure(db):
    """断言 14：取消 ⇒ ``event_write_failures`` 增量 **0**、``event_incomplete`` 增量 ≥1、reason=cancelled。

    ``CancelledError`` 是 ``BaseException`` ⇒ 原先**既不计数也无出口**（``回归修复``）；
    现在它有自己的桶，且必须**原样抛出**（取消绝不许被吞）。
    """
    chat = await _chat(db, "取消注入")
    turn_key = (chat.id, 1)
    before = event_log.failure_snapshot()
    total_before = event_log.event_incomplete_total()

    with pytest.raises(asyncio.CancelledError):
        await event_log.safe_append_event(
            _cancelling_db(), chat.id, "turn/end", {"kind": "error"}, turn=1, turn_key=turn_key
        )

    assert event_log.event_write_failures() == before.total, "取消不是写失败"
    incomplete, reason = event_log.turn_event_incomplete(turn_key)
    assert incomplete >= 1
    assert reason == event_log.INCOMPLETE_CANCELLED
    # 进程级总数**只作日志**（不进任何轮的字段），但它是「取消确实被看见」的兜底出口
    assert event_log.event_incomplete_total() > total_before
    # 别的轮不受牵连（按轮归属，不是进程全局）
    assert event_log.turn_event_incomplete((chat.id, 2)) == (0, event_log.INCOMPLETE_NONE)


@pytest.mark.asyncio
async def test_write_failure_counts_and_reports_write_failed(db):
    """断言 15：写失败 ⇒ ``event_write_failures`` 增量 ≥1、reason=write_failed。"""
    chat = await _chat(db, "写失败注入")
    turn_key = (chat.id, 1)
    before = event_log.failure_snapshot()
    total_before = event_log.event_incomplete_total()

    result = await event_log.safe_append_event(
        _breaking_db(), chat.id, "turn/end", {"kind": "error"}, turn=1, turn_key=turn_key
    )
    assert result is None, "失败隔离：返回 None、不抛"

    assert event_log.event_write_failures() > before.total
    incomplete, reason = event_log.turn_event_incomplete(turn_key)
    assert incomplete >= 1
    assert reason == event_log.INCOMPLETE_WRITE_FAILED
    assert event_log.event_incomplete_total() > total_before


@pytest.mark.asyncio
async def test_disabled_log_is_visible_as_its_own_reason(db, monkeypatch):
    """开关关着 ⇒ 事件表**零行**，而这件事有一个**看得见的原因**（``disabled``）。

    为什么必须有这条：契约 ``02:312`` 明写「日志开关关闭时不计数」——
    于是「计数为 0」在**三种**情况下都会出现而事实相反：①开关关着 ②有代码绕过 ``safe_*``
    ③读取失败回落。本条把第 ① 种从「假绿」变成「看得见的一种情况」，**不新增 health 键**。
    """
    from app.core.config import settings

    chat = await _chat(db, "开关关着")
    turn_key = (chat.id, 1)
    monkeypatch.setattr(settings, "CHAT_EVENT_LOG_ENABLE", False)
    before = event_log.failure_snapshot()

    await event_log.safe_append_event(
        db, chat.id, "turn/start", {"turn": 1}, turn=1, turn_key=turn_key
    )
    await db.commit()

    assert await _rows(db, chat.id) == [], "开关关着时事件表必须一行都没有"
    assert event_log.event_write_failures() == before.total, "关着不是「写失败」"
    incomplete, reason = event_log.turn_event_incomplete(turn_key)
    assert reason == event_log.INCOMPLETE_DISABLED
    assert incomplete == 0, "关着是**预期**，不该算成「缺行」"


@pytest.mark.asyncio
async def test_reason_priority_prefers_write_failed_over_cancelled(db):
    """同一轮里既写失败又被取消 ⇒ 报 ``write_failed``（真报错优先；取消多半是它的下游）。"""
    chat = await _chat(db, "原因优先级")
    turn_key = (chat.id, 1)
    with pytest.raises(asyncio.CancelledError):
        await event_log.safe_append_event(
            _cancelling_db(), chat.id, "turn/end", {}, turn=1, turn_key=turn_key
        )
    await event_log.safe_append_event(
        _breaking_db(), chat.id, "turn/end", {}, turn=1, turn_key=turn_key
    )
    assert event_log.turn_event_incomplete(turn_key)[1] == event_log.INCOMPLETE_WRITE_FAILED


# --------------------------------------------------------------------------
# 断言 19：N1 —— 压缩事件写失败要**真的增**（修前恒 0）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compaction_event_failure_increments_the_counter(db, monkeypatch):
    """``回归修复``（N1）：压缩的 summary/end 是用**会抛**的 ``append_event`` 写的
    （SAVEPOINT 的原子性靠它），原先异常被外层 ``except`` 吞成一句 warning ⇒
    契约 ``02:312`` 的**唯一**可观测口径 ``event_write_failures`` 恒报 0（假绿）。
    """
    from app.session import compaction

    chat = await _chat(db, "压缩事件失败")
    turn_key = (chat.id, 1)
    before = event_log.failure_snapshot()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("注入：压缩事件写失败")

    monkeypatch.setattr(event_log, "append_event", _boom)
    outcome = compaction.SummaryOutcome(text="摘要", model="m")
    await compaction._log_compaction_events(
        db,
        chat.id,
        compaction_id="cm-1",
        reason="auto",
        from_seq=0,
        to_seq=3,
        outcome=outcome,
        turn=1,
        trace=None,
        opened=True,
    )

    assert event_log.event_write_failures() > before.total, "修前这里是恒 0（假绿）"
    assert event_log.turn_event_incomplete(turn_key)[1] == event_log.INCOMPLETE_WRITE_FAILED
    # 摘要/关闭事件一条都没落（SAVEPOINT 整体回滚）——原子性没有被这次改动破坏
    rows = await _rows(db, chat.id)
    assert [row.type for row in rows] == []


# --------------------------------------------------------------------------
# 断言 18 + D7：词表是全集、写入点是清单
# --------------------------------------------------------------------------


def test_known_event_types_is_exactly_surface_union_log_only():
    """断言 18：``KNOWN_EVENT_TYPES == SURFACE ∪ LOG_ONLY``（防类型漂移，表驱动）。"""
    assert event_log.KNOWN_EVENT_TYPES == frozenset(event_log.SURFACE_EVENT_TYPES) | frozenset(
        event_log.LOG_ONLY_EVENT_TYPES
    )
    # 两个词表不许有交集（一个事件不可能既表层又仅日志）
    assert not set(event_log.SURFACE_EVENT_TYPES) & set(event_log.LOG_ONLY_EVENT_TYPES)
    assert len(event_log.KNOWN_EVENT_TYPES) == len(event_log.SURFACE_EVENT_TYPES) + len(
        event_log.LOG_ONLY_EVENT_TYPES
    ), "两个词表都不许有重复项"


#: 「已声明但**尚未**实装写入点」的事件类型 → 归属（D7 的清单，**表驱动**）。
#: ⚠️ 这张表改了就等于改了验收口径：往里加一项必须同时写清归属阶段。
DECLARED_WITHOUT_WRITER = {
    "system/message": "阶段 5（上下文由事件派生时才有系统消息事件）",
    "step/start": "阶段 4.5.x（Agent 步骤留痕细分，与 agent_steps 同源）",
    "step/end": "阶段 4.5.x（同上）",
    "grade/decided": "阶段 5（检索打分子）",
    "chat/title": "阶段 4.5.x（会话标题自动生成）",
}


def _constant_values() -> dict[str, str]:
    """事件类型常量的「名字 → 值」表（``event_log.X`` / ``attempt_mod.X`` 两种写法都能解析）。"""
    table: dict[str, str] = {}
    for module in (event_log, attempt_mod):
        for name in dir(module):
            value = getattr(module, name)
            if isinstance(value, str) and "/" in value:
                table[name] = value
    return table


#: **表驱动**的写入点（AST 扫不到「事件类型」这个实参的场景，必须**显式登记**）。
#: 不登记的话，这条用例会把自己的**扫描盲区**读成「这一类没实装」，然后去改清单——
#: 那是「用假事实修文档」。每项的第二个元素是**证明它真的存在**的字面量所在文件。
DYNAMIC_WRITE_POINTS = {
    "citation/used": "app/observe/citation.py",  # log_citation_events 按 events_for() 的表分派
    "citation/unresolved": "app/observe/citation.py",  # 同上（判不出的引用）
}


def test_dynamic_write_points_are_really_written():
    """登记为「表驱动写入点」的类型，必须在**它声明的那个文件里**真的出现（防登记过期）。"""
    for event_type, owner in DYNAMIC_WRITE_POINTS.items():
        source = (REPO_ROOT / "backend" / owner).read_text(encoding="utf-8")
        assert event_type in source, (
            f"{event_type} 登记为 {owner} 的表驱动写入点，但该文件里找不到这个字面量——"
            "登记过期了（要么它真的没实装，要么写点搬走了）"
        )


def _writer_event_types() -> set[str]:
    """扫 ``backend/app`` 里**真的调用**了事件写入的调用点，收集写入的事件类型。

    为什么要用 AST 而不是 grep：看一眼「``"tool/result"`` 出现在哪」会被注释、文档字符串、
    常量定义和**读**事件的地方（``list_events_by_type``）带偏；这里只看
    ``append_event`` / ``safe_append_event`` 调用的**第 3 个位置**（``event_type``），
    只认三种能静态定值的形态：字符串字面量、``模块.常量``、``本文件模块级常量名``。
    其余形态（如把类型名当变量传进来）**扫不到** ⇒ 必须进 :data:`DYNAMIC_WRITE_POINTS`。
    """
    constants = _constant_values()
    found: set[str] = set()
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        file_constants: dict[str, str] = {}
        for stmt in tree.body:
            if (
                isinstance(stmt, ast.Assign)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        file_constants[target.id] = stmt.value.value
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name not in ("append_event", "safe_append_event"):
                continue
            if len(node.args) < 3:
                continue
            arg = node.args[2]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
            elif isinstance(arg, ast.Attribute) and arg.attr in constants:
                found.add(constants[arg.attr])
            elif isinstance(arg, ast.Name) and arg.id in file_constants:
                found.add(file_constants[arg.id])
    return found | set(DYNAMIC_WRITE_POINTS)


def test_declared_event_types_without_writers_match_the_registered_list():
    """D7：词表是**面向未来的全集**，已有写入点与其余 5 类**归属明确**。

    本条补的两类（``assistant/attempt`` / ``recovery/synthesized``）必须真的出现写入点；
    剩下的必须**恰好**等于 :data:`DECLARED_WITHOUT_WRITER`——多一类（有人把归属忘了）或
    少一类（有人悄悄补了写入点却没更新清单）都会让这条用例红。
    """
    writers = _writer_event_types()
    assert {"assistant/attempt", "recovery/synthesized", "rewrite/applied"} <= writers, (
        f"已实现的三类必须真的有写入点，实测写入点集合：{sorted(writers)}"
    )
    assert writers <= event_log.KNOWN_EVENT_TYPES, (
        f"写入点里出现了词表外的事件类型：{sorted(writers - event_log.KNOWN_EVENT_TYPES)}"
    )
    declared = event_log.KNOWN_EVENT_TYPES - writers
    assert declared == set(DECLARED_WITHOUT_WRITER), (
        "「已声明未实装」清单与代码不符：\n"
        f"  代码里没有写入点的：{sorted(declared)}\n"
        f"  清单里登记的：      {sorted(DECLARED_WITHOUT_WRITER)}"
    )
    # 清单里的每一项都要写清归属（空值等于没登记）
    assert all(owner.strip() for owner in DECLARED_WITHOUT_WRITER.values())


# --------------------------------------------------------------------------
# 4.4.4：Agent 的工具任务用**自己的 session**（`回归修复` 后半的根治）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_tool_task_uses_its_own_session_not_the_request_one(db, monkeypatch):
    """4.4.4 的**目标断言**：工具执行不再与消费端任务共用请求级 ``AsyncSession``。

    为什么必须用「**看对象身份**」而不是「跑一遍没报错」来验：并发共用 session 的症状是
    **随机的**（``This session is in 'prepared' state`` / ``readexactly() while another
    coroutine is already waiting``），只在竞态窗口里出现——「这次没炸」证明不了「不再共用」。
    这里取工具侧**真的拿到**的那个会话（``knowledge_search`` 的检索回调第一个形参就是它），
    直接断言它与请求会话**不是同一个对象**、但绑在**同一个引擎**上（同库同方言）。

    实现口径（``app.db.session.new_independent_session``）：按**请求会话所属的引擎**新建，
    而不是用全局 ``SessionLocal`` —— 后者在单测里会跨事件循环（连接池绑定在创建它的循环上）。
    """
    from cubeloop.providers import FauxProvider, faux_assistant_message, faux_tool_call

    from app.agent.tools import knowledge_search
    from app.api import chat_routes
    from app.session import repository as repo

    chat = await repo.create_chat(db, title="独立会话")
    await db.commit()

    seen: dict[str, object] = {}

    async def fake_retrieve(session, question, scope, *, stats=None):  # noqa: ANN001
        seen["session"] = session  # ← 工具侧**真的**拿到的那个 session
        return [], "", None

    monkeypatch.setattr(knowledge_search, "_default_retrieve", lambda: fake_retrieve)
    provider = FauxProvider(provider_id="faux")
    provider.set_responses(
        [
            faux_assistant_message([faux_tool_call("knowledge_search", {"query": "蓝鲸"})]),
            faux_assistant_message("可以作答了"),
        ]
    )

    plan = chat_routes.TurnPlan(
        chat=chat,
        question="蓝鲸是什么",
        images=[],
        history=[],
        filters=None,
        skill_ids=[],
        source_type="external",
        kind="normal",
        answer_source=None,
        agent_mode="react",
    )
    stream = chat_routes.run_agent_turn(plan, provider.model("faux-chat"), db)
    async for _kind, _payload in stream:
        if "session" in seen:
            break
    await stream.aclose()

    tool_session = seen.get("session")
    assert tool_session is not None, "工具没跑到检索（用例本身失效）"
    assert tool_session is not db, "工具任务必须用自己的会话，不能与请求级会话共用"
    assert tool_session.bind is db.bind, "两条会话必须绑在同一个引擎上（同库，别跨循环用全局引擎）"
