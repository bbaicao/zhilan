"""专项 · 工具调用能力第 0 批 · ③ 范围求交泛化与属性测试（T0-A14 / A15 的后半）。

**为什么单独一个文件**：F6-A 的钉子 ``tests/agent/test_knowledge_scope_f6.py`` 在本批
**只许重跑、不许改判据**（开工包 A15 的复核更正）——它的收集数必须与改前**逐字相同**
（改前实测 **69** 个收集节点）。于是本批新增的用例**一律落这里**。

三块面（都不连真模型、不连真库）：

1. **属性测试**：对任意记录 ``x``，「合并后匹配 == 宿主条件匹配 ∧ 请求侧条件匹配」
   ——照抄 F6 已有的**确定性笛卡尔采样**写法（``_ROWS`` × ``_SCOPES``），
   ⚠️ **不用 hypothesis**（仓里没有，且开工包 §0.3 禁新增依赖）；
2. **泛化入口的形状**：``intersect_scope_filters`` 是**唯一实现**，``intersect_tool_filters``
   是它的**薄壳**——两个名字必须对同一输入给出**同一答案**（不是两份逻辑）；
3. **边界样本逐条**（D16 的表就是逐字段判据）：标签取并集、列表交集为空、单值别名、
   日期坏值、区间反向。

⚠️ 本目录（``tests/agent/``）**没有 conftest.py**，不继承 ``tests/api/conftest.py`` 的
八个 autouse 钉子。本文件**不用 DB、不用模型**：纯函数级断言 + ``build_spec`` 的假检索注入
⇒ 向量库 / 重排 / 图通道一条都不会被走到，因此无需另钉。
"""

from __future__ import annotations

import inspect
from copy import deepcopy
from datetime import datetime

import pytest

from app.agent.tools.knowledge_search import KnowledgeSearchArgs, build_spec
from app.retrieval import types as retrieval_types
from app.retrieval.types import (
    intersect_scope_filters,
    intersect_tool_filters,
    matches_document,
    matches_note,
)

# --------------------------------------------------------------------------
# 公共样本：宿主范围 / 记录行（与 F6 的样本**同形**，便于对照）
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
# 1 · 泛化入口的形状（唯一实现 + 薄壳同答案）
# --------------------------------------------------------------------------


def test_t0_scope_filters_is_exported_and_distinct_name():
    """D15②：广义入口必须进 ``__all__``（第 2 批的本地资料工具要 import 它）。"""
    assert "intersect_scope_filters" in retrieval_types.__all__
    assert set(retrieval_types.__all__) <= set(dir(retrieval_types))
    assert len(retrieval_types.__all__) == len(set(retrieval_types.__all__))


def test_t0_tool_filters_is_a_thin_shell_over_the_scope_filters():
    """D15①：两个名字是**同一份实现**——壳不做任何加工（签名逐字相同）。

    鉴别力：若有人把壳写成「另一套逻辑」（比如把 tags 改成取交），下面的同答案断言会红；
    若有人给壳加了参数或改了入参序，签名断言会红。
    签名**必须**是 ``(host_filters, requested_filters)`` 两个位置参数、关键字名逐字不变
    （F6-A 的钉子按位置传参，看不出入参序——这里补一条按**关键字名**调的判据）。
    """
    shell_sig = inspect.signature(intersect_tool_filters)
    real_sig = inspect.signature(intersect_scope_filters)
    assert list(shell_sig.parameters) == ["host_filters", "requested_filters"]
    assert list(real_sig.parameters) == ["host_filters", "requested_filters"]

    samples = (
        (HOST, {"tags": ["a", "c"]}),
        (HOST, {"kb_ids": [1, 2]}),
        (HOST, None),
        (None, HOST),
        (None, None),
        ({"kb_id": 1}, {"kb_id": 1}),
        (HOST, {"created_after": "2026-01-01"}),
    )
    for host, requested in samples:
        assert intersect_tool_filters(host, requested) == intersect_scope_filters(
            host, requested
        ), (host, requested)


def test_t0_scope_entry_accepts_keyword_arguments_by_the_same_names():
    """两个名字都按**关键字**调用同一个实现（入参序不是唯一口径）。"""
    for fn in (intersect_scope_filters, intersect_tool_filters):
        assert fn(host_filters=HOST, requested_filters=None) == {
            "kb_ids": [1],
            "source_types": ["note"],
            "categories": ["allowed"],
            "tags": ["signed"],
        }


def test_t0_production_handler_goes_through_the_generalized_entry(monkeypatch):
    """D15①：生产 handler 必须走**泛化后的入口**（否则「唯一实现」只是名字上的）。

    判据 = 打桩计数：替换 ``knowledge_search`` 模块里那个符号，handler 跑一次必须命中一次。
    （打的是模块命名空间的绑定，不是 `retrieval.types` 里的定义——handler 用的是前者的局部名。）
    """
    from app.agent.tools import knowledge_search as ks

    seen: list[tuple] = []
    real = ks.intersect_scope_filters

    def spy(host, requested):
        seen.append((host, requested))
        return real(host, requested)

    monkeypatch.setattr(ks, "intersect_scope_filters", spy)
    retrieve = _capture()
    spec = build_spec(db=object(), filters={"kb_ids": [1, 2]}, retrieve=retrieve)

    import asyncio

    obs = asyncio.run(spec.handler(KnowledgeSearchArgs(query="q", filters={"kb_ids": [2, 3]})))
    assert obs.ok is True
    assert seen == [({"kb_ids": [1, 2]}, {"kb_ids": [2, 3]})]
    assert [call[2] for call in retrieve.calls] == [{"kb_ids": [2]}]


def test_t0_generalized_entry_is_directly_usable_by_a_local_material_tool():
    """D15①的「将来的本地资料工具可复用」：**不经过 knowledge_search** 也能直接用。

    模拟第 2 批 ``list_documents`` 的用法：拿宿主范围 + 工具参数 → 拿规范范围 → 下推检索。
    """
    scope = intersect_scope_filters(
        {"kb_ids": [1, 2], "tags": ["signed"]}, {"kb_ids": [2], "categories": ["allowed"]}
    )
    assert scope == {"kb_ids": [2], "tags": ["signed"], "categories": ["allowed"]}
    # 同一处实现也负责「不可满足」的拒绝（不得编码成 []）
    with pytest.raises(ValueError):
        intersect_scope_filters({"kb_ids": [1]}, {"kb_ids": [9]})


# --------------------------------------------------------------------------
# 2 · 属性测试：合并后匹配 == 宿主匹配 ∧ 请求侧匹配（确定性笛卡尔采样）
# --------------------------------------------------------------------------

#: 记录样本：**必须是合法真实行形状**（非空 ``kb_id`` 与 ``created_at``）——空值会让
#: ``matches_*`` 走「该维度跳过」的分支，那样测出来的是另一件事（F6 A9 的同款纪律）。
_ROWS = (
    {"kb_id": 1, "category": "allowed", "tags": ["signed"], "created_at": datetime(2026, 3, 1)},
    {"kb_id": 1, "category": "outside", "tags": [], "created_at": datetime(2026, 3, 1)},
    {"kb_id": 2, "category": "allowed", "tags": ["signed", "review"], "created_at": datetime(2025, 1, 1)},
    {"kb_id": 2, "category": "allowed", "tags": ["signed"], "created_at": datetime(2026, 6, 1)},
    {"kb_id": 3, "category": "", "tags": ["signed", "review"], "created_at": datetime(2026, 1, 15)},
)

#: 范围样本：**覆盖 D16 的五类边界**（本批 A14 明文要求）——
#: 标签不同 / 列表交集为空 / 单值别名 / 日期坏值 / 区间反向，另有「两侧都不限」与组合式。
_SCOPES = (
    None,
    {},
    # —— 单值/多值别名
    {"kb_ids": [1]},
    {"kb_id": 1},
    {"kb_id": 2},
    {"kb_ids": [1, 2]},
    {"kb_ids": [3]},
    {"source_types": ["document"]},
    {"source_type": "note"},
    {"categories": ["allowed"]},
    {"categories": ["outside"]},
    # —— 标签：不同标签（并集语义的判据就是这一格）
    {"tags": ["signed"]},
    {"tags": ["review"]},
    {"tags": ["signed", "review"]},
    {"tags": ["absent"]},
    # —— 日期：正常两侧 / 端点相等 / **坏值** / **反向区间**
    {"created_after": "2026-01-01"},
    {"created_before": "2026-06-01"},
    {"created_after": "2026-06-01", "created_before": "2026-06-01"},
    {"created_after": "not-a-date"},  # 宿主机侧：宽松口径 ⇒ 该维度被忽略
    {"created_before": "2026-09-01", "created_after": "2026-01-01"},
    # —— 组合
    {"kb_ids": [1], "tags": ["signed"], "created_after": "2026-01-01"},
    {"kb_id": 1, "source_types": ["note"], "tags": ["review"]},
)


def _host_side(scope: dict | None) -> dict | None:
    """宿主侧的「有效范围」：坏日期按既有宽松口径被忽略 ⇒ 归一后再作为左操作数。"""
    if scope is None:
        return None
    data = {k: v for k, v in scope.items() if k != "created_after" or v != "not-a-date"}
    return data or None


def _tool_side(scope: dict | None) -> dict | None:
    """请求侧的「有效范围」：坏日期在信任边界上会被**拒绝**（不参与匹配语义）。"""
    if scope is None:
        return None
    if "created_after" in scope and scope["created_after"] == "not-a-date":
        return None  # 该侧恒被拒绝 ⇒ 用「无限制」当右操作数不成立，见下面的 skip 分支
    return scope


@pytest.mark.parametrize("matcher", [matches_document, matches_note], ids=["document", "note"])
def test_t0_a14_merged_scope_equals_the_conjunction_of_both_sides(matcher):
    """A14：``合并后匹配 == 宿主 ∧ 请求侧``；不可满足（冲突）视为**恒 false**。

    ⚠️ 两侧的样本空间并**不对称**，这是有意的（D16 的两种归一口径）：
    宿主侧的坏日期沿用旧宽松口径（该维度被忽略 ⇒ 等价于「不限」），
    请求侧（信任边界）的坏日期**一律报错**——那种输入不构成「一个范围」，
    故整格跳过并在下面单独判它的拒绝行为（``test_t0_a14_bad_values...``）。
    """
    checked = 0
    skipped = 0
    for host in _SCOPES:
        for tool in _SCOPES:
            if _tool_side(tool) is None and tool is not None and tool != {}:
                # 请求侧样本本身不合法（坏日期）⇒ 它的语义是「拒绝」，不是「一个范围」
                skipped += 1
                continue
            effective_host = _host_side(host)
            try:
                merged = intersect_scope_filters(host, tool)
            except ValueError:
                # 冲突 ⇒ **恒 false**：真正同时满足两侧的行必须不存在
                for row in _ROWS:
                    assert not (
                        matcher(effective_host, **row) and matcher(tool, **row)
                    ), (host, tool, row)
                checked += 1
                continue
            for row in _ROWS:
                assert matcher(merged, **row) == bool(
                    matcher(effective_host, **row) and matcher(tool, **row)
                ), (host, tool, merged, row)
                checked += 1
    assert checked > 200  # 样本真的跑到了（防止参数化被写空）
    assert skipped == len(_SCOPES)  # 恰好一列「坏日期」样本被跳过


def test_t0_a14_merged_never_widens_the_host_scope():
    """A14 的方向性（比「等价」更朴素、也更能抓住放宽）：合并后**不可能**多匹配一行。

    「放宽」本项最怕的失效方向（tags 取交就是典型），所以单独留一条**方向断言**：
    对任意 host 与任意合法 tool，合并结果匹配的行**必须**是宿主匹配行的子集。
    """
    for host in _SCOPES:
        effective_host = _host_side(host)
        for tool in _SCOPES:
            if _tool_side(tool) is None and tool is not None and tool != {}:
                continue
            try:
                merged = intersect_scope_filters(host, tool)
            except ValueError:
                continue  # 冲突 ⇒ 什么都不匹配，方向性自动成立（不检索）
            for row in _ROWS:
                if matches_document(merged, **row):
                    assert matches_document(effective_host, **row), (host, tool, merged, row)


def test_t0_a14_output_is_canonical_and_inputs_are_never_mutated():
    """A14 附：合并结果是**规范形**（无单值别名）且**输入逐字不变**（深拷贝比对）。"""
    host = {"kb_id": 1, "source_type": "note", "tags": ["a"]}
    requested = {"kb_ids": [1], "source_types": ["note"], "tags": ["b"]}
    host_before, requested_before = deepcopy(host), deepcopy(requested)
    merged = intersect_scope_filters(host, requested)
    assert merged == {"kb_ids": [1], "source_types": ["note"], "tags": ["a", "b"]}
    assert "kb_id" not in merged and "source_type" not in merged
    assert host == host_before and requested == requested_before


# --------------------------------------------------------------------------
# 3 · D16 的逐类边界（每条都点名它对应哪一类，便于逐条对账）
# --------------------------------------------------------------------------


def test_t0_boundary_tags_take_union_and_stay_and():
    """边界①「标签不同」：宿主 ``[a,b]`` + 请求 ``[a,c]`` ⇒ 要求 a/b/c（并集仍逐标签 AND）。

    ⚠️ 反向鉴别：若实现成 tags 取**交**，``merged`` 会变成 ``["a"]`` ⇒ 下面第二条断言红。
    """
    merged = intersect_scope_filters({"tags": ["a", "b"]}, {"tags": ["a", "c"]})
    assert merged == {"tags": ["a", "b", "c"]}

    def row(tags):
        return dict(kb_id=1, category="", tags=tags, created_at=datetime(2026, 3, 1))

    assert matches_document(merged, **row(["a", "b", "c"])) is True
    assert matches_document(merged, **row(["a"])) is False  # 只具备 a ⇒ 不满足（AND 未破）
    assert matches_document(merged, **row(["a", "b"])) is False


def test_t0_boundary_empty_intersection_raises_instead_of_empty_list():
    """边界②「列表交集为空」：抛 ``ValueError``，**不是** ``[]`` / ``{}`` / ``None``。

    为什么这条必须单列：``Filters.to_stored()`` 会把空列表**删掉**、检索侧把空列表读成
    「不限」⇒ 把冲突编码成 ``[]`` 等于把「一个都不允许」悄悄变成「全库允许」（放宽）。
    """
    with pytest.raises(ValueError) as excinfo:
        intersect_scope_filters({"kb_ids": [1]}, {"kb_ids": [9]})
    assert "没有交集" in str(excinfo.value)

    with pytest.raises(ValueError) as excinfo:
        intersect_scope_filters({"categories": ["A"]}, {"categories": ["B"]})
    assert "分类" in str(excinfo.value)


def test_t0_boundary_single_value_aliases_merge_then_intersect():
    """边界③「单值别名」：同侧 ``kb_id`` + ``kb_ids`` 先按旧语义合并，再与另一侧取交。"""
    assert intersect_scope_filters({"kb_id": 1, "kb_ids": [2]}, {"kb_ids": [2, 3]}) == {
        "kb_ids": [2]
    }
    assert intersect_scope_filters({"kb_id": 1, "kb_ids": [2]}, None) == {"kb_ids": [1, 2]}
    assert intersect_scope_filters(None, {"source_type": "note"}) == {"source_types": ["note"]}
    assert intersect_scope_filters(
        {"source_types": ["document", "note"]}, {"source_type": "note"}
    ) == {"source_types": ["note"]}


def test_t0_boundary_bad_dates_are_rejected_on_the_trust_boundary():
    """边界④「日期坏值」：请求侧坏日期**一律报错**（不得被 ``_parse_dt`` 静默吞成不限）。"""
    for bad in ("not-a-date", 20260919, ["2026-01-01"]):
        with pytest.raises(ValueError):
            intersect_scope_filters({}, {"created_after": bad})
    # ⚠️ **空白串是「没给」而不是坏值**（与单值别名的既有权口径一致：``kb_id: ""`` 同义）
    # ⇒ 它不该报错，也不该产出任何范围（「没给」≠「给了一个非法值」）。
    assert intersect_scope_filters({}, {"created_after": ""}) is None
    assert intersect_scope_filters({}, {"created_after": "   "}) is None
    # 反向对照：**宿主侧**坏日期仍是旧宽松口径（该维度被忽略）——两条口径不混
    assert intersect_scope_filters({"created_after": "not-a-date"}, None) is None
    assert intersect_scope_filters({"created_after": "not-a-date"}, {"kb_ids": [1]}) == {
        "kb_ids": [1]
    }


def test_t0_boundary_inverted_range_is_a_conflict_but_equal_endpoints_are_legal():
    """边界⑤「区间反向」：起点 **>** 终点 ⇒ 冲突；起点 **==** 终点合法（零宽区间仍是一次真检索）。"""
    with pytest.raises(ValueError) as excinfo:
        intersect_scope_filters(
            {"created_after": "2026-09-01"}, {"created_before": "2026-01-01"}
        )
    assert "晚于结束时间" in str(excinfo.value)

    equal = intersect_scope_filters({"created_after": "2026-05-01"}, {"created_before": "2026-05-01"})
    assert equal == {
        "created_after": "2026-05-01T00:00:00",
        "created_before": "2026-05-01T00:00:00",
    }


def test_t0_boundary_dates_take_the_narrower_side_and_normalize_offsets():
    """边界⑤附：日期取**更窄**者；带偏移与 ``Z`` 折成同一 instant 后比较。"""
    assert intersect_scope_filters(
        {"created_after": "2026-01-01", "created_before": "2026-12-31"},
        {"created_after": "2026-06-01", "created_before": "2026-09-01"},
    ) == {"created_after": "2026-06-01T00:00:00", "created_before": "2026-09-01T00:00:00"}

    assert intersect_scope_filters(
        {"created_after": "2026-01-01T08:00:00+08:00"}, {"created_after": "2026-01-01T00:00:00Z"}
    ) == {"created_after": "2026-01-01T00:00:00"}


def test_t0_boundary_unknown_keys_are_rejected_not_ignored():
    """边界附：拼错的键**不得**被忽略（忽略 = 少一层限制 = 放宽）。"""
    with pytest.raises(ValueError) as excinfo:
        intersect_scope_filters({}, {"kb_idz": [1]})
    assert "不认识的键" in str(excinfo.value)


def test_t0_both_sides_unrestricted_returns_none():
    """两侧都不限 ⇒ ``None``（维持「无范围」原行为，而不是空 dict）。"""
    for host, requested in ((None, None), (None, {}), ({}, None), ({}, {})):
        assert intersect_scope_filters(host, requested) is None
