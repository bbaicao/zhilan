"""专项 F6-A · 工具检索范围求交（`回归修复`）的钉子（A1–A12）。

三块面，**都不连真模型、不连真库**：

1. **共享纯函数** ``app.retrieval.types.intersect_tool_filters``（A1–A9）——
   ⚠️ §2.2 已核实：``filter_kb_ids`` / ``filter_categories`` / ``filter_tags`` /
   ``filter_source_types`` / ``_parse_dt`` 这五个归一器在 ``backend/tests/`` 下
   **零直接用例** ⇒ 本文件的相关断言是**从零建**的，不是「补一个已覆盖的点」；
2. **真 handler**（A10/A11）——冲突与坏值必须在**调用检索之前**拒绝：
   检索调用数为 0、``ok=False``、不伪造一次重排统计；
3. **两条 Agent 链路**（A12）——ReAct 与 P&E 都经**真实注册/执行链**
   （``stream_agent_turn`` → ``build_agent_tools`` → ``bootstrap.build_registry`` →
   ``knowledge_search.build_spec`` → handler）跑，模型用 CubeLoop FauxProvider。

⚠️ 本目录（``tests/agent/``）**没有 conftest.py**，不继承 ``tests/api/conftest.py`` 的
八个 autouse 钉子（向量 / 重排 / 图通道 / 联网 / 记忆 / 缓存 / 辅助模型）。本文件
**不用 DB**：``db`` 传哨兵对象、``chat_id=None`` ⇒ 事件与审计写入都是零 DB 触碰的直通；
检索实现是注入的假件 ⇒ 向量库、重排、图通道一条都不会被走到，因此无需另钉。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime

import pytest

from app.agent.runner import AgentTurn
from app.agent.tools import knowledge_search
from app.agent.tools.knowledge_search import KnowledgeSearchArgs, build_spec
from app.api.chat_schemas import Filters
from app.retrieval.types import intersect_tool_filters, matches_document, matches_note
from tests.agent.test_runner import (  # noqa: F401 —— 复用既有假件，避免同一份助手漂移两份
    CollectingRetrieval,
    RecordingModel,
    collect,
    faux_model,
    material,
    plan_json,
    says,
    tool_call,
)

# --------------------------------------------------------------------------
# 公共夹具：宿主范围 / 一条能被真实假件回收的材料
# --------------------------------------------------------------------------

HOST = {"kb_ids": [1], "source_types": ["note"], "categories": ["allowed"], "tags": ["signed"]}


def _capture():
    """假检索：记录 ``(session, question, scope)``，返回空结果（**只看收到什么范围**）。"""

    class _Capture:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def __call__(self, session, question, scope, *, stats=None):
            self.calls.append((session, question, scope))
            return [], "", None

    return _Capture()


# --------------------------------------------------------------------------
# A1 · 空态不清除宿主限制；双方不限 = 维持「无范围」
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "requested",
    [
        None,
        {},
        {"kb_ids": []},
        {"source_types": []},
        {"categories": []},
        {"tags": []},
        {"kb_ids": None, "created_after": ""},
        {"source_type": "   "},
    ],
    ids=["omitted", "empty-object", "kb-empty", "source-empty", "cat-empty",
         "tags-empty", "null-and-blank", "blank-single"],
)
def test_a1_empty_tool_filters_never_clear_host_scope(requested):
    """A1：省略 / null / ``{}`` / 空列表各态**都不清掉宿主限制**。"""
    assert intersect_tool_filters(HOST, requested) == {
        "kb_ids": [1],
        "source_types": ["note"],
        "categories": ["allowed"],
        "tags": ["signed"],
    }


def test_a1_both_sides_unrestricted_stays_no_scope():
    """A1：双方都不限 ⇒ 返回 ``None``（维持原「无范围」行为，而不是空 dict）。"""
    for host, requested in ((None, None), (None, {}), ({}, None), ({}, {})):
        assert intersect_tool_filters(host, requested) is None


# --------------------------------------------------------------------------
# A2 · kb / source_types / categories 逐维覆盖
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dim", "host_value", "tool_value", "expected"),
    [
        # —— kb：单侧限制 / 包含 / 部分相交 / 完全不相交
        ("kb_ids", None, [3], [3]),
        ("kb_ids", [1], None, [1]),
        ("kb_ids", [1], [1, 2], [1]),
        ("kb_ids", [1, 2], [2, 3], [2]),
        ("kb_ids", [2, 1], [1], [1]),
        # —— source_types：只认可满足的两值（工具侧强校验；宿主侧照旧）
        ("source_types", None, ["document"], ["document"]),
        ("source_types", ["document"], None, ["document"]),
        ("source_types", ["document"], ["document", "note"], ["document"]),
        ("source_types", ["document", "note"], ["note"], ["note"]),
        # —— categories：维度内是 OR
        ("categories", None, ["B"], ["B"]),
        ("categories", ["A"], None, ["A"]),
        ("categories", ["A"], ["A", "B"], ["A"]),
        ("categories", ["A", "B"], ["B", "C"], ["B"]),
    ],
)
def test_a2_finite_dimensions_take_intersection(dim, host_value, tool_value, expected):
    """A2：单侧限制保留该侧、两侧都限制取交（包含 / 部分相交）。"""
    host = {} if host_value is None else {dim: host_value}
    tool = {} if tool_value is None else {dim: tool_value}
    assert intersect_tool_filters(host, tool) == {dim: expected}


@pytest.mark.parametrize(
    ("host", "tool"),
    [
        ({"kb_ids": [1]}, {"kb_ids": [9]}),
        ({"categories": ["A"]}, {"categories": ["B"]}),
        ({"source_types": ["note"]}, {"source_types": ["document"]}),
        ({"kb_id": 1}, {"kb_ids": [2]}),
        ({"source_types": ["web"]}, {"source_types": ["note"]}),
    ],
    ids=["kb", "categories", "source-types", "alias-cross", "unsupported-host-source"],
)
def test_a2_disjoint_sets_are_a_conflict_not_an_empty_scope(host, tool):
    """A2：完全不相交 ⇒ 冲突（抛错），**不是**返回空集后被读成「不限」。"""
    with pytest.raises(ValueError) as excinfo:
        intersect_tool_filters(host, tool)
    assert "没有交集" in str(excinfo.value)


# --------------------------------------------------------------------------
# A3 · 别名交叉写法 / 同侧合并 / 输出移除别名
# --------------------------------------------------------------------------


def test_a3_same_side_aliases_merge_before_intersecting():
    """A3：同一侧的 ``kb_id`` + ``kb_ids`` 先按旧语义合并（``[1,2]``），再与工具侧取交。"""
    assert intersect_tool_filters({"kb_id": 1, "kb_ids": [2]}, {"kb_ids": [2, 3]}) == {
        "kb_ids": [2]
    }
    assert intersect_tool_filters({"kb_ids": [2], "kb_id": 1}, {"kb_id": 2}) == {"kb_ids": [2]}
    # 单侧只有别名写法时，也要先合并（旧语义 = 并集，不是冲突）
    assert intersect_tool_filters({"kb_id": 1, "kb_ids": [2]}, None) == {"kb_ids": [1, 2]}


def test_a3_output_drops_single_value_aliases():
    """A3：成功结果只保留 ``kb_ids`` / ``source_types``，否则下游会把别名再并回来。"""
    merged = intersect_tool_filters({"kb_id": 1, "source_type": "note"}, {"kb_id": 1})
    assert merged == {"kb_ids": [1], "source_types": ["note"]}
    assert "kb_id" not in merged and "source_type" not in merged


def test_a3_public_helper_is_exported():
    """§2.2：``types.py`` 的 ``__all__`` 此前**没有任何测试在钉** ⇒ 补一条最小断言。

    顺带查两种会静默出问题的形态：``__all__`` 里的**悬空名**（写错符号名 → ``from ... import *``
    报错）与**重复名**。
    """
    from app.retrieval import types as retrieval_types

    assert "intersect_tool_filters" in retrieval_types.__all__
    assert set(retrieval_types.__all__) <= set(dir(retrieval_types))
    assert len(retrieval_types.__all__) == len(set(retrieval_types.__all__))


# --------------------------------------------------------------------------
# A4 · tags 取并集并保持 AND
# --------------------------------------------------------------------------


def test_a4_tags_are_unioned_and_still_all_required():
    """A4：宿主 ``[a,b]`` + 工具 ``[a,c]`` ⇒ 要求 a/b/c；只有 a 的资料不匹配。"""
    merged = intersect_tool_filters({"tags": ["a", "b"]}, {"tags": ["a", "c"]})
    assert merged == {"tags": ["a", "b", "c"]}

    def row(tags):
        return dict(kb_id=1, category="", tags=tags, created_at=datetime(2026, 3, 1))

    assert matches_document(merged, **row(["a"])) is False  # 只具备 a ⇒ 不满足
    assert matches_document(merged, **row(["a", "b"])) is False
    assert matches_document(merged, **row(["a", "b", "c"])) is True
    # 反向对照：工具侧的标签若被**交集**吃掉，这一条就会假绿
    assert matches_document({"tags": ["a"]}, **row(["a"])) is True


# --------------------------------------------------------------------------
# A5 · 日期取窄 / 时区等价 / 端点相等 / 相反区间
# --------------------------------------------------------------------------


def test_a5_dates_take_the_narrower_side():
    """A5：``created_after`` 取更晚、``created_before`` 取更早。"""
    merged = intersect_tool_filters(
        {"created_after": "2026-01-01", "created_before": "2026-12-31"},
        {"created_after": "2026-06-01", "created_before": "2026-09-01"},
    )
    assert merged == {"created_after": "2026-06-01T00:00:00", "created_before": "2026-09-01T00:00:00"}


def test_a5_offsets_compare_as_the_same_instant():
    """A5：``Z`` 与 ``+08:00`` 正规化后是同一时刻 ⇒ 取窄结果就是那个时刻。"""
    merged = intersect_tool_filters(
        {"created_after": "2026-01-01T08:00:00+08:00"},
        {"created_after": "2026-01-01T00:00:00Z"},
    )
    assert merged == {"created_after": "2026-01-01T00:00:00"}


def test_a5_equal_endpoints_are_legal():
    """A5：起点**等于**终点合法（零宽区间仍然是一次真检索）。"""
    merged = intersect_tool_filters(
        {"created_after": "2026-05-01"}, {"created_before": "2026-05-01"}
    )
    assert merged == {"created_after": "2026-05-01T00:00:00", "created_before": "2026-05-01T00:00:00"}


def test_a5_inverted_range_is_a_conflict():
    """A5：合并后起点晚于终点 ⇒ 冲突。"""
    with pytest.raises(ValueError) as excinfo:
        intersect_tool_filters({"created_after": "2026-09-01"}, {"created_before": "2026-01-01"})
    assert "晚于结束时间" in str(excinfo.value)


# --------------------------------------------------------------------------
# A6 · 坏值一律报错（不静默归一为「不限」）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "requested",
    [
        {"kb_ids": [True]},  # bool 是 int 子类（实测 filter_kb_ids → [1]）
        {"kb_ids": [1.5]},
        {"kb_ids": ["1"]},
        {"kb_ids": [{"id": 1}]},
        {"kb_ids": [1, None]},
        {"kb_id": False},
        {"categories": [1]},
        {"tags": ["ok", ""]},
        {"tags": [None]},
        {"source_types": ["web"]},
        {"source_type": "DOC"},
        {"created_after": "not-a-date"},
        {"created_after": 20260919},  # _parse_dt 走 str() ⇒ 归一器**不会**报错
        {"created_before": ["2026-01-01"]},
        {"kb_idz": [1]},  # 拼错的键不能被忽略（忽略 = 放宽）
        "kb_ids=1",
    ],
    ids=["bool", "float", "str-int", "dict", "null-in-list", "bool-single", "cat-int",
         "blank-tag", "none-tag", "unknown-source", "upper-source", "bad-date", "int-date",
         "list-date", "typo-key", "not-a-dict"],
)
def test_a6_bad_tool_values_are_rejected(requested):
    """A6：非法 id / bool / 小数 / 混合类型 / 坏日期 / 未知键与来源**都不被吞掉**。"""
    with pytest.raises(ValueError):
        intersect_tool_filters(HOST, requested)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested",
    [{"kb_ids": [True]}, {"created_after": "not-a-date"}, {"kb_ids": [9]}, {"kb_idz": [1]}],
)
async def test_a6_bad_values_reach_the_model_and_never_touch_retrieval(requested):
    """A6：坏值 / 冲突最终变成 ``ok=False`` 的中文观察，且**检索调用数为 0**。"""
    retrieve = _capture()
    spec = build_spec(db=object(), filters=dict(HOST), retrieve=retrieve)
    obs = await spec.handler(KnowledgeSearchArgs(query="q", filters=requested))
    assert obs.ok is False
    assert "知识库检索未执行" in obs.text
    assert retrieve.calls == []  # 没有越界查询，也没有「正常无命中」的伪装
    assert obs.retrieval_stats is None  # 不伪造一次成功重排统计


# --------------------------------------------------------------------------
# A7 · 真冲突不得被编码成「不限」
# --------------------------------------------------------------------------


def test_a7_http_model_keeps_the_constraint_and_conflict_stays_an_error():
    """A7：冲突既不来自 ``to_stored()`` 的丢字段，也不能被归一化磨成「不限」。"""
    stored = Filters(kb_ids=[1]).to_stored()
    assert stored == {"kb_ids": [1]}  # 宿主限制真的带进来了
    with pytest.raises(ValueError):
        intersect_tool_filters(stored, {"kb_ids": [9]})
    # 反面对照：**空列表**才是「不限」（既有 HTTP 宽松口径，本项不改成 422）
    assert Filters(kb_ids=[]).to_stored() is None


@pytest.mark.asyncio
async def test_a7_conflict_is_not_disguised_as_empty_or_fake_ids():
    """A7：不给伪造 id / ``[]`` 假装拒绝——冲突就是不检索。"""
    retrieve = _capture()
    spec = build_spec(db=object(), filters={"kb_ids": [1]}, retrieve=retrieve)
    obs = await spec.handler(KnowledgeSearchArgs(query="q", filters={"kb_ids": [9]}))
    assert obs.ok is False
    assert retrieve.calls == []
    assert obs.materials == []
    assert "知识库" in obs.text  # 点明冲突维度，可读


# --------------------------------------------------------------------------
# A8 · 输入不可变 + 同轮多次调用互不串用
# --------------------------------------------------------------------------


def test_a8_inputs_are_never_mutated():
    """A8：输入字典与其中的列表逐字不变（深拷贝比对）。"""
    host = {"kb_id": 1, "kb_ids": [2], "tags": ["a"], "categories": ["A"]}
    requested = {"kb_ids": [2, 3], "tags": ["b"], "created_after": "2026-01-01"}
    host_before, requested_before = deepcopy(host), deepcopy(requested)
    intersect_tool_filters(host, requested)
    assert host == host_before
    assert requested == requested_before


@pytest.mark.asyncio
async def test_a8_no_cross_contamination_between_calls_in_one_turn():
    """A8：同轮连续「坏 → 好」「窄 → 不同窄」都不串用。"""
    retrieve = _capture()
    spec = build_spec(db=object(), filters={"kb_ids": [1, 2]}, retrieve=retrieve)

    bad = await spec.handler(KnowledgeSearchArgs(query="坏", filters={"kb_ids": [9]}))
    good = await spec.handler(KnowledgeSearchArgs(query="好", filters={"kb_ids": [2, 3]}))
    other = await spec.handler(KnowledgeSearchArgs(query="另一窄", filters={"kb_ids": [1]}))

    assert bad.ok is False
    assert good.ok is True and other.ok is True
    assert [call[2] for call in retrieve.calls] == [{"kb_ids": [2]}, {"kb_ids": [1]}]


# --------------------------------------------------------------------------
# A9 · 合并匹配 = 宿主匹配 AND 工具匹配（有限笛卡尔样本）
# --------------------------------------------------------------------------

_ROWS = (
    {"kb_id": 1, "category": "allowed", "tags": ["signed"], "created_at": datetime(2026, 3, 1)},
    {"kb_id": 1, "category": "outside", "tags": [], "created_at": datetime(2026, 3, 1)},
    {"kb_id": 2, "category": "allowed", "tags": ["signed", "review"], "created_at": datetime(2025, 1, 1)},
    {"kb_id": 2, "category": "allowed", "tags": ["signed"], "created_at": datetime(2026, 6, 1)},
)

_SCOPES = (
    None,
    {},
    {"kb_ids": [1]},
    {"kb_ids": [2]},
    {"kb_ids": [1, 2]},
    {"categories": ["allowed"]},
    {"tags": ["signed"]},
    {"tags": ["signed", "review"]},
    {"created_after": "2026-01-01"},
    {"created_before": "2026-06-01"},
    {"source_types": ["document"]},
    {"kb_ids": [1], "tags": ["signed"], "created_after": "2026-01-01"},
)


@pytest.mark.parametrize("matcher", [matches_document, matches_note], ids=["document", "note"])
def test_a9_merged_scope_equals_the_conjunction_of_both_sides(matcher):
    """A9：合并后的匹配 = 宿主匹配 AND 工具匹配；冲突视为恒 false。

    ⚠️ **样本必须是合法真实行形状**（非空 ``kb_id`` 与 ``created_at``）——空值会让
    ``matches_*`` 走「该维度跳过」的分支，那样测出来的是另一件事。
    """
    checked = 0
    for host in _SCOPES:
        for tool in _SCOPES:
            try:
                merged = intersect_tool_filters(host, tool)
            except ValueError:
                # 冲突 ⇒ 恒 false：真正满足宿主**且**满足工具的行必须**不存在**
                for row in _ROWS:
                    assert not (matcher(host, **row) and matcher(tool, **row)), (host, tool, row)
                continue
            for row in _ROWS:
                assert matcher(merged, **row) == (
                    matcher(host, **row) and matcher(tool, **row)
                ), (host, tool, merged, row)
                checked += 1
    assert checked > 0  # 样本真的跑到了（防止参数化被写空）


# --------------------------------------------------------------------------
# A10 · 真 handler 的行为（检索前拒绝 / 正控取交 / limit / warning / stats）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a10_conflict_is_refused_before_any_retrieval():
    """A10：宿主 ``[1]`` + 工具 ``[9]`` ⇒ **不执行 retrieve**。"""
    retrieve = _capture()
    spec = build_spec(db=object(), filters={"kb_ids": [1]}, retrieve=retrieve)
    obs = await spec.handler(KnowledgeSearchArgs(query="越界", filters={"kb_ids": [9]}))
    assert obs.ok is False
    assert retrieve.calls == []
    assert obs.retrieval_stats is None


@pytest.mark.asyncio
async def test_a10_positive_control_receives_the_intersection():
    """A10：正控 ``[1,2]`` + ``[2,3]`` ⇒ 实收 ``[2]``；``limit`` / warning / stats 仍正常。"""
    seen: list[dict] = []

    async def fake(session, question, scope, *, stats=None):
        seen.append(scope)
        return [material(1), material(2)], "", "向量路降级"

    spec = build_spec(db=object(), filters={"kb_ids": [1, 2]}, retrieve=fake)
    obs = await spec.handler(
        KnowledgeSearchArgs(query="合法", filters={"kb_ids": [2, 3]}, limit=1)
    )
    assert seen == [{"kb_ids": [2]}]
    assert obs.ok is True
    assert len(obs.materials) == 1  # limit 仍然生效
    assert obs.warnings == ["向量路降级"]
    assert obs.retrieval_stats is not None


@pytest.mark.asyncio
async def test_a10_tool_filters_absent_keeps_the_host_scope_verbatim_when_canonical():
    """A10：工具不给 filters ⇒ 宿主范围照旧，且**宿主写法被规范化**后再下传。

    ⚠️ 宿主这里刻意用**别名形态**（``kb_id`` / ``source_type``）：若用已经是规范形的
    ``{"kb_ids": [7]}``，这条断言在「handler 在这一分支绕过求交、直接把宿主 dict 传下去」
    时**照样绿**（无鉴别力）——它同时钉住「这一分支真的过求交」与「规范形已生效」。
    """
    retrieve = _capture()
    spec = build_spec(db=object(), filters={"kb_id": 7, "source_type": "note"}, retrieve=retrieve)
    obs = await spec.handler(KnowledgeSearchArgs(query="蓝鲸"))
    assert obs.ok is True
    assert [call[2] for call in retrieve.calls] == [{"kb_ids": [7], "source_types": ["note"]}]


@pytest.mark.asyncio
async def test_a5_host_side_inverted_range_is_refused_with_zero_retrieval():
    """A5 后半句 + §2.3：**宿主自带**反向区间（模型一个 filters 都不给）也判冲突、零检索。

    这是本项对**宿主侧**唯一一处可观测行为变化（改前该请求是「正常检索、0 命中」）：
    D4 要求不可满足的范围**在检索前拒绝**，不能编码成空集冒充「无命中」。非 agent 路径
    仍是旧口径（本项只改工具信任边界），两条链路对该退化输入分路不同——**有意**，已写进
    :func:`app.retrieval.types.intersect_tool_filters` 的 docstring。
    """
    retrieve = _capture()
    spec = build_spec(
        db=object(),
        filters={"created_after": "2026-09-01", "created_before": "2026-01-01"},
        retrieve=retrieve,
    )
    obs = await spec.handler(KnowledgeSearchArgs(query="蓝鲸"))
    assert obs.ok is False
    assert "晚于结束时间" in obs.text
    assert retrieve.calls == []
    assert obs.retrieval_stats is None


def test_a5_host_side_bad_date_keeps_the_old_lenient_behaviour():
    """§2.4：**宿主侧**坏日期沿用旧宽松口径（忽略该维度）——本项不得偷偷变成 HTTP 422。"""
    # 宿主单独坏日期 ⇒ 该维度被忽略；工具也坏日期时**工具侧**仍然报错（两条口径不混）
    assert intersect_tool_filters({"created_after": "not-a-date"}, None) is None
    assert intersect_tool_filters({"created_after": "not-a-date"}, {"kb_ids": [1]}) == {
        "kb_ids": [1]
    }
    with pytest.raises(ValueError):
        intersect_tool_filters({}, {"created_after": "not-a-date"})


@pytest.mark.asyncio
async def test_a5_host_side_bad_date_still_retrieves():
    """§2.4 正控：宿主坏日期**不拦检索**（改前怎么走，现在就怎么走）。"""
    retrieve = _capture()
    spec = build_spec(db=object(), filters={"created_after": "坏日期"}, retrieve=retrieve)
    obs = await spec.handler(KnowledgeSearchArgs(query="蓝鲸"))
    assert obs.ok is True
    assert [call[2] for call in retrieve.calls] == [None]


# --------------------------------------------------------------------------
# A12 · 两条 Agent 链路都经真实注册/执行链
# --------------------------------------------------------------------------


def _scope_calls(query_bad: str, query_good: str) -> tuple[dict, dict]:
    return (
        {"query": query_bad, "filters": {"kb_ids": [9]}},  # 越界
        {"query": query_good, "filters": {"kb_ids": [2, 3]}},  # 合法收窄
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["react", "plan_execute"])
async def test_a12_both_agent_modes_refuse_out_of_scope_and_accept_the_next_call(mode):
    """A12：ReAct / P&E 都走真注册链 —— 冲突观察 ``ok=False``、无越界材料、随后合法调用成功。

    ⚠️ 这里**不注入 registry**：``build_agent_tools`` 会用 ``bootstrap.build_registry``
    现建一份（``db`` / ``filters`` 闭合进 handler），也就是生产路径那一处装配点。
    """
    bad, good = _scope_calls("越界", "合法")
    retrieval = CollectingRetrieval([[material(1, filename="合法.md")]])
    host = {**HOST, "kb_ids": [1, 2]}  # 宿主含 kb 2 ⇒ 工具那次合法收窄才成立
    if mode == "react":
        model = faux_model([tool_call("knowledge_search", bad), tool_call("knowledge_search", good), says("可以作答了")])
    else:
        model = RecordingModel(
            [plan_json({"goal": "越界", "tool": "knowledge_search", "args": bad},
                       {"goal": "合法", "tool": "knowledge_search", "args": good})]
        )

    turn = AgentTurn(mode=mode)
    frames = await collect(
        turn,
        question="范围问题",
        model=model,
        db=object(),  # 非 None ⇒ handler 不早退；chat_id=None ⇒ 事件/审计零 DB 触碰
        filters=dict(host),
        retrieve=retrieval,
    )

    observations = [f for f in frames if f["kind"] == "observation"]
    assert len(observations) == 2
    assert observations[0]["ok"] is False
    assert "知识库检索未执行" in observations[0]["detail"]
    assert observations[1]["ok"] is True
    # 越界那次**根本没发出去**；合法那次收到的是**取交结果**
    assert [call[1] for call in retrieval.calls] == ["合法"]
    assert retrieval.calls[0][2] == {
        "kb_ids": [2],
        "source_types": ["note"],
        "categories": ["allowed"],
        "tags": ["signed"],
    }
    assert [m.filename for m in turn.materials] == ["合法.md"]
