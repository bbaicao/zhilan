"""``GET /api/search``（阶段 3 · 条目 3.4）：全局搜索，支持结构化操作符限定。

行为以老仓库 ``app/api/__init__.py::search()``（L1053-1126）为基线：

- 没写操作符时与改造前**逐字一致**（整串关键词子串匹配、笔记与分块各 20 条、按 id 倒序），
  只是响应里多了 ``query`` / ``hint`` 两个只增字段（02 契约 §7）；
- 空 ``q`` → 三项全空（``query`` 为 ``None``）；写了操作符但一条都没成立（如 ``date:昨天``）
  → 空结果 + 可读 ``hint``（不把整库倒出来）；
- 无命中 → ``hint`` 是可读中文提示，说清按什么条件找的。

本模块只做 HTTP 面：解析（``app.search.query``）→ 取数（``app.search.service``）
→ 组装 pydantic 响应（``app.api.search_schemas``）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.search_schemas import SearchOut, hits_out, query_out
from app.db.session import get_db
from app.search import parse_search
from app.search.service import search

router = APIRouter()


@router.get("/search", response_model=SearchOut)
async def search_endpoint(q: str = "", db: AsyncSession = Depends(get_db)) -> SearchOut:
    """全局搜索（任务清单 15）：支持 ``tag:`` / ``path:`` / ``file:`` / ``date:`` 结构化限定。

    条件之间取交集；``path:`` 与 ``category:`` 同义；非法值只记 ``warnings`` 并跳过该条件。
    """
    query = parse_search(q)
    if query.is_blank:
        # 空查询：与改造前一致地返回空结果，且不带解析信息（老仓库的 query=None）
        return SearchOut(notes=[], chunks=[])

    result = await search(db, query)
    notes, chunks = hits_out(result)
    return SearchOut(
        notes=notes,
        chunks=chunks,
        query=query_out(query),
        hint=result.hint,
    )
