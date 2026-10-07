"""条目 1.3.1 单测：source_type → kind / answer_source 映射（统筹 §3.8）。

对照表逐行断言（**四行全覆盖**）：

| 内部 source_type            | kind     | answer_source | refs   |
|-----------------------------|----------|---------------|--------|
| internal_db（命中）          | normal   | kb            | 有引用 |
| internal_db 未命中且允许直答 | direct   | general       | 恒空   |
| 范围空 / 拒答                | refuse   | empty_scope   | 恒空   |
| 澄清                         | clarify  | clarify       | 恒空   |

外加：库外（external）→ direct；停止轮不适用；refs 恒空黑名单；开关关闭的拒答语义。
"""

from __future__ import annotations

import pytest

from app.generation import source_map


# --------------------------------------------------------------------------
# §3.8 映射表逐行
# --------------------------------------------------------------------------


def test_kind_to_answer_source_table():
    """kind → answer_source 四值映射唯一（clarify 行一并覆盖）。"""
    assert source_map.answer_source_for("normal") == "kb"
    assert source_map.answer_source_for("direct") == "general"
    assert source_map.answer_source_for("refuse") == "empty_scope"
    assert source_map.answer_source_for("clarify") == "clarify"


def test_stopped_has_no_answer_source():
    """停止轮不是一次完整回答：不写 answer_source（该列保持 NULL）。"""
    assert source_map.answer_source_for("stopped") is None
    assert source_map.answer_source_for(None) is None
    assert source_map.answer_source_for("") is None
    assert source_map.answer_source_for("bogus") is None


def test_row1_internal_hit_is_normal_kb():
    """第 1 行：库内命中 → normal / kb / 有引用。"""
    kind, answer_source, refs = source_map.map_round(
        "internal_db", has_materials=True, direct_enabled=True, refs=[{"ordinal": 1}]
    )
    assert (kind, answer_source) == ("normal", "kb")
    assert refs == [{"ordinal": 1}], "命中轮必须带出引用"


def test_row2_internal_miss_direct_is_general_empty_refs():
    """第 2 行：库内未命中且允许直答 → direct / general / refs 恒空。"""
    kind, answer_source, refs = source_map.map_round(
        "internal_db", has_materials=False, direct_enabled=True, refs=[{"ordinal": 1}]
    )
    assert (kind, answer_source) == ("direct", "general")
    assert refs == [], "直答轮 refs 必须恒空（即使误传了引用）"


def test_row3_refuse_is_empty_scope_empty_refs():
    """第 3 行：拒答 → refuse / empty_scope / refs 恒空。"""
    kind, answer_source, refs = source_map.map_round(
        "internal_db", has_materials=False, direct_enabled=False, refs=[{"ordinal": 1}]
    )
    assert (kind, answer_source) == ("refuse", "empty_scope")
    assert refs == []


def test_row4_clarify_is_clarify_empty_refs():
    """第 4 行：澄清 → clarify / clarify / refs 恒空（kind 由 1.4 产出，此处验映射）。"""
    assert source_map.answer_source_for("clarify") == "clarify"
    assert source_map.refs_empty_for("clarify") is True


def test_external_always_direct():
    """库外问题恒走 direct（不受「库内未命中」开关影响）。"""
    for enabled in (True, False):
        kind, answer_source, refs = source_map.map_round(
            "external", has_materials=False, direct_enabled=enabled
        )
        assert (kind, answer_source) == ("direct", "general")
        assert refs == []


# --------------------------------------------------------------------------
# refs 恒空黑名单
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["direct", "refuse", "clarify"])
def test_refs_empty_blacklist(kind):
    """direct / refuse / clarify 三类的 refs 恒空（I7 无假引用）。"""
    assert source_map.refs_empty_for(kind) is True


@pytest.mark.parametrize("kind", ["normal", "stopped"])
def test_refs_not_forced_empty(kind):
    """normal 不强制空（有引用）；stopped 不在黑名单（其 refs 由停止路径决定）。"""
    assert source_map.refs_empty_for(kind) is False


def test_refs_never_leak_on_non_normal_kinds():
    """即使调用方误传引用，非 normal 轮也不会漏出去。"""
    for source_type, has_materials, enabled in (
        ("internal_db", False, True),
        ("internal_db", False, False),
        ("external", False, True),
    ):
        _, _, refs = source_map.map_round(
            source_type,
            has_materials=has_materials,
            direct_enabled=enabled,
            refs=[{"ordinal": 1}, {"ordinal": 2}],
        )
        assert refs == []


# --------------------------------------------------------------------------
# resolve_kind 的判定顺序
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_type,has_materials,enabled,expected",
    [
        ("external", False, True, "direct"),
        ("external", False, False, "direct"),
        ("external", True, True, "direct"),
        ("internal_db", True, True, "normal"),
        ("internal_db", True, False, "normal"),
        ("internal_db", False, True, "direct"),
        ("internal_db", False, False, "refuse"),
    ],
)
def test_resolve_kind_matrix(source_type, has_materials, enabled, expected):
    """判定矩阵：开关只管「库内未命中」那一条分支。"""
    assert (
        source_map.resolve_kind(
            source_type, has_materials=has_materials, direct_enabled=enabled
        )
        == expected
    )


def test_answer_source_is_derived_from_kind_only():
    """answer_source 只由 kind 派生——不是模型说、不是前端猜。"""
    for kind, expected in source_map.ANSWER_SOURCE_BY_KIND.items():
        assert source_map.answer_source_for(kind) == expected
    assert set(source_map.ANSWER_SOURCE_BY_KIND) == {"normal", "direct", "refuse", "clarify"}


def test_kind_vocabulary_matches_contract():
    """kind 词汇表与 02 §7.1 一致（含停止轮 stopped）。"""
    assert set(source_map.KINDS) == {"normal", "clarify", "refuse", "direct", "stopped"}
