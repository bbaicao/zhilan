"""命中块周边的有界正文窗口；重排和生成共同使用同一份正文。"""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Document, LexicalMaterial, Note, active_chunks
from app.retrieval.keyword import READY_STATUS, segments, tokenize
from app.retrieval.types import (
    MATERIAL_ITEM_MAX_CHARS, Material, document_where, matches_document,
    matches_note, note_where,
)


def focus_window(content: str, question: str, limit: int) -> tuple[str, int, int]:
    """超长单块围绕最具体的命中词裁剪，返回原正文坐标。"""
    if len(content) <= limit:
        return content, 0, len(content)
    folded = content.casefold()
    terms = sorted(set(segments(question) + tokenize(question)), key=lambda term: (-len(term), term))
    position = next((folded.find(term.casefold()) for term in terms if term.casefold() in folded), 0)
    start = max(0, min(position - limit // 3, len(content) - limit))
    end = min(len(content), start + limit)
    # 保留完整行；单行超限时仍以命中位置为中心。
    if start and (line := content.find("\n", start, min(end, start + 120))) >= 0:
        if line < position:
            start = line + 1
    if end < len(content) and (line := content.rfind("\n", max(start, end - 120), end)) >= position:
        end = line
    return content[start:end], start, end


def _join_bodies(
    bodies: list[str], spans: list[tuple[int | None, int | None]] | None = None,
) -> str:
    if not bodies:
        return ""
    text = bodies[0]
    previous_end = spans[0][1] if spans else None
    for index, body in enumerate(bodies[1:], start=1):
        start, end = spans[index] if spans else (None, None)
        known = previous_end is not None and start is not None
        # 坐标不重叠的重复正文是真实第二次出现，不能用文本相同吞掉。
        maximum = max(0, previous_end - start) if known else min(len(text), len(body))
        # ponytail: 最多一块的有界前后缀对比，兼容历史无坐标块及结构补围栏。
        overlap = next(
            (size for size in range(min(len(text), len(body), maximum), 0 if known else 7, -1)
             if text.endswith(body[:size])),
            0,
        )
        separator = "" if known and previous_end == start else "\n\n"
        text += body[overlap:] if overlap else separator + body
        previous_end = max(previous_end, end) if previous_end is not None and end is not None else end
    return text


async def expand_windows(
    session: AsyncSession, materials: list[Material], question: str,
    filters: dict | None = None, *, limit: int = MATERIAL_ITEM_MAX_CHARS,
    total_limit: int = 48_000,
) -> list[Material]:
    """仅读取命中来源的左右一块；先保留每个命中，再分配邻域预算。"""
    if not materials:
        return []
    limit = max(1, int(limit))
    total_limit = max(1, int(total_limit))
    document_hits = [item for item in materials if item.document_id > 0 and not item.url]
    document_rows: dict[tuple[int, int], object] = {}
    if document_hits:
        clauses = [
            (Chunk.document_id == item.document_id)
            & Chunk.ordinal.between(max(0, item.block_ordinal - 1), item.block_ordinal + 1)
            for item in document_hits
        ]
        rows = (await session.execute(
            select(
                Chunk.document_id, Chunk.ordinal, Chunk.content,
                Chunk.start_offset, Chunk.end_offset,
                Document.kb_id, Document.category, Document.tags, Document.created_at,
            ).join(Document, Document.id == Chunk.document_id).where(
                or_(*clauses), active_chunks(), Document.deleted_at.is_(None),
                Document.index_status == READY_STATUS, *document_where(filters),
            )
        )).all()
        document_rows = {
            (int(row.document_id), int(row.ordinal)): row for row in rows
            if matches_document(
                filters, kb_id=row.kb_id, category=row.category,
                tags=row.tags, created_at=row.created_at,
            )
        }

    note_hits = [item for item in materials if item.note_id and item.span_start is not None]
    note_rows: dict[tuple[int, int], object] = {}
    if note_hits:
        clauses = [
            (LexicalMaterial.note_id == item.note_id)
            & LexicalMaterial.fragment_ordinal.between(
                max(0, (item.fragment_ordinal or 0) - 1), (item.fragment_ordinal or 0) + 1,
            )
            for item in note_hits
        ]
        rows = (await session.execute(
            select(
                LexicalMaterial.note_id, LexicalMaterial.fragment_ordinal,
                LexicalMaterial.span_start, LexicalMaterial.span_end,
                func.substr(
                    Note.content, LexicalMaterial.span_start + 1,
                    LexicalMaterial.span_end - LexicalMaterial.span_start,
                ).label("body"),
                Note.kb_id, Note.category, Note.tags, Note.created_at,
            ).join(Note, Note.id == LexicalMaterial.note_id).where(
                or_(*clauses), Note.deleted_at.is_(None), *note_where(filters),
            )
        )).all()
        note_rows = {
            (int(row.note_id), int(row.fragment_ordinal)): row for row in rows
            if matches_note(
                filters, kb_id=row.kb_id, category=row.category,
                tags=row.tags, created_at=row.created_at,
            )
        }

    # 先为全部命中分配核心预算，再用余量补邻域；十六候选也不能超总量。
    cores = []
    remaining = total_limit
    for index, item in enumerate(materials):
        core_limit = min(limit, remaining // (len(materials) - index))
        core = focus_window(item.content, question, core_limit) if core_limit else ("", 0, 0)
        cores.append((core, core_limit))
        remaining -= len(core[0])
    result: list[Material] = []
    for item, ((core, core_start, core_end), core_limit) in zip(materials, cores):
        if item.url:
            result.append(replace(item, content=core, windowed=True))
            continue
        is_note = bool(item.note_id)
        ordinal = int((item.fragment_ordinal or 0) if is_note else item.block_ordinal)
        source_id = int(item.note_id or item.document_id)
        source_rows = note_rows if is_note else document_rows
        current = source_rows.get((source_id, ordinal))
        chosen = {ordinal: item.content}
        starts = {ordinal: item.span_start if is_note else getattr(current, "start_offset", None)}
        ends = {ordinal: item.span_end if is_note else getattr(current, "end_offset", None)}
        if len(item.content) <= core_limit:
            for neighbor in (ordinal - 1, ordinal + 1):
                row = source_rows.get((source_id, neighbor))
                if row is None:
                    continue
                candidate = {**chosen, neighbor: (row.body if is_note else row.content) or ""}
                candidate_starts = {**starts, neighbor: row.span_start if is_note else row.start_offset}
                candidate_ends = {**ends, neighbor: row.span_end if is_note else row.end_offset}
                joined = _join_bodies(
                    [candidate[key] for key in sorted(candidate)],
                    [(candidate_starts[key], candidate_ends[key]) for key in sorted(candidate)],
                )
                extra = len(joined) - len(core)
                if len(joined) <= limit and extra <= remaining:
                    chosen = candidate
                    starts, ends = candidate_starts, candidate_ends
        body = (
            _join_bodies(
                [chosen[key] for key in sorted(chosen)],
                [(starts[key], ends[key]) for key in sorted(chosen)],
            ) if len(item.content) <= core_limit else core
        )
        remaining -= max(0, len(body) - len(core))
        known_starts = [value for value in starts.values() if value is not None]
        known_ends = [value for value in ends.values() if value is not None]
        start = min(known_starts) if known_starts else None
        end = max(known_ends) if known_ends else None
        if len(item.content) > core_limit and is_note:
            # 笔记片段是原串切片，可以线性偏移。文档含展示补围栏/表头，
            # 裁剪后仍引用原块的包围范围，不能用展示字符位置推算源坐标。
            base = item.span_start
            start = base + core_start if base is not None else None
            end = base + core_end if base is not None else None
        window = replace(
            item, content=body, span_start=start, span_end=end,
            covered_ordinals=tuple(sorted(chosen)), windowed=True,
        )
        # 同一来源的相邻命中仅在完整窗口可装下时合并，保留最早的主候选排序。
        merged = False
        for index, previous in enumerate(result):
            if previous.source_ref != window.source_ref or not set(previous.covered_ordinals) & set(window.covered_ordinals):
                continue
            covered = sorted(set(previous.covered_ordinals) | set(window.covered_ordinals))
            rows = [source_rows.get((source_id, value)) for value in covered]
            if any(row is None for row in rows):
                continue
            joined = _join_bodies(
                [(row.body if is_note else row.content) or "" for row in rows],
                [(row.span_start, row.span_end) if is_note else (row.start_offset, row.end_offset) for row in rows],
            )
            extra = len(joined) - len(previous.content) - len(window.content)
            if len(joined) > limit or extra > remaining:
                continue
            remaining -= extra
            result[index] = replace(
                previous, content=joined,
                span_start=min(previous.span_start, start) if previous.span_start is not None and start is not None else None,
                span_end=max(previous.span_end, end) if previous.span_end is not None and end is not None else None,
                covered_ordinals=tuple(covered),
            )
            merged = True
            break
        if not merged:
            result.append(window)
    return result
