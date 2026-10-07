# -*- coding: utf-8 -*-
"""回归检查：搜索关键词路的 LIKE 必须把 `%` / `_` 当**字面字符**（autoescape）。

缺陷 = `回归修复`（P3）：`backend/app/search/service.py` 的关键词路两处
`contains()` 漏了 `autoescape=True`（同文件 :80/:82/:92/:94 的分类/文件名路都写了）
⇒ 用户搜 `%` 命中**全表**、搜 `a_b` 把 `_` 当单字符通配符。

两层判别力：
1. **行为层**（`search()` 真跑，走测试库夹具 `db`）：`q=%` / `q=100%` / `q=a_b`
   只命中正文/标题**真含**该字面串的行；
2. **结构层**（不连库，编译 SQL）：笔记路与分块路的 LIKE 都带 `ESCAPE` 子句，
   **MySQL 与 SQLite 两种方言各判一次**（行为层的方言随 `DB_BACKEND` 走，结构层把两边都钉住）。

修复前实测（证据件 `_s2-scratch/scan/178-修复前证据.md`）：`q=%` ⇒ 6/6 笔记 + 3/3 文档（全表）；
`q=100%` 误命中「千元预算」（正文是 `1000`）；`q=a_b` 误命中「相似命名」（正文是 `aXb`）。

⛔ 本条只动 `backend/app/search/service.py`：`backend/app/retrieval/types.py` 里的
同名 `document_where` / `note_where` 是**另一条实现路径**（`backend/app/retrieval/keyword.py` 用它），
不属本条目。
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import mysql as mysql_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect

from app.models import Chunk, Document, Note
from app.search.query import parse_search
from app.search.service import chunk_where, note_where, search

# 语料：与探针 `_s2-scratch/scan/probe_178_like.py` 逐字同一份，便于与修复前读数对照
NOTES: tuple[tuple[str, str], ...] = (
    ("知澜使用说明", "这是一段普通正文"),
    ("折扣说明", "本季度增长 100% 已完成"),
    ("命名规范", "文件名 a_b 的下划线写法"),
    ("相似命名", "文件名 aXb 的下划线写法"),  # a_b 的通配诱饵
    ("千元预算", "预算 1000 元已批准"),  # 100% 的通配诱饵
    ("季度报告", "增长率为 42% 左右"),
)

DOCS: tuple[tuple[str, str], ...] = (
    ("文档甲.txt", "文档正文里的百分号 30% 折扣"),
    ("文档乙.txt", "文档正文没有特殊符号"),
    ("文档丙.txt", "文档正文 a_b 命名"),
)


async def _seed(db) -> None:  # noqa: ANN001 —— AsyncSession
    """按探针语料造行（6 笔记 + 3 文档各 1 分块），一次 commit。"""
    for title, content in NOTES:
        db.add(Note(title=title, content=content, category="", kb_id=1, tags=None))
    await db.flush()
    for filename, text in DOCS:
        document = Document(
            filename=filename,
            file_type="txt",
            status="ready",
            index_status="ready",
            kb_id=1,
            category="",
            chunk_count=1,
        )
        db.add(document)
        await db.flush()
        db.add(Chunk(document_id=document.id, content=text, ordinal=0))
    await db.commit()


async def _hits(db, raw: str) -> tuple[list[str], list[str]]:  # noqa: ANN001
    """搜一次，返回（命中笔记标题升序, 命中文档名升序）。"""
    result = await search(db, parse_search(raw))
    return (
        sorted(note.title for note in result.notes),
        sorted(document.filename for _, document in result.chunks),
    )


@pytest.mark.asyncio
async def test_percent_keyword_matches_only_literal_percent(db) -> None:  # noqa: ANN001
    """`q=%` 只命中**真含**字面 `%` 的行，⛔ 不得命中全表。"""
    await _seed(db)
    notes, docs = await _hits(db, "%")
    assert notes == ["季度报告", "折扣说明"], f"q=% 命中笔记错误：{notes}"
    assert docs == ["文档甲.txt"], f"q=% 命中文档错误：{docs}"


@pytest.mark.asyncio
async def test_100_percent_keyword_does_not_match_1000(db) -> None:  # noqa: ANN001
    """`q=100%` 里的 `%` 是字面字符 ⇒ 不得命中正文为 `1000` 的「千元预算」。"""
    await _seed(db)
    notes, docs = await _hits(db, "100%")
    assert notes == ["折扣说明"], f"q=100% 命中笔记错误：{notes}"
    assert docs == [], f"q=100% 不该命中任何文档：{docs}"


@pytest.mark.asyncio
async def test_underscore_keyword_is_not_a_single_char_wildcard(db) -> None:  # noqa: ANN001
    """`q=a_b` 里的 `_` 是字面字符 ⇒ 不得命中正文为 `aXb` 的「相似命名」。"""
    await _seed(db)
    notes, docs = await _hits(db, "a_b")
    assert notes == ["命名规范"], f"q=a_b 命中笔记错误：{notes}"
    assert docs == ["文档丙.txt"], f"q=a_b 命中文档错误：{docs}"


@pytest.mark.asyncio
async def test_plain_keyword_hit_set_is_unchanged(db) -> None:  # noqa: ANN001
    """正常关键词（不含 `%` / `_`）的命中集合与条数**逐字不变**（回归护栏）。

    `aXb` 是 `a_b` 的诱饵：修复后它仍必须能被搜到（`_` 不再当通配符后，
    用 `a_b` 搜不到它 = 正确；它本身仍是一个正常关键词）。
    """
    await _seed(db)
    notes, docs = await _hits(db, "知澜")
    assert notes == ["知澜使用说明"]
    assert docs == []
    notes, docs = await _hits(db, "aXb")
    assert notes == ["相似命名"]
    assert docs == []


@pytest.mark.parametrize(
    "dialect",
    [mysql_dialect.dialect(), sqlite_dialect.dialect()],
    ids=["mysql", "sqlite"],
)
@pytest.mark.parametrize("word", ["%", "100%", "a_b", "知澜"])
def test_compiled_like_carries_escape_clause(dialect, word: str) -> None:
    """结构层：笔记路与分块路的 LIKE 都带 `ESCAPE` 子句（生产方言 MySQL + 测试方言 SQLite）。"""
    query = parse_search(word)
    note_clauses = note_where(query)
    assert len(note_clauses) == 1, (
        f"本用例的前提是无分类/文件名/日期条件（关键词子句只有一条），实得 {len(note_clauses)} 条"
    )
    for clause in (note_clauses[0], chunk_where(query)[0]):
        sql = str(clause.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
        assert "ESCAPE" in sql.upper(), f"{word!r} 编译出的 LIKE 没有 ESCAPE 子句：{sql}"
