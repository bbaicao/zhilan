"""条目 1.2.3 决策日志单测（I9：每次问答可追溯路由决策与 source_type）。

落点是 ``session_events``（阶段 0 的 0002 已建表，零新表）。本文件验证：

1. 三种来源（regex / llm / fallback）+ off 都能落库并被**查询**出来；
2. 没有会话上下文时只记录不落库，且**不抛异常**（路由是纯函数，不该被会话绑死）；
3. 写库失败被隔离（I5 绝不炸穿），不拖垮问答主流程；
4. ``session_events`` 的既有不变量不被破坏：``(chat_id, seq)`` 唯一、seq 连续。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import SessionEvent
from app.routing import decisions as decisions_mod
from app.routing import llm as llm_mod
from app.routing import router as router_mod
from app.routing.decision import RouteDecision
from app.session import events as event_log
from app.session import repository as repo
from tests.routing.test_router import StubClassifier


@pytest.fixture
def event_log_on(monkeypatch):
    """确保事件日志开关为开（默认即开，显式固定避免受 .env 影响）。"""
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "CHAT_EVENT_LOG_ENABLE", True)


@pytest.mark.asyncio
async def test_record_and_query_regex_decision(db, event_log_on):
    """I9 主线：正则命中 → 落库 → 可查询出 source/source_type/matched_rule。"""
    chat = await repo.create_chat(db, title="路由留痕")

    decision = await router_mod.route(
        "知识库里有没有项目周报", mode="regex", classifier=StubClassifier("external")
    )
    seq = await decisions_mod.record_decision(db, chat.id, decision)
    await db.commit()

    assert seq == 0  # 该会话第一条事件，seq 从 0 起

    loaded = await decisions_mod.load_decisions(db, chat.id)
    assert loaded == [decision]
    assert loaded[0].source == "regex"
    assert loaded[0].source_type == "internal_db"
    assert loaded[0].matched_rule == "kb_named"


@pytest.mark.asyncio
async def test_query_all_three_sources(db, event_log_on):
    """I9：regex / llm / fallback 三种来源都能被追溯出来。"""
    chat = await repo.create_chat(db, title="三来源")

    regex_decision = await router_mod.route(
        "文档里有没有预算", mode="regex", classifier=StubClassifier("external")
    )
    llm_decision = await router_mod.route(
        "帮我写首诗", mode="regex", classifier=StubClassifier("internal_db")
    )
    fallback_decision = await router_mod.route(
        "随便说说", mode="regex", classifier=StubClassifier(exc=RuntimeError("挂了")), fallback="direct"
    )

    for item in (regex_decision, llm_decision, fallback_decision):
        await decisions_mod.record_decision(db, chat.id, item)
    await db.commit()

    loaded = await decisions_mod.load_decisions(db, chat.id)
    assert [d.source for d in loaded] == ["regex", "llm", "fallback"]
    assert [d.source_type for d in loaded] == ["internal_db", "internal_db", "external"]
    assert await decisions_mod.load_latest_decision(db, chat.id) == fallback_decision


@pytest.mark.asyncio
async def test_off_mode_is_recorded_too(db, event_log_on):
    """off 模式也要留痕（I9 要求「默认」路径同样可追溯）。"""
    chat = await repo.create_chat(db, title="off")
    decision = await router_mod.route("知识库里有什么", mode="off")

    await decisions_mod.record_decision(db, chat.id, decision)
    await db.commit()

    loaded = await decisions_mod.load_decisions(db, chat.id)
    assert loaded[0].source == "off"
    assert loaded[0].source_type == "external"


@pytest.mark.asyncio
async def test_no_chat_context_skips_persist_without_raising(db, event_log_on):
    """没有会话上下文 → 跳过落库、不抛异常（路由不被会话绑死）。"""
    decision = RouteDecision(source_type="external", source="off", reason="x")

    assert await decisions_mod.record_decision(db, None, decision) is None
    assert await decisions_mod.record_decision(None, 123, decision) is None

    rows = (await db.execute(select(SessionEvent))).scalars().all()
    assert list(rows) == []


@pytest.mark.asyncio
async def test_event_write_failure_is_isolated(db, event_log_on, monkeypatch):
    """I5 绝不炸穿：写事件失败只计数 + warning，不向调用方抛异常。"""
    chat = await repo.create_chat(db, title="失败隔离")
    await db.commit()

    async def _boom(*args, **kwargs):
        raise RuntimeError("事件表写不进去")

    monkeypatch.setattr(event_log, "_next_seq", _boom)
    before = event_log.event_write_failures()

    decision = RouteDecision(source_type="external", source="fallback", reason="x")
    result = await decisions_mod.record_decision(db, chat.id, decision)

    assert result is None
    assert event_log.event_write_failures() == before + 1


@pytest.mark.asyncio
async def test_seq_is_contiguous_and_unique(db, event_log_on):
    """session_events 既有不变量：seq 连续、不断号、不重复。"""
    chat = await repo.create_chat(db, title="seq")
    for index in range(3):
        await event_log.append_event(
            db, chat.id, "routing/decided", {"n": index, "source_type": "external", "source": "regex"}
        )
    await db.commit()

    rows = await event_log.list_events(db, chat.id)
    assert [row.seq for row in rows] == [0, 1, 2]


@pytest.mark.asyncio
async def test_unknown_event_type_rejected(db, event_log_on):
    """事件类型白名单仍然生效（词汇表外的类型不写库）。"""
    chat = await repo.create_chat(db, title="白名单")

    with pytest.raises(event_log.UnknownEventTypeError):
        await event_log.append_event(db, chat.id, "routing/whatever", {})


@pytest.mark.asyncio
async def test_surface_event_requires_surface_op(db, event_log_on):
    """表层事件必须带 surface_op；仅日志事件禁止带（沿用老仓库口径）。"""
    chat = await repo.create_chat(db, title="surface")

    with pytest.raises(event_log.SurfaceOpError):
        await event_log.append_event(db, chat.id, "user/message", {"content": "x"})

    with pytest.raises(event_log.SurfaceOpError):
        await event_log.append_event(
            db, chat.id, "routing/decided", {"source_type": "external", "source": "regex"},
            surface_op="append",
        )


@pytest.mark.asyncio
async def test_lossless_json_enforced(db, event_log_on):
    """载荷必须无损 JSON（NaN / 超安全整数一律拒绝）。"""
    chat = await repo.create_chat(db, title="无损")

    with pytest.raises(event_log.EventError):
        await event_log.append_event(
            db, chat.id, "routing/decided", {"source_type": "external", "source": "regex", "n": float("nan")}
        )
    with pytest.raises(event_log.EventError):
        await event_log.append_event(
            db, chat.id, "routing/decided", {"source_type": "external", "source": "regex", "n": 2**60}
        )


@pytest.mark.asyncio
async def test_event_log_switch_off_writes_nothing(db, monkeypatch):
    """开关关闭 → 不写库、返回 None（逐字回到「没有事件日志」的行为）。"""
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "CHAT_EVENT_LOG_ENABLE", False)
    chat = await repo.create_chat(db, title="开关关")

    decision = RouteDecision(source_type="external", source="off", reason="x")
    assert await decisions_mod.record_decision(db, chat.id, decision) is None
    await db.commit()

    rows = (await db.execute(select(SessionEvent))).scalars().all()
    assert list(rows) == []


@pytest.mark.asyncio
async def test_routing_decided_is_log_only_event():
    """routing/decided 属仅日志事件：不产生模型消息（不进历史投影）。"""
    assert event_log.ROUTING_DECIDED in event_log.LOG_ONLY_EVENT_TYPES
    assert not event_log.is_surface_event(event_log.ROUTING_DECIDED)


def test_router_module_does_not_import_upward_layers():
    """分包边界：routing 不 import api / retrieval（依赖方向只向下）。"""
    import inspect

    import app.routing.router as router_module

    source = inspect.getsource(router_module)
    assert "app.api" not in source
    assert "app.retrieval" not in source
    assert "fastapi" not in source


# --------------------------------------------------------------------------
# 回归修复 / 020：撞 `uk(chat_id, request_id)` 要**报准原因、不白重试**，
# 且「事件被静默吞掉」这件事本身要有机械红灯。
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_id_conflict_is_reported_as_such_without_retry(db, event_log_on):
    """同一 `(chat_id, request_id)` 的第二条事件 → 专用异常，**不重试**、原因准确。

    `回归修复`：原实现会白重试一次（换 `seq` 解决不了 `request_id` 重复），
    再抛「同一 `(chat_id, seq)` 连续两次冲突」——把原因归错，排障方向被带偏。
    """
    chat = await repo.create_chat(db, title="request_id 冲突")
    await event_log.append_event(
        db, chat.id, "routing/decided", {"source_type": "external", "source": "regex"}, request_id="dup-1"
    )
    await db.commit()
    before = event_log.event_write_failures()

    with pytest.raises(event_log.RequestIdConflict) as excinfo:
        await event_log.append_event(
            db, chat.id, "retrieval/start", {"question": "q"}, request_id="dup-1"
        )

    message = str(excinfo.value)
    assert "request_id" in message
    assert "trace" in message, "错误文案要给出正确出路（用 trace 列串链）"
    assert "seq" not in message, "不许再把原因归到 (chat_id, seq)"
    assert event_log.event_write_failures() == before, "append_event 直抛，不经过隔离计数"

    rows = await event_log.list_events(db, chat.id)
    assert [row.type for row in rows] == ["routing/decided"], "撞约束的那条确实没落库"


@pytest.mark.asyncio
async def test_safe_append_event_isolates_request_id_conflict(db, event_log_on):
    """同一冲突走 `safe_append_event` → 返回 `None` + 计数 +1（I5 不炸穿），事件不落库。"""
    chat = await repo.create_chat(db, title="request_id 隔离")
    await event_log.safe_append_event(
        db, chat.id, "routing/decided", {"source_type": "external", "source": "regex"}, request_id="dup-2"
    )
    before = event_log.event_write_failures()

    result = await event_log.safe_append_event(
        db, chat.id, "retrieval/start", {"question": "q"}, request_id="dup-2"
    )

    assert result is None
    assert event_log.event_write_failures() == before + 1
    rows = await event_log.list_events(db, chat.id)
    assert [row.type for row in rows] == ["routing/decided"]


@pytest.mark.asyncio
async def test_seq_conflict_still_retries_and_message_says_seq(db, event_log_on, monkeypatch):
    """真正的 `(chat_id, seq)` 冲突（并发写）**仍有界重试 3 次**，且文案说的是 `seq`。

    `回归修复` 起本条走的是台账的**回落支路**（显式让 `_seq_ledger_of` 取不到引擎 ⇒
    分配值 = 纯库内读数 `MAX(seq)+1`，与改前逐字同值）：进程内的预留台账恰恰把
    「同会话并发轮抢同一个 seq」这条病态关掉了，要考「纯库内读数下冲突仍重试、且原因说的是
    `seq` 而不是 `request_id`」就必须显式回落 —— 判别力一字未减，只是把闸门放回改前那一档。
    """
    chat = await repo.create_chat(db, title="seq 冲突")
    await event_log.append_event(
        db, chat.id, "routing/decided", {"source_type": "external", "source": "regex"}
    )
    await db.commit()

    async def _always_same_seq(_db, _chat_id):  # noqa: ANN001 —— 模拟并发写抢到同一个 seq
        return 0

    monkeypatch.setattr(event_log, "_next_seq", _always_same_seq)
    # 台账回落支路：取不到引擎 ⇒ 分配值 = 纯库内读数（改前行为）。理由见 docstring。
    monkeypatch.setattr(event_log, "_seq_ledger_of", lambda _db: None)

    with pytest.raises(event_log.EventError) as excinfo:
        await event_log.append_event(db, chat.id, "retrieval/start", {"question": "q"})

    message = str(excinfo.value)
    assert "seq" in message
    assert "request_id" not in message, "seq 冲突不该被说成 request_id 冲突"


@pytest.mark.asyncio
async def test_every_known_event_type_persists_without_being_swallowed(db, event_log_on):
    """**表驱动红灯**（`回归修复` 的零风险那一步）：词汇表里每一种事件都真能落库、且不计入失败计数。

    为什么值钱：`safe_append_event` 的失败隔离是硬要求（I5），所以**任何**写事件失败都只会留一条
    warning + 计数，`None` 返回值还与「日志开关关掉」二义。阶段 4 新增 `tool/call` / `step/*` 等事件时，
    若类型拼错、忘带 `surface_op`、或载荷里混进不可序列化对象，事件会被**静默吞掉**——本用例会直接红，
    不需要等到「回放时发现事件没写」。
    """
    chat = await repo.create_chat(db, title="词表")
    before = event_log.event_write_failures()

    for event_type in sorted(event_log.KNOWN_EVENT_TYPES):
        surface_op = "append" if event_log.is_surface_event(event_type) else None
        seq = await event_log.safe_append_event(
            db, chat.id, event_type, {}, surface_op=surface_op
        )
        assert seq is not None, f"{event_type} 没写进去（被静默吞掉或类型不认）"

    await db.commit()

    assert event_log.event_write_failures() == before, "有事件写失败（被隔离吞掉了）"
    rows = await event_log.list_events(db, chat.id)
    assert {row.type for row in rows} == set(event_log.KNOWN_EVENT_TYPES)
