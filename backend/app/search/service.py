"""全局搜索的 DB 查询层（阶段 3 · 条目 3.4）：只依赖 ``app.db`` / ``app.models``。

行为以老仓库 ``app/api/__init__.py::search()`` 为基线，按 v2 分层重写；
分页/排序口径逐字沿用：笔记按 ``Note.id`` 倒序、文档路有关键词时按 ``Chunk.id`` 倒序、
只给限定条件时每篇文档只取第 0 块（``first_chunk_statement``），两侧各自上限 ``SEARCH_LIMIT``。

**v2 适配（与老仓库的三处偏离，逐条说明）**

1. **标签过滤在 Python 侧收口**（v2 统一做法，见 ``app.retrieval.types::matches_document``）：
   v2 **不写** ``JSON_CONTAINS`` / ``json_each`` 之类的方言 SQL（老仓库是 MySQL 专用写法，
   换 SQLite 就废）⇒ **带 tag 条件时不给 SQL 加 ``LIMIT``**，取回后在 Python 里按
   「同一条资料要**同时**具备全部标签」过滤，再切片到 ``SEARCH_LIMIT``；
   不带 tag 时照旧 SQL ``LIMIT 20``。标签的归一与判定直接复用检索层那一份
   （``filter_tags``），不新造第二套「标签真相源」；
2. **软删 / 就绪**：笔记与文档都排除 ``deleted_at`` 非空（回收站不进搜索，老基线同款）；
   文档**不查** ``index_status`` —— 老仓库也没查，保持一致（v2 不自行加门禁）；
3. **不搬老仓库的 ``Filters``**：``category`` / ``file`` / ``date`` 直接用 ``Document`` /
   ``Note`` 的列写 SQLAlchemy 子句（``contains(value, autoescape=True)`` 的写法照抄），
   只有 ``tag`` 走第 1 条。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Document, Note, active_chunks
from app.retrieval.types import filter_tags
from app.search.query import (
    SEARCH_LIMIT,
    SearchQuery,
    no_condition_hint,
    no_hit_hint,
)


@dataclass
class SearchResult:
    """一次搜索取回的**原始行**（笔记行 + ``(分块, 文档)`` 对），外加无命中提示。

    转换与响应组装留给 ``app/api/search_routes.py``（HTTP 层），本层不 import pydantic。
    """

    notes: list[Note] = field(default_factory=list)
    chunks: list[tuple[Chunk, Document]] = field(default_factory=list)
    hint: str = ""


def tag_filter_enabled(query: SearchQuery) -> bool:
    """是否需要在 Python 侧做标签收口（老仓库是 ``JSON_CONTAINS`` 子句，v2 不走 SQL）。"""
    return bool(filter_tags({"tags": query.tags}))


def _owned_tags(tags) -> set[str]:
    """一侧资料的标签集合：``tags`` 列历史上有列表/字典两种形态，统一去空白成集合。"""
    if isinstance(tags, dict):
        raw = list(tags.keys())
    elif isinstance(tags, (list, tuple, set)):
        raw = list(tags)
    else:
        return set()
    return {str(item).strip() for item in raw if item is not None and str(item).strip()}


def has_all_tags(owned_tags, wanted: list[str]) -> bool:
    """同一条资料要**同时**具备全部标签（判定与 ``app.retrieval.types`` 同口径）。"""
    if not wanted:
        return True
    return all(tag in _owned_tags(owned_tags) for tag in wanted)


def note_where(query: SearchQuery) -> list:
    """笔记路条件（含关键词：标题或正文命中）。

    标签**不在**这里：见模块 docstring 第 1 条，它由 :func:`has_all_tags` 在 Python 侧收口。
    """
    clauses: list = [
        Note.category.contains(value, autoescape=True) for value in query.categories
    ]
    clauses += [Note.title.contains(value, autoescape=True) for value in query.files]
    clauses += _date_clauses(Note, query)
    for word in query.keywords:
        clauses.append(
            or_(
                Note.title.contains(word, autoescape=True),
                Note.content.contains(word, autoescape=True),
            )
        )
    return clauses


def document_where(query: SearchQuery) -> list:
    """文档级条件（作用在 ``documents`` 表上，不含关键词；标签同样不在 SQL 里）。"""
    clauses: list = [
        Document.category.contains(value, autoescape=True) for value in query.categories
    ]
    clauses += [Document.filename.contains(value, autoescape=True) for value in query.files]
    clauses += _date_clauses(Document, query)
    return clauses


def chunk_where(query: SearchQuery) -> list:
    """关键词在文档路的落点：分块正文包含（与改造前一致）。"""
    return [Chunk.content.contains(word, autoescape=True) for word in query.keywords]


def first_chunk_statement(clauses: list):
    """只给了限定条件、没给关键词时的文档命中：每篇文档只取第 0 块当代表片段。

    否则一篇几十块的长文档会把 20 条结果全占满，看起来像「只有一篇文档」。
    """
    first = (
        select(Chunk.document_id, func.min(Chunk.ordinal).label("ordinal"))
        .join(Document, Document.id == Chunk.document_id)
        .where(active_chunks())
        .group_by(Chunk.document_id)
        .subquery()
    )
    statement = (
        select(Chunk, Document)
        .join(Document, Document.id == Chunk.document_id)
        .join(
            first,
            (first.c.document_id == Chunk.document_id) & (first.c.ordinal == Chunk.ordinal),
        )
        .where(active_chunks())
        .order_by(Document.id.desc())
    )
    if clauses:
        statement = statement.where(*clauses)
    return statement


def _date_clauses(model, query: SearchQuery) -> list:
    """``date:`` 条件的落点：``created_at`` 区间（``date_to`` 是右开区间）。"""
    clauses: list = []
    if query.date_from is not None:
        clauses.append(model.created_at >= query.date_from)
    if query.date_to is not None:
        clauses.append(model.created_at < query.date_to)
    return clauses


async def search(db: AsyncSession, query: SearchQuery) -> SearchResult:
    """执行一次搜索。**先判空白与「一条条件都没成立」**，两种情况都不查库。"""
    if query.is_blank:
        return SearchResult()
    if query.is_empty:
        # 写了操作符但一条都没成立（如 date:昨天）：别把整库倒出来，给可读提示
        return SearchResult(hint=no_condition_hint(query))

    wanted = filter_tags({"tags": query.tags})
    by_tag = tag_filter_enabled(query)

    note_statement = (
        select(Note)
        .where(Note.deleted_at.is_(None), *note_where(query))  # 回收站不进搜索（条目 25）
        .order_by(Note.id.desc())
    )
    if not by_tag:
        note_statement = note_statement.limit(SEARCH_LIMIT)
    notes = list((await db.execute(note_statement)).scalars().all())
    if by_tag:
        # 带 tag：SQL 不加 LIMIT，取回后按「同时具备全部标签」过滤、再切片（见模块 docstring）
        notes = [note for note in notes if has_all_tags(note.tags, wanted)][:SEARCH_LIMIT]

    # 文档级条件拼上回收站过滤：两条 chunk 路（有关键词 join / 无关键词代表块）都要看不见软删
    doc_clauses = [Document.deleted_at.is_(None), *document_where(query)]
    if query.keywords:
        chunk_statement = (
            select(Chunk, Document)
            .join(Document, Document.id == Chunk.document_id)
            .where(*doc_clauses, active_chunks(), *chunk_where(query))
            .order_by(Chunk.id.desc())
        )
    else:
        # 只给了限定条件：每篇文档只给一条代表片段，不按分块灌满结果列表
        chunk_statement = first_chunk_statement(doc_clauses)
    if not by_tag:
        chunk_statement = chunk_statement.limit(SEARCH_LIMIT)
    chunk_rows = list((await db.execute(chunk_statement)).all())
    if by_tag:
        chunk_rows = [row for row in chunk_rows if has_all_tags(row[1].tags, wanted)]
    chunk_rows = chunk_rows[:SEARCH_LIMIT]

    result = SearchResult(notes=notes, chunks=chunk_rows)
    if not result.notes and not result.chunks:
        result.hint = no_hit_hint(query)
    return result


__all__ = [
    "SEARCH_LIMIT",
    "SearchResult",
    "chunk_where",
    "document_where",
    "first_chunk_statement",
    "has_all_tags",
    "note_where",
    "search",
    "tag_filter_enabled",
]
