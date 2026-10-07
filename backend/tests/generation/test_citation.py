"""引用与归因的纯函数用例（阶段 3 · 条目 3.2）：Ref 十字段、[n] 三条规则、归因行。

三条规则（老基线 ``resolve_citations`` 与冻结前端 ``citation.ts`` 逐行同构）：
正则 ``\\[(\\d+)\\]`` / 有效集合 ``1 <= n <= len(refs)`` / 兜底 ``fallback`` 带出全部。
"""

from __future__ import annotations

import pytest

from app.generation import citation
from app.retrieval.types import FUSE_LIMIT, Material, number


def _materials(count: int = 3) -> list[Material]:
    items = [
        Material(
            chunk_id=100 + index,
            document_id=7,
            content=f"第 {index} 段：向量库使用 Qdrant。\n换行也要压平。",
            filename="手册.md",
            kb_id=1,
            block_ordinal=index,
            score=1.0 / (60 + index),
        )
        for index in range(1, count + 1)
    ]
    return number(items)


def test_max_refs_matches_retrieval_snapshot_limit():
    """上限 12 必须与检索侧快照上限**对齐**（两处常量各写一份，靠这条用例钉住）。"""
    assert citation.MAX_REFS == FUSE_LIMIT


def test_refs_from_materials_gives_all_eleven_fields():
    materials = _materials(2)
    refs = citation.refs_from_materials(materials, retrieval_id="rl-0123456789ab")

    assert len(refs) == 2
    first = refs[0]
    assert set(first) == {
        "type",
        "title",
        "snippet",
        "score",
        "kb_name",
        "source_id",
        "url",
        "chunk_id",
        "retrieval_id",
        "ordinal",
        "citation_mode",
    }
    assert first["type"] == "document"
    assert first["title"] == "手册.md"
    assert first["source_id"] == 7  # 文档 id（不是 chunk id）
    assert first["chunk_id"] == "kb:1:document:7:1"  # 冻结形态
    assert first["retrieval_id"] == "rl-0123456789ab"
    assert first["ordinal"] == 1
    assert first["citation_mode"] is None
    assert first["url"] is None
    assert first["kb_name"] is None


def test_scores_are_normalized_within_the_snapshot():
    refs = citation.refs_from_materials(_materials(3))
    assert refs[0]["score"] == pytest.approx(1.0)  # 快照内最高分归一到 1
    assert refs[1]["score"] < refs[0]["score"]
    assert 0.0 <= refs[2]["score"] < refs[1]["score"]


def test_snippet_flattens_newlines_and_truncates():
    long_material = [
        Material(chunk_id=1, document_id=1, content="行一\n行二\n" + "字" * 200, filename="a.md")
    ]
    number(long_material)
    ref = citation.refs_from_materials(long_material)[0]

    assert "\n" not in ref["snippet"]
    assert ref["snippet"].startswith("行一 行二 ")
    assert len(ref["snippet"]) == citation.SNIPPET_LIMIT + 1  # 截断 + 省略号
    assert ref["snippet"].endswith("…")


def test_cited_markers_keep_only_the_referenced_sources():
    refs = citation.refs_from_materials(_materials(3))
    resolved, mode = citation.resolve_citations("先看 [3]，再看 [1]。", refs)

    assert mode == "cited"
    assert [item["ordinal"] for item in resolved] == [1, 3]  # 按**原列表顺序**，不是正文出现顺序
    assert {item["citation_mode"] for item in resolved} == {"cited"}


def test_missing_markers_fall_back_to_every_source():
    refs = citation.refs_from_materials(_materials(3))
    resolved, mode = citation.resolve_citations("我没有写编号。", refs)

    assert mode == "fallback"
    assert [item["ordinal"] for item in resolved] == [1, 2, 3]
    assert {item["citation_mode"] for item in resolved} == {"fallback"}


def test_out_of_range_markers_also_fall_back():
    refs = citation.refs_from_materials(_materials(2))
    for answer in ("见 [9]", "见 [0]"):
        resolved, mode = citation.resolve_citations(answer, refs)
        assert mode == "fallback", answer
        assert len(resolved) == 2


def test_empty_refs_returns_none_mode():
    assert citation.resolve_citations("见 [1]", []) == ([], "none")
    assert citation.cited_refs("见 [1]", []) == []


def test_marker_forms_and_duplicates():
    refs = citation.refs_from_materials(_materials(4))
    # 前导零归一为 1；重复编号只保留一条（位置集合语义）
    resolved, mode = citation.resolve_citations("[01] 和 [1] 和 [4]", refs)
    assert mode == "cited"
    assert [item["ordinal"] for item in resolved] == [1, 4]
    # 多位数字按整体解析（`[10]` 不是 `[1]` + 残留 `0`）
    many = citation.refs_from_materials(_materials(12))
    resolved, mode = citation.resolve_citations("见 [10]", many)
    assert mode == "cited"
    assert [item["ordinal"] for item in resolved] == [10]
    # 全角括号不构成编号
    resolved, mode = citation.resolve_citations("见 ［1］", refs)
    assert mode == "fallback"


def test_resolve_does_not_mutate_input_refs():
    refs = citation.refs_from_materials(_materials(2))
    citation.resolve_citations("见 [1]", refs)
    assert [item["citation_mode"] for item in refs] == [None, None]


@pytest.mark.parametrize("kind", ["direct", "refuse", "clarify"])
def test_blacklisted_kinds_never_carry_refs(kind):
    """恒空的 kind 一律零引用——即使调用方误传了资料（I7 的兜底）。"""
    refs = citation.refs_from_materials(_materials(2))
    assert citation.refs_for_kind(kind, "见 [1]", refs) == []


def test_normal_kind_goes_through_citation_resolution():
    refs = citation.refs_from_materials(_materials(2))
    resolved = citation.refs_for_kind("normal", "见 [2]", refs)
    assert [item["ordinal"] for item in resolved] == [2]


def test_cited_materials_and_attribution_rows_are_sourced_from_the_snapshot():
    materials = _materials(3)
    refs = citation.refs_from_materials(materials, retrieval_id="rl-abcdefabcdef")
    resolved = citation.refs_for_kind("normal", "见 [2]", refs)

    cited = citation.cited_materials(materials, resolved)
    assert [item.chunk_id for item in cited] == [materials[1].chunk_id]

    rows = citation.attribution_rows(555, materials, resolved, "rl-abcdefabcdef")
    assert len(rows) == 1
    row = rows[0]
    assert row["message_id"] == 555
    assert row["chunk_id"] == materials[1].chunk_id
    assert row["ordinal"] == 2
    assert row["title"] == "手册.md"
    assert row["snippet"].startswith("第 2 段")
    assert row["score"] == pytest.approx(resolved[0]["score"])
    assert row["meta"]["retrieval_id"] == "rl-abcdefabcdef"
    assert row["meta"]["citation_mode"] == "cited"
    assert row["meta"]["chunk_key"] == "kb:1:document:7:2"
    assert row["meta"]["kb_id"] == 1


def test_build_refs_is_the_shared_entry_point_for_agent_turns():
    """阶段 4 Agent 轮的同源入口：一次给全「最终 refs + 被引用材料」。"""
    materials = _materials(2)
    refs, cited = citation.build_refs("normal", "见 [2]", materials, "rl-111122223333")

    assert [item["ordinal"] for item in refs] == [2]
    assert refs[0]["retrieval_id"] == "rl-111122223333"
    assert [item.chunk_id for item in cited] == [materials[1].chunk_id]

    # 与「分步调用」逐字段一致（同源，不多一条也不少一条）
    stepwise_all = citation.refs_from_materials(materials, "rl-111122223333")
    assert refs == citation.refs_for_kind("normal", "见 [2]", stepwise_all)


def test_attribution_rows_skip_rows_without_a_material():
    """``ordinal`` 在快照里对不上就不写行——不写半条假归因。"""
    materials = _materials(2)
    rows = citation.attribution_rows(
        1, materials, [{"ordinal": 99, "title": "x", "snippet": "y", "score": 0.5}], None
    )
    assert rows == []


def test_note_materials_carry_the_note_id_as_source_id():
    """阶段 4 · 4.5.2 的集成钉：**笔记材料的 ``Ref.source_id`` 是笔记 id**（不是 0）。

    为什么必须钉：冻结前端 `CitationCard.tsx:57` 把 ``source_id`` 当
    ``GET /api/wiki/related?type=note&id=<source_id>`` 传回来，而 `wiki_links` 的边表里
    **笔记与文档是两个 id 空间**（笔记用 ``note_id``）。若这里回 0，前端只能退回按**标题**
    解析（`wikilink.resolve_title`），重名标题就会指错来源；文档 / 网页两种材料的取值
    必须**逐字不变**（文档 = 文档 id，网页 = 0）。
    """
    from app.retrieval.types import note_chunk_id

    note = Material(
        chunk_id=note_chunk_id(7),
        document_id=0,
        kb_id=1,
        filename="笔记标题",
        content="笔记正文",
        score=1.0,
        note_id=7,
    )
    document, web = _materials(1)[0], Material(
        chunk_id=111,
        document_id=0,
        kb_id=0,
        filename="网页",
        content="正文",
        score=1.0,
        url="https://example.com/a",
    )
    refs = citation.refs_from_materials([note, document, web], "rl-note")

    assert refs[0]["type"] == "note"
    assert refs[0]["source_id"] == 7, "笔记材料的来源 id 必须是笔记 id（前端靠它查「相关笔记」）"
    assert refs[0]["chunk_id"] == "kb:1:note:7:0"
    assert refs[1]["type"] == "document" and refs[1]["source_id"] == 7  # 文档 id，未受影响
    assert refs[2]["type"] == "web" and refs[2]["source_id"] == 0, "网页不是库内来源，仍是 0"
