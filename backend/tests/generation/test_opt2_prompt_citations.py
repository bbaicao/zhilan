"""OPT2：空正文不带来源，候选与跨轮编号保留各自真实身份。"""

from copy import deepcopy

import pytest

from app.generation import citation
from app.observe.citation import classify_citations
from app.retrieval.types import Material, number


def _document(source_id: int) -> Material:
    return Material(
        chunk_id=100 + source_id,
        document_id=source_id,
        block_ordinal=1,
        filename=f"来源{source_id}.md",
        content=f"项目预算为{source_id}万元。",
    )


@pytest.mark.parametrize("answer", ["", " \t\r\n", "\u3000\n"])
def test_blank_answer_cannot_create_refs_or_attributions(answer):
    materials = number([_document(11)])
    all_refs = citation.refs_from_materials(materials, "rl-opt2-empty")
    original = deepcopy(all_refs)

    assert citation.refs_for_kind("normal", answer, all_refs) == []
    refs, selected = citation.build_refs("normal", answer, materials, "rl-opt2-empty")

    assert refs == selected == []
    assert citation.attribution_rows(31, materials, refs, "rl-opt2-empty") == []
    assert all_refs == original


@pytest.mark.parametrize("answer", ["说明性正文尚未配置引用。", "只能确认这些是候选资料 [99]。"])
def test_fallback_preserves_document_note_and_url_as_candidates(answer):
    materials = number(
        [
            _document(11),
            Material(
                chunk_id=-7,
                document_id=0,
                note_id=7,
                kb_id=2,
                filename="项目笔记",
                content="笔记原文。",
            ),
            Material(
                chunk_id=-900,
                document_id=0,
                kb_id=0,
                filename="项目网页",
                content="网页原文。",
                url="https://example.com/project",
            ),
        ]
    )

    refs, selected = citation.build_refs("normal", answer, materials, "rl-opt2-candidates")

    assert selected == materials
    assert [(ref["type"], ref["source_id"]) for ref in refs] == [
        ("document", 11),
        ("note", 7),
        ("web", 0),
    ]
    assert refs[2]["url"] == "https://example.com/project"
    assert {ref["citation_mode"] for ref in refs} == {"fallback"}
    assert classify_citations(answer, refs) == ([] if "[99]" not in answer else [
        ("citation/unresolved", {"marker": "[99]", "reason": "out_of_range", "available": 3})
    ])
    rows = citation.attribution_rows(31, materials, refs, "rl-opt2-candidates")
    assert [row["chunk_id"] for row in rows] == [111]
    assert rows[0]["meta"]["citation_mode"] == "fallback"


def test_same_marker_in_two_turns_keeps_history_and_each_source_snapshot():
    first_materials = number([_document(11)])
    second_materials = number([_document(22)])
    first_answer = "本轮预算为11万元 [1]。"
    second_answer = "本轮预算为22万元 [1]。"
    first_refs, first_selected = citation.build_refs(
        "normal", first_answer, first_materials, "rl-opt2-first"
    )
    original_first_refs = deepcopy(first_refs)
    second_refs, second_selected = citation.build_refs(
        "normal", second_answer, second_materials, "rl-opt2-second"
    )
    history = [
        {"content": first_answer, "refs": first_refs},
        {"content": second_answer, "refs": second_refs},
    ]

    assert first_refs == original_first_refs
    assert first_selected == first_materials
    assert second_selected == second_materials
    for message, expected_source, expected_snapshot in zip(
        history, [11, 22], ["rl-opt2-first", "rl-opt2-second"]
    ):
        resolved, mode = citation.resolve_citations(message["content"], message["refs"])
        assert mode == "cited"
        assert resolved[0]["ordinal"] == 1
        assert resolved[0]["source_id"] == expected_source
        assert resolved[0]["retrieval_id"] == expected_snapshot
        events = classify_citations(message["content"], message["refs"])
        assert events[0][0] == "citation/used"
        assert events[0][1]["chunk_id"] == resolved[0]["chunk_id"]
        assert events[0][1]["retrieval_id"] == expected_snapshot
    first_rows = citation.attribution_rows(31, first_materials, first_refs, "rl-opt2-first")
    second_rows = citation.attribution_rows(32, second_materials, second_refs, "rl-opt2-second")
    assert [(row["message_id"], row["chunk_id"], row["ordinal"]) for row in first_rows + second_rows] == [
        (31, 111, 1), (32, 122, 1)
    ]
    assert citation.cited_materials(second_materials, first_refs) == []
    assert citation.attribution_rows(32, second_materials, first_refs, "rl-opt2-second") == []
