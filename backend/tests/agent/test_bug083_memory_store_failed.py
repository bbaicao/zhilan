"""`回归修复`（#12）判据载体：**记忆落库失败**接进 `counters.memory.store_failed`。

🔴 口径（逐字，防误读）

· 钉的是「**失败出口存在且真的会动**」，**不是**「落库失败已被消除」（fire-and-forget 的
  隔离语义照旧：失败只 warning + 返回 0，绝不影响本轮回答）；
· 2026-09-27 第二批（业主拍板⑩）：`test_agent_audit_5_5.py` 的 A9 **精确键集**断言已按
  5.5 自己「只增不减」纪律升级为**超集断言**（A8 先例）⇒ 本批才得以给 memory 家族增第五键
  `store_failed`——落库失败与「零写入」在计数上从此可区分；
· `COUNTER_KEYS["memory"]`（`app/agent/audit.py`）同步只增第五键；键序/键义不变。
"""

from __future__ import annotations

import pytest

from app.agent import audit as agent_audit
from app.session import memory


@pytest.fixture(autouse=True)
def _reset_memory_counters():
    """memory 计数是进程全局 ⇒ 每例前后清零（既有夹具同口径）。"""
    memory.reset_failure_counts()
    yield
    memory.reset_failure_counts()


class _CommitExplodesSession:
    """假会话：`async with` 能进、`commit` 必抛（= `_store_facts` 的落库那一步）。"""

    commit_called = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self) -> None:
        self.commit_called = True
        raise RuntimeError("模拟记忆落库失败：commit 炸了")


class _OkSession:
    """假会话：commit 正常（成功路径对照）。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self) -> None:
        return None


def _stub_session_factory(monkeypatch: pytest.MonkeyPatch, session) -> None:
    monkeypatch.setattr(memory, "SessionLocal", lambda: session)
    monkeypatch.setattr(
        memory, "new_independent_session", lambda _bound: session
    )


def _stub_remember(monkeypatch: pytest.MonkeyPatch, new_id=1) -> list[dict]:
    """把 `remember` 换成桩（不连库），并记录调用。"""
    calls: list[dict] = []

    async def _fake_remember(
        _session, *, text, kind, source_chat_id=None, source_message_id=None,
        scope=memory.SCOPE_GLOBAL, fact_key=None, supersedes_id=None,
        valid_from=None, valid_until=None, source_text=None,
    ):
        calls.append({
            "text": text, "kind": kind, "source_chat_id": source_chat_id,
            "source_message_id": source_message_id, "scope": scope,
            "fact_key": fact_key, "supersedes_id": supersedes_id,
            "valid_from": valid_from, "valid_until": valid_until, "source_text": source_text,
        })
        return new_id

    monkeypatch.setattr(memory, "remember", _fake_remember)
    return calls


# --------------------------------------------------------------------------
# 主腿：落库失败 ⇒ store_failed +1，返回 0
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_failure_bumps_store_failed_and_returns_zero(monkeypatch):
    """#12 主腿：commit 必抛 ⇒ `store_failed` +1、返回 0、绝不抛（隔离语义不变）。"""
    session = _CommitExplodesSession()
    _stub_session_factory(monkeypatch, session)
    calls = _stub_remember(monkeypatch)

    stored = await memory._store_facts(
        [{"text": "事实一", "kind": "fact"}], chat_id=1, message_id=2, bound_to=None
    )

    assert stored == 0
    assert len(calls) == 1 and session.commit_called, "必须到达 commit 失败，不能由替身参数错误蒙过"
    assert memory.failure_counts()["store_failed"] >= 1, (
        f"落库失败被吞但计数没动 ⇒ 接线失效：{memory.failure_counts()}"
    )
    assert memory.failure_counts()["call_failed"] == 0, "落库失败不该记到抽取头上"


# --------------------------------------------------------------------------
# 对照与分界
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_success_does_not_bump_store_failed(monkeypatch):
    """成功路径负腿：commit 正常 ⇒ `store_failed` 恒 0、stored 计数照加。"""
    _stub_session_factory(monkeypatch, _OkSession())
    calls = _stub_remember(monkeypatch, new_id=7)

    stored = await memory._store_facts(
        [{"text": "事实一", "kind": "fact"}], chat_id=1, message_id=2, bound_to=None
    )

    assert stored == 1
    assert memory.failure_counts()["store_failed"] == 0
    assert calls == [{
        "text": "事实一", "kind": "fact", "source_chat_id": 1, "source_message_id": 2,
        "scope": "chat:1", "fact_key": None, "supersedes_id": None,
        "valid_from": None, "valid_until": None, "source_text": "",
    }]
    # 注意：`stored` 家族计数在 `remember_turn`（调用方）里加，`_store_facts` 直调不经过它。


@pytest.mark.asyncio
async def test_extract_failure_lands_on_call_failed_not_store_failed(monkeypatch):
    """分界腿：抽取阶段（模型调用）失败记 `call_failed`，**不**碰 `store_failed`。"""
    monkeypatch.setattr(memory, "_enabled", lambda: True)

    class _BoomModel:
        pass

    async def _boom_call_model(_model, _prompt):
        raise RuntimeError("模拟抽取模型调用失败")

    monkeypatch.setattr(memory, "_call_model", _boom_call_model)

    facts = await memory._extract_facts(_BoomModel(), "问", "答")

    assert facts == []
    assert memory.failure_counts()["call_failed"] >= 1
    assert memory.failure_counts()["store_failed"] == 0, "抽取失败不得记到落库头上"


# --------------------------------------------------------------------------
# 可见面：counters.memory 进 GET /api/agent/audit
# --------------------------------------------------------------------------


def test_counters_memory_shape_contains_store_failed():
    """`safe_counters()` 的 memory 子对象含第五键 `store_failed`（零态为 0）。"""
    counters = agent_audit.safe_counters()
    assert "store_failed" in counters["memory"]
    assert counters["memory"]["store_failed"] == 0
    assert {"call_failed", "parse_failed", "empty", "stored"} <= set(counters["memory"])
    assert "store_failed" in agent_audit.COUNTER_KEYS["memory"], "audit 键表必须同步只增第五键"
