"""U4.2：引用使用冻结编号、来源身份，fallback 仍仅提供候选资料。"""

from __future__ import annotations

import pytest

from app.generation import citation, source_map
from app.retrieval.types import Material, number, render_materials


def _snapshot() -> list[Material]:
    return number(
        [
            Material(
                chunk_id=100 + index,
                document_id=10 + index,
                block_ordinal=index,
                content=f"主体{index}的金额为{index}万元。",
                filename=f"来源{index}.md",
            )
            for index in range(1, 5)
        ]
    )


def test_reparsing_resolved_refs_keeps_snapshot_ordinals():
    all_refs = citation.refs_from_materials(_snapshot(), "rl-u4-snapshot")
    refs, _ = citation.resolve_citations("金额见 [2]，主体见 [4]。", all_refs)

    resolved, mode = citation.resolve_citations("主体见 [4]。", refs)

    assert mode == "cited"
    assert [ref["ordinal"] for ref in resolved] == [4]
    assert resolved[0]["source_id"] == 14
    assert resolved[0]["retrieval_id"] == "rl-u4-snapshot"


def test_list_order_does_not_reassign_frozen_numbers():
    all_refs = citation.refs_from_materials(_snapshot())
    refs = list(reversed(all_refs))

    resolved, mode = citation.resolve_citations("见 [2]。", refs)

    assert mode == "cited"
    assert [ref["source_id"] for ref in resolved] == [12]
    assert [ref["ordinal"] for ref in resolved] == [2]
    assert refs == list(reversed(all_refs))


def test_number_absent_from_sparse_snapshot_is_not_a_position():
    all_refs = citation.refs_from_materials(_snapshot())
    refs = [all_refs[1], all_refs[3]]

    resolved, mode = citation.resolve_citations("见 [1]。", refs)

    assert mode == "fallback"
    assert [ref["ordinal"] for ref in resolved] == [2, 4]
    assert {ref["citation_mode"] for ref in resolved} == {"fallback"}
    assert {ref["citation_mode"] for ref in refs} == {None}


@pytest.mark.parametrize("ordinal", [None, 0])
def test_legacy_refs_without_frozen_numbers_keep_position_fallback(ordinal):
    refs = [{"title": "旧来源一", "ordinal": ordinal}, {"title": "旧来源二"}]

    resolved, mode = citation.resolve_citations("见 [2]。", refs)

    assert mode == "cited"
    assert resolved == [{"title": "旧来源二", "citation_mode": "cited"}]


def test_unnumbered_material_refs_use_the_same_render_fallback_without_mutation():
    materials = [
        Material(chunk_id=7, document_id=17, content="原文", filename="未编号.md")
    ]
    refs = citation.refs_from_materials(materials)

    assert render_materials(materials).startswith("[1]")
    assert refs[0]["ordinal"] == 1
    assert materials[0].ordinal == 0
    assert citation.cited_materials(materials, refs) == materials
    assert citation.attribution_rows(9, materials, refs)[0]["ordinal"] == 1


def test_ref_from_another_snapshot_cannot_attribute_to_the_same_ordinal():
    materials = _snapshot()
    refs = citation.refs_from_materials(materials, "rl-u4-old")
    foreign = [dict(refs[0], chunk_id=materials[1].chunk_key)]

    assert citation.cited_materials(materials, foreign) == []
    assert citation.attribution_rows(9, materials, foreign, "rl-u4-old") == []


def test_legacy_attribution_without_chunk_identity_still_uses_frozen_number():
    materials = _snapshot()
    refs = [{"ordinal": 3, "title": "旧来源", "citation_mode": "cited"}]

    assert citation.cited_materials(materials, refs) == [materials[2]]
    rows = citation.attribution_rows(9, materials, refs)
    assert rows[0]["chunk_id"] == materials[2].chunk_id


def test_attribution_does_not_replace_a_different_retrieval_snapshot_id():
    materials = _snapshot()
    refs = citation.refs_from_materials(materials, "rl-u4-old")

    assert citation.attribution_rows(9, materials, refs, "rl-u4-new") == []


@pytest.mark.parametrize("kind", ["direct", "refuse", "clarify"])
def test_no_reference_kinds_do_not_reclassify_candidates_as_used_materials(kind):
    materials = _snapshot()

    refs, selected = citation.build_refs(kind, "金额见 [1]。", materials)

    assert refs == selected == []
    assert source_map.refs_empty_for(kind)


def test_fallback_keeps_existing_contract_and_records_only_candidate_mode():
    materials = _snapshot()

    refs, selected = citation.build_refs("normal", "模型没有标注来源。", materials)

    assert [ref["ordinal"] for ref in refs] == [1, 2, 3, 4]
    assert {ref["citation_mode"] for ref in refs} == {"fallback"}
    assert selected == materials
    assert {row["meta"]["citation_mode"] for row in citation.attribution_rows(9, materials, refs)} == {
        "fallback"
    }


def test_a_too_long_out_of_range_marker_does_not_break_answer_closeout():
    refs = citation.refs_from_materials(_snapshot())

    resolved, mode = citation.resolve_citations("见 [" + "9" * 5000 + "]。", refs)

    assert mode == "fallback"
    assert len(resolved) == 4
    for zero, digit in (("0", "4"), ("０", "４")):
        resolved, mode = citation.resolve_citations("见 [" + zero * 5000 + digit + "]。", refs)
        assert mode == "cited"
        assert [ref["ordinal"] for ref in resolved] == [4]
