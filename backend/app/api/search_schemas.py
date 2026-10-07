"""``/api/search`` 的响应模型（阶段 3 · 条目 3.4）：字段逐字照抄 02 契约 §7。

老仓库的 ``SearchHit`` / ``SearchConditionOut`` / ``SearchQueryOut`` / ``SearchOut``
（``<legacy-repo>\\backend\\app\\api\\__init__.py`` L236-269）在这里重建，
**字段名与可空性一字不改**（前端 ``api.ts`` 逐字依赖）：

- ``SearchHit``：``id, title, snippet, category, document_id, created_at, tags``
- ``SearchConditionOut``：``kind, label, value``
- ``SearchQueryOut``：``raw, keywords, conditions, warnings, describe``
- ``SearchOut``：``notes, chunks, query, hint``

数据来自 :mod:`app.search.service` 的原始行，模型只做形状转换（``from_search_*``），
不查库、不解析。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models import Chunk, Document, Note
from app.search.query import Condition, SearchQuery
from app.search.service import SearchResult


class SearchHit(BaseModel):
    """一条搜索命中（笔记或文档分块，两侧共用同一形状）。"""

    id: int
    title: str
    snippet: str
    category: str | None = None
    document_id: int | None = None
    # 任务清单 15：结果带上元数据，界面能显示「分类 / 标签 / 入库日期」
    created_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)


class SearchConditionOut(BaseModel):
    """解析出的一条搜索限定（界面照它画条件 chips）。"""

    kind: str
    label: str
    value: str


class SearchQueryOut(BaseModel):
    """搜索词的解析结果：关键词、限定条件、可读警告。"""

    raw: str = ""
    keywords: list[str] = Field(default_factory=list)
    conditions: list[SearchConditionOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    describe: str = ""


class SearchOut(BaseModel):
    """``GET /api/search`` 的响应体。"""

    notes: list[SearchHit]
    chunks: list[SearchHit]
    query: SearchQueryOut | None = None
    hint: str = ""


def snippet(text: str) -> str:
    """片段口径照老仓库 ``_snippet``：换行压成空格、裁到 160 字、超长补省略号。"""
    value = (text or "").strip().replace("\n", " ")
    return value[:160] + ("…" if len(value) > 160 else "")


def tags_list(value) -> list[str]:
    """tags 列历史上有列表/字典两种形态；统一成字符串列表给界面（只读，不改库）。"""
    if isinstance(value, dict):
        return [str(key) for key in value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    return []


def condition_out(condition: Condition) -> SearchConditionOut:
    """一条解析条件 → 响应模型（``label`` 是 ``kind`` 的中文名）。"""
    return SearchConditionOut(
        kind=condition.kind, label=condition.label, value=condition.value
    )


def query_out(query: SearchQuery) -> SearchQueryOut:
    """解析结果 → 响应模型（``describe`` 由解析层现算，不缓存）。"""
    return SearchQueryOut(
        raw=query.raw,
        keywords=list(query.keywords),
        conditions=[condition_out(item) for item in query.conditions],
        warnings=list(query.warnings),
        describe=query.describe(),
    )


def note_hit(note: Note) -> SearchHit:
    """笔记行 → 命中（``document_id`` 恒 ``None``：笔记不属任何文档）。"""
    return SearchHit(
        id=note.id,
        title=note.title,
        snippet=snippet(note.content),
        category=note.category,
        created_at=note.created_at,
        tags=tags_list(note.tags),
    )


def chunk_hit(chunk: Chunk, document: Document) -> SearchHit:
    """``(分块, 文档)`` → 命中：标题取文档名，``document_id`` 指回文档。"""
    return SearchHit(
        id=chunk.id,
        title=document.filename,
        snippet=snippet(chunk.content),
        category=document.category or None,
        document_id=document.id,
        created_at=document.created_at,
        tags=tags_list(document.tags),
    )


def hits_out(result: SearchResult) -> tuple[list[SearchHit], list[SearchHit]]:
    """原始行 → ``(notes, chunks)`` 两组命中。"""
    return (
        [note_hit(note) for note in result.notes],
        [chunk_hit(chunk, document) for chunk, document in result.chunks],
    )
