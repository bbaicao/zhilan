"""citation 器官（阶段 3 · 条目 3.3）：把「模型写了什么编号」变成**可追溯的会话事件**。

判定表（与老基线 ``rag/api.py`` 的口径逐条对应）：

| 情形 | 事件 | 载荷 |
|---|---|---|
| 有效：``[n]`` 对应本轮快照的冻结编号 | ``citation/used`` | ``{marker, ordinal, chunk_id, retrieval_id}`` |
| 越界：编号不在本轮快照中（N = 可用资料条数） | ``citation/unresolved`` | ``{marker, reason: "out_of_range", available: N}`` |
| 重复：同一编号第二次出现 | ``citation/unresolved`` | ``{marker, reason: "duplicate"}`` |
| 形态异常：全角 ``［1］`` / ``[ 1 ]``（**落在严格匹配之外**才算） | ``citation/unresolved`` | ``{marker, reason: "malformed"}`` |

``marker`` 是**正文里原样出现的字符串**，``ordinal`` 是归一化后的编号——两个字段同时给出，
「模型写了什么」与「解析成几号」在日志里才分得清。
``citation/used`` 保留现行事件名，仅证明有效标注编号，不能证明资料支持回答事实。

**零事件的轮次**：``clarify`` / ``refuse`` 一律零事件；``normal`` 且 ``refs`` 为空（理论上不发生）
也零事件；``direct`` 是**唯一例外**——它没有资料，任何严格形态编号都越界（``available: 0``），
必须留痕而不是当作「没检索结果」一笔带过。

**Agent 轮可复用**：判定函数是纯函数，Agent 只要把自己的材料快照（``materials`` + ``retrieval_id``）
喂进来即可，与普通轮**同源**（I3）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.generation.citation import CITE_RE, parse_citation_ordinal, snapshot_ordinal
from app.generation import source_map
from app.session import events as event_log

logger = logging.getLogger(__name__)

# 宽松形态：半角 / 全角方括号里夹着数字（`［1］`、`[ 1 ]`）。只有**落在严格匹配之外**的才算
# malformed；`（1）` 之类中文序号不记——它在正文里普遍是列表序号，认成未解析引用只会有噪声。
LOOSE_CITE_RE = re.compile(r"[\[［]\s*\d+\s*[\]］]")

USED = "citation/used"
UNRESOLVED = "citation/unresolved"

REASON_OUT_OF_RANGE = "out_of_range"
REASON_DUPLICATE = "duplicate"
REASON_MALFORMED = "malformed"


def classify_citations(answer: str, refs: list[dict]) -> list[tuple[str, dict]]:
    """逐条判定正文里的编号，返回 ``[(事件类型, 载荷), ...]``（按在正文里出现的先后）。

    编号取自冻结快照（旧记录缺编号按位置兼容），available 仍是提供的资料条数。
    有效编号只说明合法标注，不能证明资料支持事实或模型实际使用。
    """
    text = answer or ""
    total = len(refs or [])
    if total <= 0:
        return []
    by_ordinal = {
        snapshot_ordinal(ref.get("ordinal"), pos): ref
        for pos, ref in enumerate(refs, 1)
    }

    verdicts: list[tuple[int, str, dict]] = []
    seen: set[int] = set()
    strict_spans: set[int] = set()
    for match in CITE_RE.finditer(text):
        strict_spans.update(range(match.start(), match.end()))
        ordinal = parse_citation_ordinal(match.group(1))
        marker = match.group(0)
        if ordinal not in by_ordinal:
            verdicts.append(
                (
                    match.start(),
                    UNRESOLVED,
                    {"marker": marker, "reason": REASON_OUT_OF_RANGE, "available": total},
                )
            )
        elif ordinal in seen:
            verdicts.append(
                (match.start(), UNRESOLVED, {"marker": marker, "reason": REASON_DUPLICATE})
            )
        else:
            seen.add(ordinal)
            ref: dict[str, Any] = by_ordinal[ordinal]
            verdicts.append(
                (
                    match.start(),
                    USED,
                    {
                        "marker": marker,
                        "ordinal": ordinal,
                        "chunk_id": ref.get("chunk_id"),
                        "retrieval_id": ref.get("retrieval_id"),
                        "semantic_support": None,
                        "support_review": "unmeasured",
                    },
                )
            )
    for match in LOOSE_CITE_RE.finditer(text):
        if match.start() in strict_spans:
            continue  # 已经按严格形态判过（`[1]` / `[01]` / `[[1]]` 都落在这里）
        verdicts.append(
            (match.start(), UNRESOLVED, {"marker": match.group(0), "reason": REASON_MALFORMED})
        )
    verdicts.sort(key=lambda item: item[0])
    return [(event_type, payload) for _pos, event_type, payload in verdicts]


def classify_direct_citations(answer: str) -> list[tuple[str, dict]]:
    """直答轮（``kind == "direct"``）：本轮**没有任何资料**，所以任何编号都越界。

    :func:`classify_citations` 以 ``len(refs)`` 为分母、``refs`` 为空时直接返回空列表；
    直答轮要的恰恰相反（分母恒为 0），因此独立走一遍严格正则。
    """
    return [
        (UNRESOLVED, {"marker": match.group(0), "reason": REASON_OUT_OF_RANGE, "available": 0})
        for match in CITE_RE.finditer(answer or "")
    ]


def events_for(kind: str | None, answer: str, all_refs: list[dict]) -> list[tuple[str, dict]]:
    """按 ``kind`` 决定这一轮该写哪些引用事件（**唯一控制点**）。"""
    if source_map.refs_empty_for(kind) and (kind or "") != source_map.DIRECT_KIND:
        return []  # clarify / refuse：正文不是「依据资料的回答」，一条都不写
    if (kind or "") == source_map.DIRECT_KIND:
        return classify_direct_citations(answer)
    if not all_refs:
        return []
    return classify_citations(answer, all_refs)


async def log_citation_events(
    db: AsyncSession,
    chat_id: int | None,
    kind: str | None,
    answer: str,
    all_refs: list[dict],
    *,
    turn: int | None = None,
    trace: str | None = None,
) -> int:
    """写本轮 ``citation/*`` 事件，返回写出条数（**绝不抛**）。

    ``chat_id`` 为空时零 DB 触碰；每条事件都经 ``safe_append_event``（失败只留警告 + 计数）。

    ``trace`` 传本轮的 ``retrieval_id``：把这一轮的 ``retrieval/*`` 与 ``citation/*`` 串成一条链。
    **不传 ``request_id``**——``session_events`` 上 ``uk(chat_id, request_id)`` 是唯一约束，
    一轮只允许一条事件带它（现为 ``routing/decided``），多带会被静默吞掉（见 ``observe.trace`` docstring）。
    """
    if not chat_id:
        return 0
    written = 0
    for event_type, payload in events_for(kind, answer, all_refs):
        await event_log.safe_append_event(
            db, chat_id, event_type, payload, turn=turn, trace=trace
        )
        written += 1
    return written


__all__ = [
    "LOOSE_CITE_RE",
    "REASON_DUPLICATE",
    "REASON_MALFORMED",
    "REASON_OUT_OF_RANGE",
    "UNRESOLVED",
    "USED",
    "classify_citations",
    "classify_direct_citations",
    "events_for",
    "log_citation_events",
]
