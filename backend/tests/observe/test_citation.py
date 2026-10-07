"""citation 器官用例（阶段 3 · 条目 3.3）：判定表四类 + 零事件的轮次 + 写库入口。

判定表与老基线 ``rag/api.py`` 逐条对应——`used` / `out_of_range` / `duplicate` / `malformed`。
"""

from __future__ import annotations

import pytest

from app.observe import citation as organ
from app.observe.citation import (
    REASON_DUPLICATE,
    REASON_MALFORMED,
    REASON_OUT_OF_RANGE,
    UNRESOLVED,
    USED,
)


def _refs(count: int = 3) -> list[dict]:
    return [
        {
            "ordinal": index,
            "chunk_id": f"kb:1:document:7:{index}",
            "retrieval_id": "rl-0123456789ab",
        }
        for index in range(1, count + 1)
    ]


def test_used_and_out_of_range_and_duplicate_and_malformed():
    answer = "见 [1]、[9]、[1] 与 ［2］。"
    events = organ.classify_citations(answer, _refs(3))

    assert events == [
        (
            USED,
            {
                "marker": "[1]",
                "ordinal": 1,
                "chunk_id": "kb:1:document:7:1",
                "retrieval_id": "rl-0123456789ab",
                "semantic_support": None,
                "support_review": "unmeasured",
            },
        ),
        (UNRESOLVED, {"marker": "[9]", "reason": REASON_OUT_OF_RANGE, "available": 3}),
        (UNRESOLVED, {"marker": "[1]", "reason": REASON_DUPLICATE}),
        (UNRESOLVED, {"marker": "［2］", "reason": REASON_MALFORMED}),
    ]


def test_events_follow_the_order_they_appear_in_the_answer():
    """事件顺序 = 正文出现顺序（与「留痕按正文先后」的老基线口径一致）。"""
    events = organ.classify_citations("[3] 先说，[1] 后说", _refs(3))
    assert [payload["ordinal"] for _type, payload in events] == [3, 1]


def test_leading_zero_and_multi_digit_markers():
    entries = [(USED, 1), (USED, 10)]
    events = organ.classify_citations("[01] 和 [10]", _refs(12))
    assert [(kind, payload["ordinal"]) for kind, payload in events] == entries


def test_no_refs_means_no_events():
    assert organ.classify_citations("见 [1]", []) == []


def test_direct_round_marks_every_marker_as_out_of_range():
    """直答轮没有资料 ⇒ 任何编号都越界（``available: 0``），必须留痕而不是静默。"""
    events = organ.classify_direct_citations("据资料 [1] 可知")
    assert events == [
        (UNRESOLVED, {"marker": "[1]", "reason": REASON_OUT_OF_RANGE, "available": 0})
    ]


@pytest.mark.parametrize("kind", ["clarify", "refuse"])
def test_blacklisted_kinds_produce_no_events(kind):
    assert organ.events_for(kind, "见 [1]", _refs(2)) == []


def test_events_for_routes_by_kind():
    assert organ.events_for("direct", "见 [1]", []) == organ.classify_direct_citations("见 [1]")
    assert organ.events_for("normal", "见 [2]", _refs(3)) == organ.classify_citations(
        "见 [2]", _refs(3)
    )
    # normal 但本轮无资料：零事件（理论上不发生，兜一层）
    assert organ.events_for("normal", "见 [2]", []) == []


@pytest.mark.asyncio
async def test_log_without_chat_id_touches_nothing():
    assert await organ.log_citation_events(None, None, "normal", "见 [1]", _refs(1)) == 0
