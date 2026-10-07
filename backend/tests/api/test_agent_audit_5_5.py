"""``GET /api/agent/audit`` 的条目 **5.5（可观测）** 扩展面：A2 / A3 / A4 / A5 / A8 / A9 / A11 / A13。

本文件与 ``test_agent_audit_endpoint.py`` 的分工：**那份钉 4.3.2 的既有四律**（外层 ``{"items": […]}"`` /
``limit`` 1..500 夹取 / ``id`` 倒序 / 只读 / 空表），本文件钉 **5.5 的只增扩展**。5.5 的契约义务是
「相对**改前快照**只增不减」，所以第一组用例把历史服务响应**逐字钉成常量**，
验证现有字段继续存在；测试直接使用本文件常量。

四律**不在本文件重复断言**：既有那份文件在 5.5 里只做了「精确键集 → 只增超集」的**升级**，
不是删断言（开工包 §7.2 第 1 条的硬要求）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent import audit as agent_audit
from app.api import chat_routes
from app.cache import store as cache_store
from app.core.config import settings
from app.models import AgentRun, AgentStep
from app.retrieval import graph
from app.session import memory as memory_mod
from tests.api.test_chat_contract import _StubClassifier, client, parse_sse  # noqa: F401
from tests.api.test_semantic_cache_http import (  # noqa: F401 —— 复用 5.2 那套：不重复造假模型
    _CountingModel,
    _install,
    _materials_from,
)
from tests.retrieval.conftest import seed_document

pytestmark = pytest.mark.asyncio

#: ``回归检查5改前形状.json`` 的**逐字副本**（2026-09-19 真服务实测；改前 ``top_keys`` 只有一个键）
BEFORE_TOP_KEYS = ["items"]
#: 改前 ``items[]`` 的 11 键——5.5 之后必须**逐字仍在**（只许多出第 12 键 ``run``）
BEFORE_ITEM_KEYS = [
    "id",
    "run_id",
    "step_no",
    "type",
    "tool_name",
    "tool_call_id",
    "content",
    "meta",
    "created_at",
    "mode",
    "status",
]
#: 改前 OpenAPI 里这个端点的查询参数（5.5 只许**追加**，不许改这两个的名字 / 顺序 / 可选性）
BEFORE_QUERY_PARAMS = ["chat_id", "limit"]
#: 改前真服务数出来的 OpenAPI 路径数；历史基准保留。
BEFORE_OPENAPI_PATH_COUNT = 45
#: 02 v1.0.35 用户授权的三个具名新增接口（同机设置页导入离线模型）。
APPROVED_MODEL_IMPORT_PATHS = {
    "/api/settings/offline-models",
    "/api/settings/offline-models/import",
    "/api/system/pick-model-archive",
}
#: 5.5 新增的 3 个顶层键 / 第 12 个 item 键 / 6 个查询参数（**显式列出**，少一个就红）
ADDED_TOP_KEYS = {"runs", "query", "counters"}
ADDED_ITEM_KEY = "run"
ADDED_QUERY_PARAMS = {"run_id", "status", "type", "since", "until", "offset"}

#: 「参数没给」与「参数明确给 None」必须能区分（``finished_at=None`` 是**合法**取值：
#: 未收口的 run 就该给 ``null``）——用一个私有哨兵，别用 ``None`` 当默认。
_UNSET: object = object()


# --------------------------------------------------------------------------
# 造数助手
# --------------------------------------------------------------------------


class _FixedRetrieval:
    """把检索换成本地固定材料（真 ``chunk_id`` ⇒ 5.2 的缓存存活校验能过；零出网）。"""

    def __init__(self, materials, text: str) -> None:
        self.materials = materials
        self.text = text
        self.calls: list[str] = []

    async def __call__(self, db, text, filters=None, *, stats=None):  # noqa: ANN001
        self.calls.append(text)
        if stats is not None:
            stats.status = "disabled"
            stats.candidates = len(self.materials)
        return list(self.materials), self.text, ""


class _ExtractBoomModel:
    """回答照常（第 1 次调用），**抽取那次**（第 2 次）抛 —— 用来让 ``memory.call_failed`` 真 +1。

    ⚠️ 这条是 A9 的关键：`5.1` 的失败分级计数此前**只在模块内 + 日志**里，没有出口；
    「开关打开 ⇒ 真的能从端点读到非 0 的失败数」是**面板化债收口**的实证。
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    async def generate(self, messages, system_prompt=None, max_output_tokens=None, **extra):  # noqa: ANN001
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("模拟抽取调用挂了")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.reply)], stop_reason="end_turn"
        )

    async def stream(self, messages, system_prompt=None, **extra):  # noqa: ANN001
        raise RuntimeError("本用例只走同步链路")


async def _seed_run(  # noqa: ANN001
    db,
    *,
    steps: int = 2,
    mode: str = "react",
    status: str = "done",
    started_at: datetime | None = None,
    finished_at: datetime | None | object = _UNSET,
    run_meta: dict | None = None,
    created_at: datetime | None = None,
    titles: str = "5.5 审计用例",
    with_message: bool = False,
):
    """造「真会话 + 1 run + N step」，返回 ``(chat_id, run_id, [step_id 升序], message_id)``。

    ⚠️ 会话必须是**真行**（``agent_runs.chat_id`` 有外键）；时间戳**显式给值**是为了让
    ``since`` / ``until`` 与 ``runs[]`` 排序这三条断言有确定的输入（服务器默认值不可控）。
    """
    from app.session import repository as repo

    chat = await repo.create_chat(db, title=titles)
    await db.flush()
    message_id = None
    if with_message:
        message = await repo.add_message(db, chat_id=chat.id, role="assistant", content="回答正文")
        message_id = int(message.id)
    run = AgentRun(
        chat_id=chat.id,
        message_id=message_id,
        mode=mode,
        status=status,
        started_at=started_at or datetime(2026, 1, 1, 12, 0, 0),
        # 哨兵判据：``finished_at=None`` 是「明确要 NULL」，只有**没传**才用默认时间
        finished_at=(
            datetime(2026, 1, 1, 12, 0, 5) if finished_at is _UNSET else finished_at  # type: ignore[arg-type]
        ),
        meta=run_meta if run_meta is not None else {"retrieval_id": "rl-5.5", "turn": 3},
    )
    db.add(run)
    await db.flush()
    ids: list[int] = []
    for index in range(1, steps + 1):
        step = AgentStep(
            run_id=run.id,
            step_no=index,
            type="tool_call",
            tool_name="knowledge_search",
            tool_call_id=f"tc-{run.id}-{index}",
            content=f"观察 {index}",
            meta={"ok": index % 2 == 1, "ms": 10 * index, "args": {"query": "蓝鲸"}},
            created_at=(created_at or datetime(2026, 1, 1, 12, 1, 0)) + timedelta(seconds=index),
        )
        db.add(step)
        await db.flush()
        ids.append(int(step.id))
    await db.commit()
    return int(chat.id), int(run.id), ids, message_id


async def _audit(client, **params):  # noqa: ANN001
    """打一次端点并断言 200（返回解析后的 body）。"""
    resp = await client.get("/api/agent/audit", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ==========================================================================
# A2 / A3 —— 只增不减（以改前真服务快照为基准）
# ==========================================================================


async def test_a2_openapi_path_set_is_still_the_frozen_catalog():
    """**A2**：OpenAPI 路径集 == 当前契约表唯一路径 + 根路径 ``/``。

    ``tests/contract/test_endpoints_catalog.py::ENDPOINTS`` 是冻结的 56 条 ``(method, path)``；
    这里用**等式**（不是包含）钉住路径集合：**多一条就红**——「不新造端点」是纪律，
    原 45 条路径基准保留；02 v1.0.35 只批准上面三条具名新增路径。
    """
    from main import app
    from tests.contract.test_endpoints_catalog import ENDPOINTS

    paths = set(app.openapi()["paths"])
    expected = {path for _method, path in ENDPOINTS} | {"/"}
    assert APPROVED_MODEL_IMPORT_PATHS <= expected
    extra, missing = sorted(paths - expected), sorted(expected - paths)
    assert (extra, missing) == ([], []), f"路径集变了（新增 / 删除端点）：多 {extra}／少 {missing}"
    assert len(paths - APPROVED_MODEL_IMPORT_PATHS) == BEFORE_OPENAPI_PATH_COUNT, "原 45 条路径数量必须保留"


async def test_a3_pre_change_shape_is_a_subset_of_the_new_shape(client, db):  # noqa: ANN001
    """**A3**：改前顶层键 / 11 个 item 键**逐字仍在**，只多出 3 个顶层键与第 12 键 ``run``。"""
    chat_id, _run_id, _ids, _message_id = await _seed_run(db, steps=1)
    body = await _audit(client, chat_id=chat_id)

    assert set(BEFORE_TOP_KEYS) <= set(body), "改前顶层键必须逐字保留"
    assert set(body) == set(BEFORE_TOP_KEYS) | ADDED_TOP_KEYS, "只增：恰好这 4 个顶层键"

    item = body["items"][0]
    assert set(BEFORE_ITEM_KEYS) <= set(item), "改前 11 键必须逐字保留（名字一个都不许变）"
    assert set(item) == set(BEFORE_ITEM_KEYS) | {ADDED_ITEM_KEY}, "只增：第 12 键是 run"
    # 11 键里的值语义也照旧（这半段是 4.3.2 的既有口径，这里只是不复看新键）
    assert item["step_no"] == 1
    assert item["type"] == "tool_call"
    assert item["tool_name"] == "knowledge_search"
    assert item["content"] == "观察 1"
    assert item["meta"]["ms"] == 10
    assert item["mode"] == "react"
    assert item["status"] == "done"


async def test_a3_query_params_are_only_appended(client):
    """**A3**：改前的 ``chat_id`` / ``limit`` 仍在（名字与顺序不变），6 个新参数**追加**。"""
    from main import app

    params = app.openapi()["paths"]["/api/agent/audit"]["get"]["parameters"]
    names = [p["name"] for p in params]
    assert names[:2] == BEFORE_QUERY_PARAMS, f"改前两个参数的名字与顺序都不许动：{names}"
    assert ADDED_QUERY_PARAMS <= set(names), f"新增参数少了：{sorted(ADDED_QUERY_PARAMS - set(names))}"
    assert all(p.get("required") is False for p in params), "查询参数一律可选（不改既有调用）"


# ==========================================================================
# A4 —— run 级字段有出口、逐字段对库
# ==========================================================================


async def test_a4_run_projection_has_eight_keys_and_matches_the_db_row(client, db):  # noqa: ANN001
    """**A4**：``items[].run`` 的 8 键齐全，且 ``started_at`` / ``finished_at`` /
    ``message_id`` / ``meta`` 与库内行**逐一相等**（这 4 个键在 5.5 之前**零出口**）。"""
    started = datetime(2026, 2, 3, 9, 30, 15)
    finished = datetime(2026, 2, 3, 9, 30, 42)
    chat_id, run_id, _ids, message_id = await _seed_run(
        db,
        steps=1,
        started_at=started,
        finished_at=finished,
        run_meta={"retrieval_id": "rl-abc", "turn": 7},
        with_message=True,
    )
    body = await _audit(client, chat_id=chat_id)
    run = body["items"][0]["run"]

    assert set(run) == {
        "id",
        "chat_id",
        "mode",
        "status",
        "started_at",
        "finished_at",
        "message_id",
        "meta",
    }
    row = await db.get(AgentRun, run_id)
    assert run["id"] == int(row.id) == run_id
    assert run["chat_id"] == int(row.chat_id) == chat_id
    assert run["mode"] == row.mode == "react"
    assert run["status"] == row.status == "done"
    assert run["started_at"] == started.isoformat(), "started_at 改前零出口，必须与库内值逐字相等"
    assert run["finished_at"] == finished.isoformat()
    assert run["message_id"] == int(row.message_id) == message_id
    assert run["meta"] == {"retrieval_id": "rl-abc", "turn": 7}
    # 既有键与新键**同源**（mode/status 两处同值，不是各算一份）
    assert run["mode"] == body["items"][0]["mode"]
    assert run["status"] == body["items"][0]["status"]


async def test_a4_unfinished_run_has_null_finished_at(client, db):  # noqa: ANN001
    """``finished_at`` 可空（未收口的 run）⇒ 如实给 ``null``，不编一个值。"""
    chat_id, _run_id, _ids, _mid = await _seed_run(db, steps=1, status="running", finished_at=None)
    run = (await _audit(client, chat_id=chat_id))["items"][0]["run"]
    assert run["status"] == "running"
    assert run["finished_at"] is None


async def test_a4_runs_are_deduped_and_ordered_by_started_at_desc(client, db):  # noqa: ANN001
    """**A4**：``runs[]`` = 本次查询涉及的 run 摘要，**去重**、按 ``started_at`` **倒序**。"""
    base = datetime(2026, 3, 1, 8, 0, 0)
    first_chat, first_run, _a, _b = await _seed_run(db, steps=2, started_at=base, titles="5.5-早")
    second_chat, second_run, _c, _d = await _seed_run(
        db, steps=3, started_at=base + timedelta(hours=1), titles="5.5-晚"
    )
    body = await _audit(client, limit=500)

    assert body["query"]["count"] == len(body["items"]) == 5, "本次查询涉及 5 条 step"
    assert [run["id"] for run in body["runs"]] == [second_run, first_run], "倒序 + 去重"
    assert len({run["id"] for run in body["runs"]}) == 2, "同一个 run 的多条 step 只能出现在一处"
    # 只回**本次查询涉及**的 run（不是全表 run）
    scoped = await _audit(client, chat_id=first_chat)
    assert [run["id"] for run in scoped["runs"]] == [first_run]
    assert second_chat  # 第二个会话确实造了数据（防止这条断言退化成空跑）


async def test_a4_step_meta_and_run_meta_do_not_mix(client, db):  # noqa: ANN001
    """⚠️ 两个 ``meta`` **都要给、别混**：step 级有 ``ok/ms/args``，run 级有 ``retrieval_id/turn``。"""
    chat_id, _run_id, _ids, _mid = await _seed_run(db, steps=1)
    item = (await _audit(client, chat_id=chat_id))["items"][0]
    assert set(item["meta"]) == {"ok", "ms", "args"}, "step 级 meta"
    assert set(item["run"]["meta"]) == {"retrieval_id", "turn"}, "run 级 meta"
    assert "retrieval_id" not in item["meta"]
    assert "ok" not in item["run"]["meta"]


# ==========================================================================
# A5 —— 过滤与分页（6 个新参数逐条）
# ==========================================================================


async def test_a5_run_id_filter_and_unknown_id_is_not_404(client, db):  # noqa: ANN001
    """``run_id`` 只查该 run 的 step；**不存在的 id ⇒ ``items: []``，不 404**（A8 同条）。"""
    _chat_a, run_a, _ids_a, _ma = await _seed_run(db, steps=3, titles="5.5-A")
    _chat_b, run_b, _ids_b, _mb = await _seed_run(db, steps=2, titles="5.5-B")

    body = await _audit(client, run_id=run_a)
    assert [item["run_id"] for item in body["items"]] == [run_a] * 3
    assert body["query"]["filters"]["run_id"] == run_a
    assert [run["id"] for run in body["runs"]] == [run_a]

    missing = await client.get("/api/agent/audit", params={"run_id": 99999999})
    assert missing.status_code == 200, "未知 run_id 不是 404（过滤出空集）"
    assert missing.json()["items"] == []
    assert missing.json()["runs"] == []
    assert run_b  # 另一个 run 确实存在（不是「库本来就空」）


async def test_a5_status_filter_and_bad_value(client, db):  # noqa: ANN001
    """``status`` 按 run 状态过滤（**下推 SQL**）；域外取值 ⇒ **422 + 可读中文**。"""
    _chat_done, run_done, _a, _b = await _seed_run(db, steps=2, status="done", titles="5.5-done")
    _chat_fail, run_fail, _c, _d = await _seed_run(db, steps=1, status="failed", titles="5.5-fail")

    only_done = await _audit(client, status="done")
    assert {item["run_id"] for item in only_done["items"]} == {run_done}
    assert [run["id"] for run in only_done["runs"]] == [run_done]
    assert only_done["query"]["filters"]["status"] == "done"

    only_failed = await _audit(client, status="failed")
    assert {item["run_id"] for item in only_failed["items"]} == {run_fail}
    assert {run["id"] for run in only_failed["runs"]} == {run_fail}

    bad = await client.get("/api/agent/audit", params={"status": "bogus"})
    assert bad.status_code == 422, f"域外 status 必须 422，不许静默忽略：{bad.status_code}"
    detail = str(bad.json()["detail"])
    assert "bogus" in detail and "status" in detail, detail
    assert any("\u4e00" <= ch <= "\u9fff" for ch in detail), f"detail 要可读中文：{detail}"


async def test_a5_type_filter_is_passed_through(client, db):  # noqa: ANN001
    """``type`` 按 step 类型过滤（现状恒 ``tool_call``，按列取值透传；未命中 ⇒ 空）。"""
    chat_id, _run_id, _ids, _mid = await _seed_run(db, steps=2)
    assert len((await _audit(client, chat_id=chat_id, type="tool_call"))["items"]) == 2
    assert (await _audit(client, chat_id=chat_id, type="thought"))["items"] == []


async def test_a5_since_until_is_a_half_open_interval(client, db):  # noqa: ANN001
    """``since`` / ``until`` 作用在 ``agent_steps.created_at``，区间 ``[since, until)``（**半开**）。"""
    created = datetime(2026, 4, 1, 10, 0, 0)
    chat_id, _run_id, ids, _mid = await _seed_run(db, steps=3, created_at=created)
    stamps = [(created + timedelta(seconds=index)).isoformat() for index in range(1, 4)]

    lower = await _audit(client, chat_id=chat_id, since=stamps[1])
    assert [item["id"] for item in lower["items"]] == [ids[2], ids[1]], "≥ since（含边界本身）"

    window = await _audit(client, chat_id=chat_id, since=stamps[0], until=stamps[2])
    assert [item["id"] for item in window["items"]] == [ids[1], ids[0]], (
        "半开区间 [since, until) ⇒ 含 since 那条、**不含** until 那条"
    )
    assert ids[2] not in {item["id"] for item in window["items"]}, "until 边界那条必须被排除"
    assert window["query"]["filters"]["since"] == stamps[0], "回显的是生效值（归一后的）"
    assert window["query"]["filters"]["until"] == stamps[2]
    assert window["query"]["count"] == 2


@pytest.mark.parametrize("field,value", [("since", "notatime"), ("until", "2026-13-45")])
async def test_a5_bad_time_is_422_with_readable_chinese(client, field, value):  # noqa: ANN001
    """坏时间串 ⇒ **422 + 可读中文**（**不许**静默当成「没过滤」——那会把查全当成查准）。"""
    resp = await client.get("/api/agent/audit", params={field: value})
    assert resp.status_code == 422, f"{field}={value!r} 应当 422，实际 {resp.status_code}"
    detail = str(resp.json()["detail"])
    assert field in detail and value in detail, detail
    assert any("\u4e00" <= ch <= "\u9fff" for ch in detail), f"detail 要可读中文：{detail}"


@pytest.mark.parametrize("field", ["since", "until", "status", "type"])
@pytest.mark.parametrize("blank", ["", "   "])
async def test_a5_blank_filter_means_no_filter_and_never_422(client, db, field, blank):  # noqa: ANN001
    """**空串 / 纯空白 = 没给该过滤**（``?since=`` 与 ``?since=%20`` 行为必须**一致**）。

    ⚠️ 这条是**对抗性审计抓到的**口径破口：原先 ``?since=``（空串）被 `if since else None` 静默
    吞成「没过滤」，而 ``?since=%20``（空白）走 helper 抛 **422 中文**——**相邻两种输入行为相反**，
    且契约写的是「坏值一律 422」。裁决：**空 = 没给**（清空筛选框 / 表单空字段的自然语义，
    也是 ``type`` / ``status`` 的既有写法），**真解析不了才 422**；口径已写进 ``02`` v1.0.4。
    """
    chat_id, _run_id, _ids, _mid = await _seed_run(db, steps=2)
    body = await _audit(client, chat_id=chat_id, **{field: blank})
    assert body["query"]["count"] == 2, f"{field}={blank!r} 应当等于「没给过滤」"
    assert body["query"]["filters"][field] is None
    assert len(body["items"]) == 2


async def test_a5_offset_paginates_and_never_goes_negative(client, db):  # noqa: ANN001
    """``offset`` 与 ``limit`` 组合分页；负值**夹 0**（与 ``safe_limit`` 同口径，不 422）。"""
    chat_id, _run_id, ids, _mid = await _seed_run(db, steps=5)
    desc = list(reversed(ids))

    first = await _audit(client, chat_id=chat_id, limit=2)
    assert [item["id"] for item in first["items"]] == desc[:2]
    assert first["query"] == {
        "limit": 2,
        "offset": 0,
        "count": 2,
        "filters": {
            "chat_id": chat_id,
            "run_id": None,
            "status": None,
            "type": None,
            "since": None,
            "until": None,
        },
    }

    second = await _audit(client, chat_id=chat_id, limit=2, offset=2)
    assert [item["id"] for item in second["items"]] == desc[2:4]
    assert second["query"]["offset"] == 2 and second["query"]["count"] == 2

    negative = await _audit(client, chat_id=chat_id, limit=2, offset=-5)
    assert [item["id"] for item in negative["items"]] == desc[:2], "负 offset 夹 0"
    assert negative["query"]["offset"] == 0


# ==========================================================================
# A8 —— 边界不炸
# ==========================================================================


async def test_a8_empty_table_gives_empty_arrays_and_full_counters_shape(client):
    """空库 ⇒ ``items`` / ``runs`` 是 ``[]``，``query.count`` 为 0，``counters`` 形状仍在。"""
    body = await _audit(client, chat_id=999999)
    assert body["items"] == []
    assert body["runs"] == []
    assert body["query"]["count"] == 0
    # ⚠️ 口径升级（2026-09-26 · `回归修复`）：**从「精确键集」改为「改前键逐字 + 只增超集」**。
    # 理由：`counters` 是**只增不减**的观测面（`5.5` 的成文纪律），而 `083` 新增了 `derived`
    # 子对象（派生数据失败出口）。精确键集会**把任何只增都判成回归**，与契约口径相反。
    # 本条仍**逐字钉住 5.5 当时的六个键**，只放开「可新增」。
    _A8_BASE_KEYS = {"_note", "events", "audit", "graph", "cache", "memory"}
    assert _A8_BASE_KEYS <= set(body["counters"]), (
        f"counters 丢了 5.5 的既有键：{sorted(_A8_BASE_KEYS - set(body['counters']))}"
    )
    assert body["counters"]["graph"]["calls"] == 0


async def test_a8_offset_beyond_range_is_empty_not_error(client, db):  # noqa: ANN001
    """``offset`` 超出范围 ⇒ 空数组（200，不是 500 / 404）。"""
    chat_id, _run_id, _ids, _mid = await _seed_run(db, steps=2)
    body = await _audit(client, chat_id=chat_id, offset=9999)
    assert body["items"] == [] and body["runs"] == []
    assert body["query"]["offset"] == 9999


async def test_a8_limit_semantics_are_untouched(client, db):  # noqa: ANN001
    """``limit`` 仍夹 1..500（``0`` → 1、超大 → 上限）；``offset`` **不改变** ``limit`` 的夹取语义。"""
    chat_id, _run_id, _ids, _mid = await _seed_run(db, steps=4)
    zero = await _audit(client, chat_id=chat_id, limit=0)
    assert len(zero["items"]) == 1 and zero["query"]["limit"] == 1
    huge = await _audit(client, chat_id=chat_id, limit=9999)
    assert huge["query"]["limit"] == agent_audit.MAX_AUDIT_LIMIT == 500
    assert len(huge["items"]) == 4, "只有 4 行，夹到 500 也只给 4 行"


# ==========================================================================
# A9 —— counters 有真数字（两态对照）
# ==========================================================================


async def test_a9_counter_keys_match_each_module_verbatim():
    """**A9**：``counters`` 的键名 = 各模块既有计数的键名**逐字**（不许自创同义名）。

    graph 的 ``failure_counts()`` 是**稀疏**字典（只回被 bump 过的键）⇒ 与它的比对用包含关系；
    另外拿**模块源码文本**做一次交叉核对（那 8 个键必须在 ``graph.py`` 里真的出现），
    这样「测试里这 8 个键」不是从测试自己身上自证的。
    """
    from app.session import events as events_mod

    counters = agent_audit.safe_counters()
    assert counters["_note"] == agent_audit.COUNTERS_NOTE
    assert set(counters["events"]) == {"write_failures", "incomplete_total"}
    assert set(counters["audit"]) == {"write_failures"}

    graph_keys = {
        "calls",
        "seeds",
        "neighbors",
        "materials",
        "failures",
        "no_terms",
        "no_budget",
        "no_seeds",
    }
    assert set(counters["graph"]) == graph_keys
    assert set(graph.failure_counts()) <= graph_keys, "模块真多了键 ⇒ 这里必须同步（只增）"
    graph_source = (
        Path(__file__).resolve().parents[2] / "app" / "retrieval" / "graph.py"
    ).read_text(encoding="utf-8")
    missing = sorted(key for key in graph_keys if key not in graph_source)
    assert missing == [], f"这些键在 graph.py 里根本不存在（自创？）：{missing}"

    assert set(counters["cache"]) == set(cache_store.failure_counts())
    assert set(counters["cache"]) == {"hit", "miss", "stale", "stored", "evicted", "expired", "error"}
    assert set(counters["memory"]) == set(memory_mod.failure_counts())
    # 🔴 2026-09-27 第二批升级（`回归修复` #12，业主拍板⑩「按 5.5 只增不减纪律升超集」，
    # A8 先例）：本行原为**精确键集**断言 `== {"call_failed","parse_failed","empty","stored"}`，
    # 它把 memory 家族**任何只增**都判成回归 ⇒ 升级为「既有四键**逐字**在位 + 允许只增超集」。
    assert {"call_failed", "parse_failed", "empty", "stored"} <= set(counters["memory"])
    assert counters["memory"]["store_failed"] == memory_mod.failure_counts()["store_failed"]
    assert counters["events"] == {
        "write_failures": events_mod.event_write_failures(),
        "incomplete_total": events_mod.event_incomplete_total(),
    }
    assert counters["audit"]["write_failures"] == agent_audit.audit_write_failures()


async def test_a9_counter_read_failure_falls_back_to_all_zeros(monkeypatch):
    """**A9 / I5**：任一模块读失败 / 不可导入 ⇒ 该子对象回落**全 0**，**绝不抛**。

    钉的是「观测项不该让被观测的请求炸掉」：延迟 import + 逐块 try 的组合。
    """
    from app.session import events as events_mod

    def _boom():  # noqa: ANN202
        raise RuntimeError("模拟模块不可用")

    monkeypatch.setattr(agent_audit, "_read_graph_counters", _boom)
    monkeypatch.setattr(agent_audit, "_read_cache_counters", _boom)
    monkeypatch.setattr(agent_audit, "_read_memory_counters", _boom)
    monkeypatch.setattr(events_mod, "event_write_failures", _boom)
    monkeypatch.setattr(events_mod, "event_incomplete_total", _boom)

    counters = agent_audit.safe_counters()  # 不抛
    assert set(counters["graph"].values()) == {0}
    assert set(counters["cache"].values()) == {0}
    assert set(counters["memory"].values()) == {0}
    assert counters["events"] == {"write_failures": 0, "incomplete_total": 0}


async def test_a9_closed_state_turn_moves_no_counter(client, db, monkeypatch):  # noqa: ANN001
    """**A9**：默认关态跑一整轮问答，``counters`` **逐项不变**（「关 = 零影响」的观测面证据）。

    用「前后快照相等」而不是「绝对值 == 0」：进程内计数**跨用例共享**（本仓踩过「进程全局
    计数 + 逐条断言」），绝对值会被别的用例污染；**增量**才是这条要证明的东西。
    """
    materials, text = await _materials_from(db)
    monkeypatch.setattr(chat_routes, "retrieve_materials", _FixedRetrieval(materials, text))
    _install(monkeypatch)

    before = agent_audit.safe_counters()
    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert resp.status_code == 200, resp.text
    after = agent_audit.safe_counters()

    for section in ("events", "audit", "graph", "cache", "memory"):
        assert after[section] == before[section], (
            f"关态下 {section} 计数不该动：{before[section]} → {after[section]}"
        )
    assert after["graph"]["calls"] == 0, "关态不该进入图通道（conftest 的钉子已清零）"
    assert after["cache"]["hit"] == 0
    assert _CountingModel  # noqa: B018 —— 用的是 5.2 那份假模型（同源，不另造）


async def test_a9_cache_hits_are_visible_in_counters(client, db, monkeypatch):  # noqa: ANN001
    """**A9（开态真数字）**：``SEMANTIC_CACHE_ENABLE=true`` 命中一次后 ``counters.cache.hit ≥ 1``。

    同一份数字必须与 ``cache.failure_counts()`` 完全一致——**两套读法一个真相源**。
    """
    materials, text = await _materials_from(db)
    monkeypatch.setattr(chat_routes, "retrieve_materials", _FixedRetrieval(materials, text))
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    cache_store.reset_cache()
    cache_store.reset_failure_counts()
    _install(monkeypatch)

    before = agent_audit.safe_counters()["cache"]["hit"]
    assert before == 0, "conftest 的钉子已清零"
    question = {"message": "向量库用什么存储？"}
    assert (await client.post("/api/chat", json=question)).status_code == 200
    await chat_routes.drain_cache_tasks()
    assert (await client.post("/api/chat", json=question)).status_code == 200

    counters = agent_audit.safe_counters()["cache"]
    assert counters["hit"] == before + 1, f"命中轮之后 counters.cache.hit = {counters['hit']}"
    assert counters == cache_store.failure_counts(), "端点投影必须与模块自己的读法一致"
    cache_store.reset_cache()
    cache_store.reset_failure_counts()


async def test_a9_graph_channel_calls_are_visible_in_counters(client, db, monkeypatch):  # noqa: ANN001
    """**A9（开态真数字）**：``GRAPHRAG_ENABLE=true`` 跑一轮 ⇒ ``counters.graph.calls > 0``。

    ⚠️ **不**替换 ``chat_routes.retrieve_materials``——图通道就在它里面，替掉就等于没测。
    向量路由 ``tests/api/conftest.py`` 的钉子钉成假件（零出网），关键词路读的是测试库。
    """
    await seed_document(db, ["系统概览 讲的是整体架构。"], filename="系统概览.md")
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", True)
    graph.reset_counters()
    _install(monkeypatch)

    resp = await client.post("/api/chat", json={"message": "系统概览说的是什么？"})
    assert resp.status_code == 200, resp.text

    counters = agent_audit.safe_counters()["graph"]
    assert counters["calls"] > 0, f"开态跑了一轮，图通道却没被进入：{counters}"
    assert counters["calls"] == graph.failure_counts().get("calls", 0), "两套读法必须同源"
    graph.reset_counters()


async def test_a9_memory_extraction_failure_is_visible_in_counters(client, db, monkeypatch):  # noqa: ANN001
    """**A9（开态真数字）**：``MEMORY_ENABLE=true`` + 抽取调用失败 ⇒ ``counters.memory.call_failed ≥ 1``。

    这是 `5.1` 的「失败面板」债**真的被收口**的证据：那一类失败此前只落在**模块内 + 日志**里。
    """
    materials, text = await _materials_from(db)
    monkeypatch.setattr(chat_routes, "retrieve_materials", _FixedRetrieval(materials, text))
    model = _ExtractBoomModel("据资料 [1]，向量库用 Qdrant 内嵌模式。")
    monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
    monkeypatch.setattr(
        chat_routes, "resolve_routing_classifier", lambda: _StubClassifier("internal_db")
    )
    monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
    memory_mod.reset_failure_counts()

    resp = await client.post("/api/chat", json={"message": "向量库用什么存储？"})
    assert resp.status_code == 200, resp.text
    await memory_mod.drain_memory_tasks()  # fire-and-forget 的唯一等待点

    assert model.calls == 2, f"应当是「回答 1 次 + 抽取 1 次」，实际 {model.calls}"
    counters = agent_audit.safe_counters()["memory"]
    assert counters["call_failed"] >= 1, f"抽取失败没进 counters：{counters}"
    assert counters == memory_mod.failure_counts(), "两套读法必须同源"
    memory_mod.reset_failure_counts()


async def test_a9_health_still_has_twelve_keys_and_agrees_with_counters(client):
    """⚠️ **有意重复**：``counters.events.write_failures`` 与 ``/api/health`` 的第 12 键**同值**。

    两处语义不同（health 面向就绪、``counters`` 面向面板），但必须是同一个数；
    这条同时钉「health 的 12 键没被本条改掉」（报告里也要写清这不是重复口径）。
    """
    health = await client.get("/api/health")
    assert health.status_code == 200, health.text
    body = health.json()
    assert len(body) == 12, f"/api/health 仍是 12 键，实际 {sorted(body)}"
    counters = (await _audit(client))["counters"]
    assert counters["events"]["write_failures"] == body["event_write_failures"]


# ==========================================================================
# A11 / A13 —— 注释收口 与 零新增依赖
# ==========================================================================


async def test_a11_panel_debt_comments_now_point_to_counters():
    """**A11**：`5.1`/`5.2`/`5.3` 的三处「面板化归 5.5」注释**已就地更新**（不留过期注释）。"""
    app_root = Path(__file__).resolve().parents[2] / "app"
    for rel in ("retrieval/graph.py", "cache/store.py", "session/memory.py"):
        text = (app_root / rel).read_text(encoding="utf-8")
        assert "counters" in text, f"{rel} 的注释没指向 counters"
    stale = sorted(
        str(path.relative_to(app_root))
        for path in app_root.rglob("*.py")
        if "面板化归 5.5" in path.read_text(encoding="utf-8")
    )
    assert stale == [], f"这些文件还留着「面板化归 5.5」的过期注释：{stale}"


async def test_a13_no_new_dependency_in_manifests():
    """**A13**：依赖面仍 **0 命中** Langfuse / OTel SDK / posthog / sentry（本条不装包）。"""
    repo_root = Path(__file__).resolve().parents[3]
    forbidden = ("langfuse", "opentelemetry-sdk", "opentelemetry_sdk", "posthog", "sentry-sdk")
    for rel in ("backend/requirements.txt", "tools/packaging/requirements-lock.txt"):
        text = (repo_root / rel).read_text(encoding="utf-8").lower()
        hits = [name for name in forbidden if name in text]
        assert hits == [], f"{rel} 出现了被裁决禁止的依赖：{hits}"


# ==========================================================================
# 纯函数（无 HTTP）：夹取 / 解析 / 排序 / 去重——端点之外的第二层钉子
# ==========================================================================


async def test_safe_offset_clamps_like_safe_limit():
    """``safe_offset``：负值夹 0、坏值回落 0（与 ``safe_limit`` 同口径，不抛）。"""
    assert agent_audit.safe_offset(0) == 0
    assert agent_audit.safe_offset(-1) == 0
    assert agent_audit.safe_offset("7") == 7
    assert agent_audit.safe_offset(None) == 0
    assert agent_audit.safe_offset("abc") == 0
    assert agent_audit.safe_offset(10**9) == 10**9, "offset 没有上限（有上限的只有 limit）"


async def test_parse_audit_time_normalizes_and_rejects():
    """``parse_audit_time``：接受 ISO / ``Z`` / 带偏移（折到**库内时钟**）；坏值抛中文 ``ValueError``。"""
    assert agent_audit.parse_audit_time("2026-09-19T10:00:00", field="since") == datetime(
        2026, 9, 19, 10, 0
    ), "naive 串按库内时钟原样解释"
    assert agent_audit.parse_audit_time("2026-09-19T10:00:00Z", field="since").tzinfo is None
    for bad in ("", "notatime", "2026-13-45"):
        with pytest.raises(ValueError) as excinfo:
            agent_audit.parse_audit_time(bad, field="since")
        message = str(excinfo.value)
        assert "since" in message and any("\u4e00" <= ch <= "\u9fff" for ch in message), message


async def test_fold_to_db_clock_uses_the_db_clock_not_the_local_one(monkeypatch):
    """⚠️ **对抗性审计抓到的真缺陷**：带偏移的时间要折到**库内时钟**，不是「本机时区」。

    ``agent_steps.created_at`` / ``agent_runs.started_at`` 都是 ``server_default=func.now()``
    = **数据库时钟**：MySQL 的 ``NOW()`` 是服务器本地时间（compose 钉了 ``TZ: Asia/Shanghai``），
    而 SQLite 的 ``func.now()`` 编译成 ``CURRENT_TIMESTAMP`` = **UTC**。折成本机时区在开发态
    （本机就是 +08:00）看不出来，桌面交付档会整段偏一个时区 ⇒ 本条把它钉成两态都断言。

    用 ``db_clock_is_utc`` 打桩而不是依赖本机时区：这样用例在任何时区的机器上都成立
    （对审审计指出「按 ``hour == 10`` 断言」在 UTC 的 CI 上会红）。
    """
    aware = datetime.fromisoformat("2026-09-19T10:00:00+08:00")
    monkeypatch.setattr(agent_audit, "db_clock_is_utc", lambda: True)
    assert agent_audit.fold_to_db_clock(aware) == aware.astimezone(timezone.utc).replace(
        tzinfo=None
    ), "SQLite：折到 UTC"
    monkeypatch.setattr(agent_audit, "db_clock_is_utc", lambda: False)
    assert agent_audit.fold_to_db_clock(aware) == aware.astimezone().replace(
        tzinfo=None
    ), "MySQL：折到服务器本地时间（= 本机时区，开发态两者同值）"
    naive = datetime(2026, 9, 19, 10, 0)
    assert agent_audit.fold_to_db_clock(naive) == naive, "naive 输入原样（按库内时钟解释）"
    assert (
        agent_audit.parse_audit_time("2026-09-19T10:00:00+08:00", field="since")
        == aware.astimezone().replace(tzinfo=None)
    )


async def test_normalize_run_status_reads_like_a_whitelist():
    """``normalize_run_status``：空值 = 不过滤；域内透传；域外抛中文（**不静默忽略**）。"""
    assert agent_audit.normalize_run_status(None) is None
    assert agent_audit.normalize_run_status("") is None
    for value in agent_audit.RUN_STATUSES:
        assert agent_audit.normalize_run_status(value) == value
    with pytest.raises(ValueError) as excinfo:
        agent_audit.normalize_run_status("bogus")
    message = str(excinfo.value)
    assert "bogus" in message and "running" in message, message


async def test_summarize_runs_dedupes_with_a_stable_tiebreak():
    """``summarize_runs``：同 ``started_at`` 时按 ``id`` 倒序 ⇒ 顺序**确定可断言**。"""
    same = "2026-01-01T00:00:00"
    items = [
        {"run": {"id": 1, "started_at": same}},
        {"run": {"id": 2, "started_at": same}},
        {"run": {"id": 1, "started_at": same}},
        {"run": None},  # 孤儿 step（isouter 连接）：如实给 None，不该在这里炸
        {"no_run_key": 1},
    ]
    assert [run["id"] for run in agent_audit.summarize_runs(items)] == [2, 1]
    assert agent_audit.summarize_runs([]) == []
