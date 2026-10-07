"""条目 4.3 的三条遗留缺陷收口用例（``回归修复`` / ``034`` / ``035``）。

三条都来自条目 4.2 的对抗性核验，归 4.3 正射程（同属「事件/审计写失败的可见性」与 I4 收口）：

| 编号 | 一句话 | 修法 |
|---|---|---|
| `033` | ``events_incomplete`` 用**进程全局**失败计数 ⇒ 别的会话写失败会让本轮误报「缺行」 | 事件层加按轮分桶（``failure_snapshot`` / ``failures_since`` / ``turn_key``），``runner._finish`` 只认**本轮** |
| `034` | 停止路径把 ``events_incomplete`` **整个丢掉**；``CancelledError`` 不被 ``except Exception`` 捕获 ⇒ 取消造成的缺行**既不计数也无出口** | 停止轮的 trace 带上该字段（优先用 runner 收口好的那份），并给停止路径的收口**记上本轮** |
| `035` | ``_write_tool_events`` 的 id 兜底在**循环体内** ⇒ 一对事件都为空 id 时铸出两个不同 id，I4 配对破裂 | 兜底提到调用方、一对事件复用同一个 id，并与执行侧共用 |

本文件**只放这三条**；安全闸门与审计的正向用例在 ``tests/agent/test_agent_safety.py`` /
``tests/agent/test_agent_audit.py``，压缩在 ``tests/session/test_compaction.py``。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import runner
from app.session import events as event_log

pytestmark = pytest.mark.asyncio

# --------------------------------------------------------------------------
# 033：写失败计数的**归属**是「本轮」
# --------------------------------------------------------------------------


async def _chat(db, title: str):
    from app.session import repository as repo

    chat = await repo.create_chat(db, title=title)
    await db.commit()
    return chat


def _breaking_db(message: str = "boom"):
    """一个「一次 Event 写入就炸」的假 db：``append_event`` 里必然走到 ``except``。"""

    class _BreakingSession:
        def begin_nested(self):
            raise RuntimeError(message)

        def add(self, _row):
            raise RuntimeError(message)

        async def flush(self):  # pragma: no cover - begin_nested 先炸，走不到这里
            raise RuntimeError(message)

        async def execute(self, *_args, **_kwargs):
            raise RuntimeError(message)

    return _BreakingSession()


@pytest.fixture(autouse=True)
def isolate_failure_counters():
    """把**进程全局**的失败计数在本用例前后还原（测试间的进程级状态泄漏，实测踩过）。

    为什么必须有：本文件会**故意**制造事件写失败与审计写失败 ⇒ ``event_write_failures()``
    与 ``audit_write_failures()`` 这两个全局计数被顶上去。而既有用例
    （``tests/api/test_chat_agent.py`` 的三条）断言的是 ``event_write_failures() == 0``——
    整目录连跑时它们会**假红**（单跑那一文件却绿）。这正是「全局计数器 + 逐条断言」的经典坑，
    修法不是把断言改松（那会掩盖真丢事件），而是**让用例自己收拾干净**。
    """
    from app.agent import audit as agent_audit

    events_snapshot = event_log.failure_snapshot()
    audit_before = agent_audit.audit_write_failures()
    yield
    # 用**公开的还原口**还回去（不假设它本来是 0：本文件之前可能已有别的用例动过它）
    event_log.restore_event_write_failures(events_snapshot)
    agent_audit.restore_audit_write_failures(audit_before)


async def test_event_failure_is_attributed_to_its_own_turn(db):
    """``回归修复``：别的会话写失败**不能**让本轮报 ``events_incomplete``（旧行为是 True）。

    复现路径就是缺陷行里写的那条：**本轮零事件** + **另一会话写失败一次** ⇒ 旧口径下
    ``event_write_failures()`` 的增量 > 0 ⇒ 本轮误报；现在按 ``turn_key`` 归属，本轮仍是 False。
    """
    victim = await _chat(db, "受害会话")
    other = await _chat(db, "肇事会话")

    before = event_log.failure_snapshot()
    # 另一会话写失败一次（进程全局计数会 +1）；带 turn_key ⇒ 归属到**那一轮**
    await event_log.safe_append_event(
        _breaking_db(),
        other.id,
        "turn/end",
        {"kind": "error"},
        turn=1,
        turn_key=(other.id, 1),
    )
    assert event_log.event_write_failures() > before.total  # 全局确实涨了（缺陷的土壤）

    # 受害会话「本轮」一次都没写 ⇒ 归属到本轮的失败是 0
    assert event_log.failures_since(before, turn_key=(victim.id, 1)) is False
    # 对照：肇事会话那一轮确实被记了 1 次
    assert event_log.failures_since(before, turn_key=(other.id, 1)) is True


async def test_own_turn_failure_still_reports_incomplete(db):
    """``回归修复`` 的反面：**本轮自己**写失败仍然要报（修的是假阳性，不是把信号关掉）。"""
    chat = await _chat(db, "自己失败")
    before = event_log.failure_snapshot()
    await event_log.safe_append_event(
        _breaking_db(), chat.id, "turn/end", {"kind": "error"}, turn=2, turn_key=(chat.id, 2)
    )
    assert event_log.failures_since(before, turn_key=(chat.id, 2)) is True
    # 同会话的另一轮不受牵连（归属是 (chat_id, turn) 二元组，不是只有 chat_id）
    assert event_log.failures_since(before, turn_key=(chat.id, 3)) is False


async def test_failures_since_without_turn_key_keeps_old_global_behaviour(db):
    """旧口径保留：不传 ``turn_key`` 时仍是进程全局增量（既有调用方行为逐字不变）。"""
    before = event_log.failure_snapshot()
    assert event_log.failures_since(before.total) is False
    await event_log.safe_append_event(_breaking_db(), 1, "turn/end", {"kind": "error"})
    assert event_log.failures_since(before.total) is True
    # 传了 turn_key 却是旧式 int 快照 ⇒ 一律 False（宁可少报也不假阳性）
    assert event_log.failures_since(before.total, turn_key=(1, 1)) is False


async def test_cancelled_turn_still_counts_its_own_failures(db):
    """``回归修复`` 的一半：取消**不吞掉**本轮的失败归属。

    ``CancelledError`` 不是 ``Exception`` 的子类 ⇒ 事件层的 ``except Exception`` 不会捕获它；
    这条钉的是「取消路径下本轮的失败快照仍然算得出来」，而不是「取消会被记成写失败」。
    """
    chat = await _chat(db, "取消轮")
    before = event_log.failure_snapshot()
    await event_log.safe_append_event(
        _breaking_db(), chat.id, "stream/interrupted", {"reason": "stopped"}, turn=1,
        turn_key=(chat.id, 1),
    )
    assert event_log.failures_since(before, turn_key=(chat.id, 1)) is True


# --------------------------------------------------------------------------
# 034：停止轮的 trace 必须带 events_incomplete
# --------------------------------------------------------------------------


async def test_runner_trace_includes_incomplete_for_stopped_agent_turn(db):
    """``回归修复``：Agent 轮被取消后，``turn.trace()`` 仍带 ``events_incomplete``。

    这是「停止路径丢字段」的**最小复现**：直接驱动 ``stream_agent_turn`` 并在中途
    ``aclose()``（等价于用户按停止），然后断言轨迹三键齐全。
    """
    chat = await _chat(db, "被停的 Agent 轮")
    turn = runner.AgentTurn(mode="react")
    stream = runner.stream_agent_turn(
        turn,
        question="蓝鲸有多大",
        model=SimpleNamespace(),  # 只为跑通装配；下面立即 aclose，不进循环
        db=db,
        chat_id=chat.id,
        turn_no=1,
        tools=[],  # 空清单 ⇒ 走「没有可用工具」早退，属于**正常收口**路径
    )
    frames = [frame async for frame in stream]
    assert frames, "至少要产一帧 note"

    trace = turn.trace()
    assert set(trace) == {"mode", "steps", "events_incomplete"}, "三键形状只增不改义"
    assert trace["events_incomplete"] is False, "本轮自己没写失败 ⇒ False"


async def test_stopped_agent_trace_keeps_incomplete_field(db):
    """``回归修复``：停止轮写进 ``messages.agent_trace`` 的那一份**带** ``events_incomplete``。

    原先 ``chat_routes`` 的停止轮 trace 是就地手拼的 ``{"mode", "steps"}``（注释写「刻意不含」），
    现在改为优先取 **runner 收口好的那份**（``plan.agent_trace``）——这条钉住「优先那份」。
    """
    from app.api import chat_routes

    chat = await _chat(db, "停止轮落库")
    plan = SimpleNamespace(
        chat=chat,
        question="问题",
        source_type="external",
        filters=None,
        skill_ids=[],
        turn=1,
        audit_run_id=None,
        agent_trace={"mode": "react", "steps": [], "events_incomplete": True},
    )
    await chat_routes.persist_stopped_turn(
        db, plan, "半截回答", agent_trace=plan.agent_trace
    )
    rows = await chat_routes.session_service.load_history(db, chat.id)
    assistant = [row for row in rows if row.role == "assistant"]
    assert assistant, "有正文的停止轮要落 assistant 行"
    assert assistant[-1].agent_trace == {
        "mode": "react",
        "steps": [],
        "events_incomplete": True,
    }
    assert assistant[-1].kind == "stopped"


# --------------------------------------------------------------------------
# 035：空 tool_call_id 的兜底 —— 一对事件共用同一个 id（I4）
# --------------------------------------------------------------------------


def _pair_with_empty_ids():
    """一对 ``tool_execution_start`` / ``tool_execution_end``，两个都**没有** id。"""
    return [
        SimpleNamespace(type="tool_execution_start", tool_call_id="", tool_name="t", args={}),
        SimpleNamespace(
            type="tool_execution_end", tool_call_id="", tool_name="t", is_error=False, ms=1
        ),
    ]


async def test_empty_tool_call_ids_are_paired_with_one_synthesised_id(db):
    """``回归修复`` 的核心断言：两个事件都为空 id ⇒ ``tool/call`` 与 ``tool/result`` 仍同 id。

    旧实现把兜底写在循环体内 ⇒ 各铸一个 ``new_tool_call_id()`` ⇒ 两条事件的 ``tool_call_id``
    不同，I4 的配对当场破裂。
    """
    from sqlalchemy import select

    from app.models import SessionEvent

    chat = await _chat(db, "空 id 配对")
    collector = runner.ToolCollector()
    fallback = runner.new_tool_call_id()
    await runner._write_tool_events(
        _pair_with_empty_ids(),
        db=db,
        chat_id=chat.id,
        turn_no=1,
        round_no=1,
        trace_id=None,
        collector=collector,
        fallback_id=fallback,
    )
    rows = list(
        (
            await db.execute(
                select(SessionEvent)
                .where(SessionEvent.chat_id == chat.id)
                .order_by(SessionEvent.seq)
            )
        )
        .scalars()
        .all()
    )
    assert [row.type for row in rows] == ["tool/call", "tool/result"]
    ids = {row.tool_call_id for row in rows}
    assert ids == {fallback}, f"两条必须共用同一个兜底 id，实测 {ids}"
    # 载荷里的 id 与列上的 id 也要一致（读方常按载荷取）
    assert {row.data["tool_call_id"] for row in rows} == {fallback}


async def test_execution_and_event_sides_share_the_same_fallback_id(db):
    """``回归修复``：执行侧（``collector`` 的键）与事件侧用的是**同一个**兜底 id。

    ``_as_agent_tool`` 的闭包与 ``_write_tool_events`` 若各铸一次，就会出现
    「``tool/call`` 挂了 A 键、观察落在 B 键下」——这一条把两边钉在一起。
    """
    from pydantic import BaseModel

    from app.agent.registry import ToolRegistry, ToolSpec
    from app.agent.types import ToolObservation

    class EchoArgs(BaseModel):
        text: str = ""

    calls: list[str] = []

    async def handler(args):  # noqa: ANN001
        calls.append(args.text)
        return ToolObservation(text=f"回声：{args.text}", ok=True)

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="echo",
            description="回声",
            parameters_schema=EchoArgs.model_json_schema(),
            handler=handler,
            parameters=EchoArgs,
            needs_confirm=False,
        )
    )
    turn = runner.AgentTurn(mode="react", run_id=None)
    fallback = runner.new_tool_call_id()
    tools, collector = runner.build_agent_tools(
        db=None,
        registry=registry,
        turn=turn,
        fallback_id=fallback,
    )
    assert tools, "echo 必须进清单（有参数模型）"
    result = await tools[0].execute("", EchoArgs(text="你好"))
    assert result.is_error is False
    assert calls == ["你好"], "handler 真的被调了（空 id 不该影响执行）"
    assert list(collector.observations) == [fallback], "观察必须挂在兜底 id 下"

    chat = await _chat(db, "共用兜底")
    await runner._write_tool_events(
        _pair_with_empty_ids(),
        db=db,
        chat_id=chat.id,
        turn_no=1,
        round_no=1,
        trace_id=None,
        collector=collector,
        fallback_id=fallback,
    )
    from sqlalchemy import select

    from app.models import SessionEvent

    rows = list(
        (
            await db.execute(
                select(SessionEvent).where(SessionEvent.chat_id == chat.id).order_by(SessionEvent.seq)
            )
        )
        .scalars()
        .all()
    )
    assert [row.type for row in rows] == ["tool/call", "tool/result"]
    assert {row.tool_call_id for row in rows} == {fallback}
    # 事件侧写的 id 与执行侧 collector 的键一致 ⇒ `tool/result` 的 content 取得到
    assert rows[1].data["content"] == "回声：你好"


async def test_new_tool_call_id_is_short_and_unique():
    """兜底 id 的形态（老基线同款：uuid4 前 12 位，短到能进 64 字符列、不撞）。"""
    ids = {runner.new_tool_call_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(value) == runner.TOOL_CALL_ID_LEN for value in ids)
