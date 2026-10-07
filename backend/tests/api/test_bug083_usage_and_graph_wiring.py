"""`回归修复` 剩余面批次判据载体：**用量链（S6–S9）与图谱链（S1）的吞点接线**。

🔴 口径（逐字，防误读）

· 钉的是「**失败出口存在且真的会动**」，**不是**「派生失败已被消除」；
· 可见面 = `GET /api/agent/audit` 的 **`counters.derived`**，**不是日志**
  （冻结态应用层日志**不进控制台** = `回归修复`）；
· 该出口**前端零消费**（`grep -rn counters frontend/src` = 0 命中）⇒ 正确措辞 =
  「**机器可读 + 人工 curl 的可见面**」；⛔ 不得写「用户已能看见派生失败」。

背景（吞点地图 = `回归检查-吞点地图-20260926.md`）
----
| 地图编号 | 落点 | 本笔处置 |
|---|---|---|
| S6–S9 | `app/usage/recorder.py` 四个吞点（`_insert_event` / 无事件循环 / 会话不可用 / SAVEPOINT） | 接新键 **`usage_events`**（四点同族一链） |
| S1 | `app/pipeline/knowledge_graph.py` 4 处裸 `continue` | 接新键 **`graph_view_edges`**；⚠️ 只接 **3 处**——「目标在回收站」那处是**条目 25 的刻意语义**（软删内容不进图），不是失败，**豁免不计数** |
| #4 | `app/pipeline/wikilink.py::remove_links_for_deleted` 清理边失败 | 补接线（复用 `note_links` 键）+ 本文件补腿 |

🔴 **范围裁决（如实登记）**：地图「零判据载体」#12（`session/memory.py:491-493` 记忆落库）
**本笔不接线**——memory 家族计数形状被 `test_agent_audit_5_5.py:519` 的**精确键集断言**钉死
（5.5 收口物），扩它要动 5.5 冻结面 ⇒ 留待后续批次单独处置，本笔不越界。

腿表
----
| 用例 | 钉什么 |
|---|---|
| `test_zero_state_has_all_five_derived_keys` | 零态键集 = `COUNTER_KEYS` 全集（函数名保留历史；`回归修复` 后八键 ⇒ 2026-09-28 批次三后 = **十键**，只增不改义的形状面） |
| `test_s6_insert_event_failure_bumps` | `_insert_event` 落库炸 ⇒ `usage_events` +1 |
| `test_s7_no_loop_broken_session_factory_bumps` | 无事件循环 + 会话工厂炸 ⇒ +1 |
| `test_s8_session_unavailable_bumps` | 有事件循环但会话建不出 ⇒ +1 |
| `test_s9_savepoint_failure_bumps` | 回答收口 SAVEPOINT 写入炸 ⇒ +1 |
| `test_usage_success_path_does_not_bump` | **负腿**：成功路径计数不动（排掉「调用即计数」） |
| `test_s1_missing_source_and_untitleable_target_bump` | 源缺失 + 目标无标题 ⇒ `graph_view_edges` +2；回收站目标**不**计数 |
| `test_leg4_cleanup_failure_bumps_note_links` | 双链边清理失败 ⇒ `note_links` +1（复用键） |
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.core import derived_counters
from app.pipeline import knowledge_graph
from app.pipeline import wikilink
from app.usage import recorder


@pytest.fixture(autouse=True)
def _reset_process_counters():
    """进程全局计数**跨用例泄漏**是本仓踩过的坑 ⇒ 每例前后清零。"""
    derived_counters.reset_failure_counts()
    yield
    derived_counters.reset_failure_counts()


# --------------------------------------------------------------------------
# 零态
# --------------------------------------------------------------------------


def test_zero_state_has_all_five_derived_keys():
    """零态：`ordered_counts()` 键集 = `COUNTER_KEYS` 全集（`回归修复` 后 = 八键；2026-09-28 批次三后 = **十键**）且全 0。"""
    counts = derived_counters.ordered_counts()
    assert set(counts) == set(derived_counters.COUNTER_KEYS)
    assert counts == dict.fromkeys(derived_counters.COUNTER_KEYS, 0)


# --------------------------------------------------------------------------
# S6–S9：usage/recorder.py 四个吞点 → usage_events
# --------------------------------------------------------------------------


class _CommitExplodesSession:
    """假会话：`async with` 能进、`commit` 炸（= `_insert_event` 的落库那一步）。"""

    def __init__(self) -> None:
        self.added: list[object] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj) -> None:  # noqa: ANN001
        self.added.append(obj)

    async def commit(self) -> None:
        raise RuntimeError("模拟用量事件落库失败：commit 炸了")


@pytest.mark.asyncio
async def test_s6_insert_event_failure_bumps():
    """S6：`_insert_event` 内部落库失败 ⇒ `usage_events` +1（原本只有一条 warning）。"""
    session = _CommitExplodesSession()

    await recorder._insert_event(session, "chat", "faux", "faux-model", None, None, None, 1, None)

    assert derived_counters.failure_counts().get("usage_events", 0) >= 1, (
        "用量事件落库失败被吞但计数没动 ⇒ 接线失效",
        derived_counters.failure_counts(),
    )


def test_s7_no_loop_broken_session_factory_bumps(monkeypatch):
    """S7：无事件循环（纯同步上下文）+ 会话工厂炸 ⇒ `usage_events` +1，且不抛。"""

    def _boom():
        raise RuntimeError("模拟 SessionLocal 建不出来")

    monkeypatch.setattr(recorder, "SessionLocal", _boom)

    recorder.record_usage("chat", provider="faux", model="faux-model")  # 同步测试 ⇒ 无运行中事件循环

    assert derived_counters.failure_counts().get("usage_events", 0) >= 1


@pytest.mark.asyncio
async def test_s8_session_unavailable_bumps(monkeypatch):
    """S8：有事件循环但 `_open_session` 建不出会话 ⇒ `usage_events` +1，且不抛。

    ⚠️ `record_usage` 本身是**同步**函数（fire-and-forget 语义）——本腿用异步用例
    只为制造「运行中事件循环」的前提，调用处**不 await**。
    """

    def _boom():
        raise RuntimeError("模拟会话不可用")

    monkeypatch.setattr(recorder, "SessionLocal", _boom)

    recorder.record_usage("chat", provider="faux", model="faux-model")

    assert derived_counters.failure_counts().get("usage_events", 0) >= 1


class _SavepointExplodesDB:
    """假 db：`begin_nested()` 给出一个进入即炸的 SAVEPOINT（= 回答收口路径）。"""

    @asynccontextmanager
    async def begin_nested(self):
        raise RuntimeError("模拟 SAVEPOINT 写入失败")
        yield  # pragma: no cover —— 上面已抛，仅为语法占位

    def add(self, _obj) -> None:  # noqa: ANN001
        raise AssertionError("SAVEPOINT 已炸，不该走到 add")


@pytest.mark.asyncio
async def test_s9_savepoint_failure_bumps():
    """S9：`record_usage_on`（回答收口）SAVEPOINT 失败 ⇒ `usage_events` +1，且不抛。"""

    await recorder.record_usage_on(_SavepointExplodesDB(), "chat", provider="faux", model="faux-model")

    assert derived_counters.failure_counts().get("usage_events", 0) >= 1


class _SavepointOkDB:
    """假 db：SAVEPOINT 正常（成功路径负腿用）。"""

    def __init__(self) -> None:
        self.added: list[object] = []

    @asynccontextmanager
    async def begin_nested(self):
        yield self

    def add(self, obj) -> None:  # noqa: ANN001
        self.added.append(obj)


@pytest.mark.asyncio
async def test_usage_success_path_does_not_bump():
    """负腿：写入成功 ⇒ 计数**不动**（排掉「调用即计数」的假接线）。"""
    db = _SavepointOkDB()

    await recorder.record_usage_on(db, "chat", provider="faux", model="faux-model")

    assert db.added, "成功腿自证：行确实进了会话"
    assert derived_counters.failure_counts() == {}, (
        "成功路径不该产生任何派生失败计数",
        derived_counters.failure_counts(),
    )


# --------------------------------------------------------------------------
# S1：knowledge_graph.py 的边丢弃 → graph_view_edges（3 处接线 + 1 处豁免）
# --------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeGraphDB:
    """假 db：`build_graph` 的 5 个查询按**调用序**回放（notes / docs / 软删笔记 / 软删文档 / 边）。

    ⚠️ 按调用序回放是刻意的最小桩：查询一旦重排本桩会自证失败（各页行类型对不上），
    这比「按语句类型分发」更能把实现顺序钉住。
    """

    def __init__(self, pages: list[list]) -> None:
        self._pages = pages
        self.calls = 0

    async def execute(self, _stmt):
        rows = self._pages[self.calls] if self.calls < len(self._pages) else []
        self.calls += 1
        return _FakeResult(rows)


def _note(ident: int) -> SimpleNamespace:
    return SimpleNamespace(id=ident, title=f"笔记{ident}", category="", tags=[], kb_id=1, source_document_id=None)


def _link(**kw) -> SimpleNamespace:
    base = dict(
        source_type="note",
        source_id=1,
        target_note_id=None,
        target_document_id=None,
        target_title="",
        kb_id=1,
        resolved=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_s1_missing_source_and_untitleable_target_bump():
    """S1：源缺失的边 + 目标无标题的边 ⇒ `graph_view_edges` +2；回收站目标的边**豁免**不计数。"""
    db = _FakeGraphDB(
        [
            [_note(1)],            # 可见笔记
            [],                    # 可见文档
            [(77, 1, "回收站笔记")],  # 软删笔记 id、库、标题
            [],                    # 软删文档 id
            [
                _link(source_id=999, target_title="幽灵标题"),     # ① 源笔记不可见 ⇒ 计数
                _link(target_note_id=77, resolved=True),           # ② 目标在回收站 ⇒ 刻意丢弃，不计数
                _link(target_title="   "),                         # ③ 目标解析不了又无标题 ⇒ 计数
            ],
        ]
    )

    graph = await knowledge_graph.build_graph(db)

    counts = derived_counters.failure_counts()
    assert counts.get("graph_view_edges", 0) == 2, (
        f"边丢弃计数应恰为 2（源缺失 + 无标题；回收站豁免），实得：{counts}"
    )
    assert graph["edges"] == [], "三条边都应被丢弃（两条计数 + 一条刻意语义）"
    assert graph["stats"]["ghosts"] == 0, "幽灵标题的边因源缺失先被丢弃 ⇒ 不应产生幽灵节点"


# --------------------------------------------------------------------------
# #4：wikilink.remove_links_for_deleted 清理失败 → note_links（复用键）
# --------------------------------------------------------------------------


class _ExecuteExplodesSession:
    async def execute(self, _stmt):
        raise RuntimeError("模拟清理链接边的 DB 失败")

    async def rollback(self):
        return None


@pytest.mark.asyncio
async def test_leg4_cleanup_failure_bumps_note_links():
    """#4：删除实体后清理其链接边失败 ⇒ `note_links` +1（与维护路径同键、原本零出口）。"""

    await wikilink.remove_links_for_deleted(_ExecuteExplodesSession(), "note", 42)

    assert derived_counters.failure_counts().get("note_links", 0) >= 1, (
        "清理失败被吞但计数没动 ⇒ 接线失效",
        derived_counters.failure_counts(),
    )
