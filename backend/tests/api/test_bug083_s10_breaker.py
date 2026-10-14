"""`回归修复` · **S10** 判据载体：压缩摘要**落库失败**必须可计数、且进可见面。

🔴 口径（逐字，防误读）

· 钉的是「**失败出口存在且真的会动**」，**不是**「派生失败已被消除」；
· 可见面 = `GET /api/agent/audit` 的 **`counters.derived`**，**不是日志**
  （冻结态应用层日志**不进控制台** = `回归修复`）；
· 该出口**前端零消费**（`grep -rn counters frontend/src` = 0 命中）⇒ 正确措辞 =
  「**机器可读 + 人工 curl 的可见面**」；⛔ 不得写「用户已能看见派生失败」。

背景
----
摘要落库失败（`app/session/compaction.py`）存在可观察性风险：压缩断路器 `_BREAKER`
里有一个**按 `chat_id` 分桶的连续失败数**（不进 `counters`），唯一出口
`CompactionResult.warnings` 又在 `app/session/service.py::build_model_history` 里被一句
`logger.warning` 吃掉 ⇒ 用户唯一的感知是「发给模型的历史变短了」。

本文件钉 **S10 三件**（= 本仓判据三件套）+ **三条「零判据载体」补腿**（同族：吞点无载体）：

| 用例 | 钉什么 |
|---|---|
| `test_s10_shape_zero_state_is_stable` | ① **零态**：`counters.derived` 键齐（含 `compaction_summary`）且全 0 —— **不是缺席** |
| `test_s10_store_failure_bumps_but_success_does_not` | ② **真会动**、且**只在失败时动**（成功路径计数不动 ⇒ 排掉「调用即计数」） |
| `test_s10_store_failure_reaches_the_audit_endpoint` | ③ **进可见面**：真走一次落库失败 ⇒ 端点上读得到**同一个数** |
| `test_leg_memory_*` | 补腿（地图「零判据载体」#10 / #11）：`memory._extract_facts` 的两级失败计数此前**零载体** |
| `test_leg_clean_status_*` | 补腿（地图「零判据载体」S14）：磁盘读失败的 **fail-closed** 语义此前**零载体** |

⚠️ ② 为什么用**假 db / 假 chat** 而不是打 HTTP（照 `tests/session/test_compaction.py::_CommitFailsDB`
的写法）：走 `POST /api/chat` 触发压缩要真造长会话 + 真模型，而本腿要测的是**落库那一步的吞点**；
直接驱动 `compaction.compact_history` 更贴近被测点，且**不连库**（只有 ③ 那一腿要打端点）。

⚠️ 本文件**不改实现**；失败只写进交付报告。
"""

from __future__ import annotations

from types import SimpleNamespace
from contextlib import asynccontextmanager

import pytest

from app.core import derived_counters
from tests.api.test_chat_contract import client  # noqa: F401 —— ③ 那一腿要用 ASGI 直连客户端

#: 假摘要正文（不含标点漂移风险）
SUMMARY_REPLY = "用户在做压缩落库失败的可见性验收；摘要只落 chats 两列。"

#: ① 零态与键集断言里逐字写死的键（**故意不从实现里取**：键少了/多了都要红）
#: 🔴 2026-09-27 批次适配：`083` 剩余面接线新增 `usage_events`（S6–S9）与
#: `graph_view_edges`（S1）两键 ⇒ 键集随之扩容（`COUNTER_KEYS` 只增不改义）。
#: 🔴 2026-09-27 第二批适配：`083` 第二子集接线新增 `pdf_ocr_fallback`（S11–S13）与
#: `web_fetch_degrade`（S18–S19）两键 ⇒ 五键 → 七键（同上「只增不改义」纪律）。
#: 🔴 2026-09-27 缺陷清单-2 阶段二适配：`回归修复` 修复新增 `stale_vector_points`
#: （旧 Qdrant 点删除失败 ⇒ 孤儿点挤占向量检索）⇒ 七键 → **八键**。
#: 🔴 2026-09-28 批次三（`083` 收口）适配：新增 `trace_persist`（本轮尾部溯源/审计落库失败）
#: 与 `tool_result_channel`（工具结果通道降级）两键 ⇒ 八键 → **十键**（仍「只增不改义」）。
#: ⚠️ **本锚是「独立锚」不是「抄实现」**：它是**逐字写死**的键集，实现里多/少一个键都必须在此处
#: 显式跟随（照前三次扩容先例）；改实现却漏改这里 ⇒ 本腿红，正是它的判别力所在。
EXPECTED_DERIVED_KEYS = {
    "note_links",
    "doc_links",
    "compaction_summary",
    "usage_events",
    "graph_view_edges",
    "pdf_ocr_fallback",
    "web_fetch_degrade",
    "stale_vector_points",
    "trace_persist",
    "tool_result_channel",
    # 🔴 2026-10-08 专项「导入资料自动连线」：第十一键（只增不改义，尾部追加）。
    "similar_links",
}


@pytest.fixture(autouse=True)
def _reset_process_counters():
    """进程全局计数**跨用例泄漏**是本仓踩过的坑 ⇒ 每例前后清零（含 memory 计数家族）。"""
    from app.session import memory

    derived_counters.reset_failure_counts()
    memory.reset_failure_counts()
    yield
    derived_counters.reset_failure_counts()
    memory.reset_failure_counts()


# --------------------------------------------------------------------------
# S10 的脚手架：最小行 / 最小摘要模型 / 两个假 db
# --------------------------------------------------------------------------


class _Row:
    """最小消息行：`compact_history` 只读 `role` / `content` / `id` 三个属性。"""

    def __init__(self, ident: int, role: str, content: str) -> None:
        self.id = ident
        self.role = role
        self.content = content


def _rows(rounds: int = 3) -> list[_Row]:
    """造 `rounds` 轮问答（user + assistant，id 升序），正文足够长以越过阈值。"""
    out: list[_Row] = []
    for index in range(rounds):
        out.append(_Row(10 + index * 2, "user", "第{}轮提问：".format(index + 1) + "问" * 60))
        out.append(_Row(11 + index * 2, "assistant", "第{}轮回答：".format(index + 1) + "答" * 60))
    return out


class _FakeSummaryModel:
    """最小摘要模型（照 `FakeCompactionModel` 的接线面写）：回一段文本，`error` 非空则抛。"""

    def __init__(self, text: str = SUMMARY_REPLY, *, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls = 0
        self.spec = SimpleNamespace(id="faux-s10")

    async def generate(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        self.calls += 1
        if self.error is not None:
            raise self.error
        block = SimpleNamespace(type="text", text=self.text)
        return SimpleNamespace(content=[block], stop_reason="stop")


class _CommitFailsDB:
    """SAVEPOINT flush fails; event writes are independently isolated."""

    def __init__(self, message: str = "模拟摘要落库失败：flush 炸了") -> None:
        self.message = message
        self.added: list[object] = []
        self.commits = 0

    def add(self, obj) -> None:  # noqa: ANN001
        self.added.append(obj)

    @asynccontextmanager
    async def begin_nested(self):
        yield

    async def flush(self) -> None:
        self.commits += 1
        raise RuntimeError(self.message)

    async def rollback(self) -> None:
        raise RuntimeError("模拟回滚也炸了（还原失败也不该炸穿）")

    async def execute(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("模拟事件写入也炸了（safe_append_event 应隔离）")


class _CommitOkDB:
    """SAVEPOINT flush succeeds; event writes are independently isolated."""

    def __init__(self) -> None:
        self.commits = 0

    def add(self, _obj) -> None:  # noqa: ANN001
        return None

    @asynccontextmanager
    async def begin_nested(self):
        yield

    async def flush(self) -> None:
        self.commits += 1

    async def execute(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("模拟事件写入也炸了（与本腿无关）")


def _fake_chat(chat_id: int):
    """最小 chat：`compact_history` 只读/写 `id` / `context_summary` / `compressed_up_to`。"""
    from app.models import Chat

    return Chat(id=chat_id, context_summary=None, compressed_up_to=None)


def _tighten(monkeypatch) -> None:  # noqa: ANN001
    """把压缩阈值收紧到「3 轮问答必然超阈」（`settings` 是进程单例，`monkeypatch` 会还原）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "CHAT_COMPRESS_ENABLE", True)
    monkeypatch.setattr(settings, "CHAT_COMPRESS_THRESHOLD", 100)
    monkeypatch.setattr(settings, "CHAT_COMPRESS_KEEP", 2)


# --------------------------------------------------------------------------
# ① 零态形状稳定
# --------------------------------------------------------------------------


def test_s10_shape_zero_state_is_stable():
    """① **零态**：没失败时 `counters.derived` 在、键齐（含 S10 的键）、全 0。

    「键齐」= 与 `derived_counters.COUNTER_KEYS` **同集**（键少了/多了都红）；
    另用逐字写死的 `EXPECTED_DERIVED_KEYS` 做一次独立锚（防止「两边一起改」自证）。
    """
    from app.agent.audit import safe_counters

    counters = safe_counters()
    assert "derived" in counters, "counters 必须有 derived 子对象"
    derived = counters["derived"]
    assert set(derived) == set(derived_counters.COUNTER_KEYS), (
        "键集不稳定：{} vs {}".format(sorted(derived), sorted(derived_counters.COUNTER_KEYS))
    )
    assert set(derived) == EXPECTED_DERIVED_KEYS, "S10 的 compaction_summary 必须在零态就存在（缺席=读不到）"
    assert all(value == 0 for value in derived.values()), derived


# --------------------------------------------------------------------------
# ② 真会动（且只在失败时动）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s10_store_failure_bumps_but_success_does_not(monkeypatch):  # noqa: ANN001
    """② **真会动**：摘要**落库真的抛**时 `compaction_summary` +1；**成功路径**计数不动。

    两条腿合起来才排得掉「调用即计数」：只钉正腿的话，把 bump 挪到「每次压缩都记」也照样绿。
    """
    from app.session import compaction

    compaction.reset_breakers()
    _tighten(monkeypatch)
    rows = _rows()

    # —— 负腿：落库成功 ⇒ 计数**不动** ——
    ok_chat = _fake_chat(4243)
    ok_db = _CommitOkDB()
    ok = await compaction.compact_history(ok_db, ok_chat, rows, model=_FakeSummaryModel())
    assert ok_db.commits == 1, "确实走到了 SAVEPOINT flush 那一步"
    assert ok.applied is True and ok.reason == "", "负腿的前提是「真成功」"
    assert derived_counters.failure_counts().get("compaction_summary", 0) == 0, (
        "成功路径不该计数 ⇒ 否则计数退化成「调用即计数」"
    )

    # —— 正腿：落库必抛 ⇒ 计数 +1 ——
    bad_chat = _fake_chat(4244)
    bad_db = _CommitFailsDB()
    failed = await compaction.compact_history(bad_db, bad_chat, rows, model=_FakeSummaryModel())
    assert bad_db.commits == 1, "确实走到了 SAVEPOINT flush 那一步"
    assert failed.attempted is True and failed.applied is False
    assert failed.reason == "store_failed", "走的必须是 store 这一支（不是 model_failed）"
    assert bad_chat.context_summary is None, "失败摘要不许留在内存对象上"
    assert compaction.breaker_note(4244) == 1, "熔断语义不动（本接线只加出口）"

    counts = derived_counters.failure_counts()
    assert counts.get("compaction_summary", 0) == 1, (
        "摘要落库失败被吞了但计数没动 ⇒ 该失败仍不可观测",
        counts,
    )
    assert counts.get("note_links", 0) == 0 and counts.get("doc_links", 0) == 0, (
        "别串键：本条只该动 compaction_summary",
        counts,
    )


# --------------------------------------------------------------------------
# ③ 进可见面（GET /api/agent/audit 的 counters.derived）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s10_store_failure_reaches_the_audit_endpoint(client, monkeypatch):  # noqa: ANN001
    """③ **进可见面**：真驱动一次落库失败 ⇒ `GET /api/agent/audit` 读得到**同一个数**。

    本腿**不手动 bump**（既有 083 载体的 ③ 是直接 `bump` 再读端点）：
    它把「真失败 ⇒ 计数 ⇒ 端点」整条链一次钉住，**删掉接线就红**。
    """
    from app.session import compaction

    compaction.reset_breakers()
    _tighten(monkeypatch)
    derived_counters.reset_failure_counts()

    chat = _fake_chat(4245)
    result = await compaction.compact_history(
        _CommitFailsDB(), chat, _rows(), model=_FakeSummaryModel()
    )
    assert result.reason == "store_failed"
    assert derived_counters.failure_counts().get("compaction_summary", 0) == 1

    resp = await client.get("/api/agent/audit")
    assert resp.status_code == 200, resp.text
    counters = resp.json().get("counters") or {}
    assert "derived" in counters, "counters 缺 derived：{}".format(sorted(counters))
    assert counters["derived"]["compaction_summary"] == 1, counters["derived"]
    assert counters["derived"]["compaction_summary"] == derived_counters.failure_counts()["compaction_summary"], (
        "端点读数与进程内计数必须同源（两套真相 = 假绿）"
    )


# --------------------------------------------------------------------------
# 补腿 · ① memory 抽取的两级失败计数（地图 #10 / #11：**零判据载体**）
# --------------------------------------------------------------------------


def _stub_call_model(reply: str = "", error: Exception | None = None):
    """替掉 `memory._call_model`：要么回固定正文、要么必抛（= 把失败注入在**计数分支的上游**）。"""

    async def _call(_model, _material):  # noqa: ANN001, ANN002
        if error is not None:
            raise error
        return reply

    return _call


@pytest.mark.asyncio
async def test_leg_memory_call_failure_is_counted_and_isolated(monkeypatch):  # noqa: ANN001
    """补腿（地图 #10）：`_extract_facts` 的**模型调用失败**计数此前**零载体**。

    钉两点：① 失败被隔离（返回空列表、不抛）；② `call_failed` +1，且**不混进**别的桶。
    """
    from app.core.config import settings
    from app.session import memory

    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    monkeypatch.setattr(
        memory, "_call_model", _stub_call_model(error=RuntimeError("注入的抽取调用失败（本用例故意）"))
    )

    assert await memory._extract_facts(object(), "问", "答") == [], "抽取失败必须被隔离（不抛）"
    counts = memory.failure_counts()
    assert counts["call_failed"] == 1, counts
    assert counts["parse_failed"] == 0 and counts["empty"] == 0, "失败要分级，不许混桶"


@pytest.mark.asyncio
async def test_leg_memory_parse_failure_and_empty_boundary(monkeypatch):  # noqa: ANN001
    """补腿（地图 #11 + 「2/3 分界」）：**解析失败**与**解析成功但为空**是两回事。

    ⚠️ 地图对 S11 的口径：`parse_failed`（压根解析不出对象）与 `empty`（解析得出但 facts 为空
    = **正常**）分桶，这条分界线此前**零载体** ⇒ 把两者合桶不会有任何用例变红。
    """
    from app.core.config import settings
    from app.session import memory

    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)

    # 正腿：模型回了一段**压根不是 JSON** 的正文 ⇒ 解析失败
    monkeypatch.setattr(memory, "_call_model", _stub_call_model(reply="这不是 JSON，只是一段话。"))
    assert await memory._extract_facts(object(), "问", "答") == []
    assert memory.failure_counts()["parse_failed"] == 1, memory.failure_counts()

    # 边界腿：解析得出、但 facts 为空 ⇒ 记 empty（**正常**），不许记成 parse_failed
    memory.reset_failure_counts()
    monkeypatch.setattr(memory, "_call_model", _stub_call_model(reply='{"facts": []}'))
    assert await memory._extract_facts(object(), "问", "答") == []
    counts = memory.failure_counts()
    assert counts["empty"] == 1 and counts["parse_failed"] == 0, counts

    # 负腿：真抽到事实 ⇒ 三个失败计数一个都不动
    memory.reset_failure_counts()
    monkeypatch.setattr(
        memory,
        "_call_model",
        _stub_call_model(reply='{"facts": [{"text": "用户在验收可见面", "kind": "knowledge"}]}'),
    )
    facts = await memory._extract_facts(object(), "问", "答")
    assert facts == [{"text": "用户在验收可见面", "kind": "knowledge"}], facts
    counts = memory.failure_counts()
    assert counts["call_failed"] == 0 and counts["parse_failed"] == 0 and counts["empty"] == 0, counts


# --------------------------------------------------------------------------
# 补腿 · S14 clean_status 的磁盘读失败（地图 S14：**零判据载体**）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leg_clean_status_unreadable_file_is_fail_closed(tmp_path):  # noqa: ANN001
    """补腿（地图 S14）：`clean_status._read_disk_text` 的**读失败**此前**零载体**。

    三条腿互为对照（同一用例内）：
      · 文件不存在（`OSError`）⇒ **判为过期**（fail-closed，不许静默当「未过期」）；
      · 内容已变 ⇒ 判为过期；
      · 内容逐字相同 ⇒ **不判**过期。
    变异判别力：把 fail-closed 那条改成「跳过读不到的文档」⇒ 本用例变红。
    """
    from app.pipeline import clean_status

    class _StubSession:
        """最小会话：`stale_content_documents` 只要 `execute(...).scalars().all()`。"""

        def __init__(self, docs) -> None:  # noqa: ANN001
            self._docs = list(docs)

        async def execute(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
            scalars = SimpleNamespace(all=lambda: list(self._docs))
            return SimpleNamespace(scalars=lambda: scalars)

    gone = tmp_path / "已删除.py"  # 从不创建 ⇒ read_bytes 抛 FileNotFoundError(OSError)
    doc_missing = SimpleNamespace(id=7, file_path=str(gone), raw_content="正文")
    assert await clean_status.stale_content_documents(_StubSession([doc_missing])) == [7], (
        "读不到磁盘必须 fail-closed 判过期 —— 静默当「未过期」正是 S14 的吞点形态"
    )

    changed = tmp_path / "改过了.py"
    changed.write_text("改过的正文", encoding="utf-8")
    doc_changed = SimpleNamespace(id=8, file_path=str(changed), raw_content="正文")
    assert await clean_status.stale_content_documents(_StubSession([doc_changed])) == [8]

    same = tmp_path / "一致.py"
    same.write_text("正文", encoding="utf-8")
    doc_same = SimpleNamespace(id=9, file_path=str(same), raw_content="正文")
    assert await clean_status.stale_content_documents(_StubSession([doc_same])) == [], (
        "内容逐字相同 ⇒ 不许判过期（否则「读失败」那条腿就没有对照）"
    )
