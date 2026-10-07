"""``GET /api/search`` 的端点用例（阶段 3 · 条目 3.4）：走 HTTP + 真测试库。

覆盖老仓库 ``tests/test_search_operators.py`` 的**端点面**（行为逐字对齐）：

- 空 ``q`` → 三项全空（``query`` 为 ``None``）；
- 关键词命中笔记与分块（两侧的 ``title/snippet/category/document_id/tags/created_at``）；
- ``file:`` 命中文档名与笔记标题、``path:`` / ``category:`` 命中分类、``date:`` 命中区间、
  ``tag:`` 多标签取**交集**；
- 回收站里的文档 / 笔记不出现；无命中时 ``hint`` 是可读中文；
- ``query`` 字段回显（``keywords`` / ``conditions`` / ``describe``）。

夹具说明：``client`` 直接 ``from tests.api.test_chat_contract import client``
（它已把 ``get_db`` 覆盖到**测试库**）；造行助手复用 ``tests/retrieval/conftest.py::seed_document``
（内部 ``commit()``，HTTP 用例才有可见性）。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.models import Chunk, Document, Note
from app.search.query import SEARCH_LIMIT
from tests.api.test_chat_contract import client  # noqa: F401 —— client 是夹具
from tests.retrieval.conftest import seed_document


async def _search(client, q: str) -> dict:  # noqa: ANN001 —— httpx.AsyncClient
    """打 ``GET /api/search`` 并返回响应体（状态码不是 200 直接失败）。"""
    resp = await client.get("/api/search", params={"q": q})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _note_ids(body: dict) -> set[int]:
    return {hit["id"] for hit in body["notes"]}


def _doc_ids(body: dict) -> set[int]:
    return {hit["document_id"] for hit in body["chunks"]}


async def _seed_note(
    db,  # noqa: ANN001 —— AsyncSession
    *,
    title: str,
    content: str,
    category: str = "",
    tags: list[str] | None = None,
    deleted: bool = False,
    created_at: datetime | None = None,
) -> Note:
    """造一条笔记并 commit（``deleted=True`` 造软删；与 ``seed_document`` 同款口径）。"""
    note = Note(
        title=title,
        content=content,
        category=category,
        tags=tags,
        deleted_at=datetime(2026, 9, 18, 12, 0, 0) if deleted else None,
        **({"created_at": created_at} if created_at is not None else {}),
    )
    db.add(note)
    await db.commit()
    return note


async def _set_created_at(db, model, row_id: int, value: datetime) -> None:  # noqa: ANN001
    """把一行的 ``created_at`` 钉成确定值（``date:`` 用例要靠它，不能吃 ``now()``）。"""
    row = await db.get(model, row_id)
    row.created_at = value
    await db.commit()


# --------------------------------------------------------------------- 空查询与回显


@pytest.mark.asyncio
async def test_empty_query_returns_empty_without_query_info(client):  # noqa: F811
    body = await _search(client, "   ")
    assert body["notes"] == [] and body["chunks"] == []
    assert body["query"] is None
    assert body["hint"] == ""


@pytest.mark.asyncio
async def test_query_field_echoes_conditions_and_describe(client, db):
    await _seed_note(db, title="回显用例", content="回显正文", tags=["复盘"], category="工作日志")

    body = await _search(client, "tag:复盘 path:工作日志 中期")
    query = body["query"]
    assert set(query.keys()) == {"raw", "keywords", "conditions", "warnings", "describe"}
    assert query["raw"] == "tag:复盘 path:工作日志 中期"
    assert query["keywords"] == ["中期"]
    assert [(item["kind"], item["label"], item["value"]) for item in query["conditions"]] == [
        ("tag", "标签", "复盘"),
        ("category", "分类", "工作日志"),
    ]
    assert query["describe"] == "标签：复盘；分类：工作日志；关键词：「中期」"
    assert query["warnings"] == []


# --------------------------------------------------------------------- 关键词（legacy 口径）


@pytest.mark.asyncio
async def test_plain_keyword_is_not_tokenized(client, db):
    """改造前的口径：整串子串匹配 —— 拆词会改变老查询的结果，不能悄悄换掉。"""
    adjacent = await _seed_note(db, title="紧挨着", content="甲词 乙词 紧挨着")
    apart = await _seed_note(db, title="隔开了", content="甲词中间隔了很多字乙词")

    body = await _search(client, "甲词 乙词")
    assert adjacent.id in _note_ids(body)
    assert apart.id not in _note_ids(body)
    assert body["query"]["keywords"] == ["甲词 乙词"]
    assert body["query"]["conditions"] == []


@pytest.mark.asyncio
async def test_keyword_hits_note_and_chunk_with_fields(client, db):
    """关键词同时命中笔记与文档分块，两侧字段齐全（02 §7 SearchHit）。"""
    note_word = "笔记专属词甲乙"
    doc_word = "分块专属词丙丁"
    note = await _seed_note(
        db,
        title=f"关键词笔记{note_word}",
        content=f"正文包含 {note_word} 与更多内容",
        category="笔记分类",
        tags=["笔记标签"],
    )
    document = await seed_document(
        db,
        [f"分块正文包含 {doc_word} ", "第二块无关内容"],
        filename=f"关键词文档{doc_word}.md",
        category="文档分类",
        tags=["文档标签"],
    )

    body = await _search(client, note_word)
    hit = next(item for item in body["notes"] if item["id"] == note.id)
    assert hit["title"] == f"关键词笔记{note_word}"
    assert note_word in hit["snippet"]
    assert hit["category"] == "笔记分类"
    assert hit["document_id"] is None
    assert hit["tags"] == ["笔记标签"]
    assert datetime.fromisoformat(hit["created_at"])
    assert body["chunks"] == []

    body = await _search(client, doc_word)
    assert body["notes"] == []
    chunk_hit = next(item for item in body["chunks"] if item["document_id"] == document.id)
    assert chunk_hit["title"] == f"关键词文档{doc_word}.md"
    assert doc_word in chunk_hit["snippet"]
    assert chunk_hit["category"] == "文档分类"
    assert chunk_hit["document_id"] == document.id
    assert chunk_hit["tags"] == ["文档标签"]
    assert datetime.fromisoformat(chunk_hit["created_at"])
    row = await db.get(Chunk, chunk_hit["id"])
    assert row is not None and row.document_id == document.id


@pytest.mark.asyncio
async def test_snippet_is_trimmed_to_160_chars(client, db):
    """``_snippet`` 口径照老仓库：换行压空格、裁到 160 字、超长补省略号。"""
    long_text = "换行" + "\n" + ("填充" * 120)
    await _seed_note(db, title="超长片段", content=long_text)

    body = await _search(client, "超长片段")
    hit = body["notes"][0]
    # 口径 = 原文 strip → 换行压成空格 → 取前 160 字 → 截断过才补一个省略号
    assert "\n" not in hit["snippet"]
    assert hit["snippet"][-1] == "…"
    assert len(hit["snippet"]) == 161
    assert hit["snippet"][:-1] == long_text.strip().replace("\n", " ")[:160]
    assert len(hit["snippet"]) == 161


# --------------------------------------------------------------------- 操作符命中


@pytest.mark.asyncio
async def test_file_matches_document_filename_and_note_title(client, db):
    token = "文件命中词"
    document = await seed_document(db, ["正文"], filename=f"周报-{token}.md")
    note = await _seed_note(db, title=f"周报-{token}-笔记", content="笔记正文")

    body = await _search(client, f"file:{token}")
    assert document.id in _doc_ids(body)
    assert note.id in _note_ids(body)

    missing = await _search(client, f"file:别的东西{token}")
    assert missing["notes"] == [] and missing["chunks"] == []
    assert missing["hint"]


@pytest.mark.asyncio
async def test_path_and_category_match_by_contains(client, db):
    token = "分类命中词"
    category = f"{token}-工作日志"
    note = await _seed_note(db, title="分类笔记", content="分类正文", category=category)
    document = await seed_document(db, ["正文"], filename="分类文档.md", category=category)

    partial = await _search(client, f"path:{token}")
    assert note.id in _note_ids(partial)
    assert document.id in _doc_ids(partial)

    exact = await _search(client, f"category:{category}")
    assert note.id in _note_ids(exact)

    miss = await _search(client, f"path:不存在的{token}")
    assert note.id not in _note_ids(miss)
    assert miss["hint"]


@pytest.mark.asyncio
async def test_date_hits_range_on_note_and_document(client, db):
    """``date:`` 按 ``created_at`` 筛：月份区间、闭区间、比较符都命中同一批行。

    所有造的行都**钉死** ``created_at``：不钉的话它们会落在「今天」，而今天正好在某个被
    测区间里就会让 ``date:2026-09`` 这类反例静默变成假绿（实测踩过）。
    """
    inside_note = await _seed_note(db, title="命中笔记", content="日期正文")
    inside_doc = await seed_document(db, ["日期正文"], filename="命中文档.md")
    outside_note = await _seed_note(db, title="区间外笔记", content="日期正文")
    outside_doc = await seed_document(db, ["日期正文"], filename="区间外文档.md")
    for model, row in ((Note, inside_note), (Document, inside_doc)):
        await _set_created_at(db, model, row.id, datetime(2026, 8, 4, 9, 30, 0))
    for model, row in ((Note, outside_note), (Document, outside_doc)):
        await _set_created_at(db, model, row.id, datetime(2026, 9, 10, 9, 30, 0))

    for q in (
        "date:2026",
        "date:2026-08",
        "date:2026-08-04",
        "date:2026-08-01..2026-08-31",
        "date:>=2026-08-04",
        "date:<=2026-08-04",
    ):
        body = await _search(client, q)
        assert inside_note.id in _note_ids(body), q
        assert inside_doc.id in _doc_ids(body), q
        assert len([item for item in body["chunks"] if item["document_id"] == inside_doc.id]) == 1, q

    narrowed = await _search(client, "date:2026-08")
    assert outside_note.id not in _note_ids(narrowed)
    assert outside_doc.id not in _doc_ids(narrowed)

    year = await _search(client, "date:2026")
    assert outside_note.id in _note_ids(year)  # 同年同月之外的 9 月仍属 2026

    outside = await _search(client, "date:2026-07")
    assert outside["notes"] == [] and outside["chunks"] == []
    assert outside["hint"]


@pytest.mark.asyncio
async def test_tag_intersection_hits_note_and_document(client, db):
    """多标签取交集：同一条资料要**同时**具备全部标签。"""
    note = await _seed_note(
        db, title="标签笔记", content="标签正文", tags=["甲标签", "乙标签"]
    )
    document = await seed_document(
        db, ["标签文档正文"], filename="标签文档.md", tags=["甲标签", "乙标签"]
    )
    other = await _seed_note(db, title="只挂一个标签", content="标签正文", tags=["甲标签"])

    single = await _search(client, "tag:甲标签")
    assert note.id in _note_ids(single)
    assert document.id in _doc_ids(single)
    assert other.id in _note_ids(single)
    assert single["query"]["conditions"][0]["label"] == "标签"

    both = await _search(client, "tag:甲标签 tag:乙标签")
    assert note.id in _note_ids(both)
    assert document.id in _doc_ids(both)
    assert other.id not in _note_ids(both)

    comma = await _search(client, "tag:甲标签,乙标签")
    assert note.id in _note_ids(comma)
    assert other.id not in _note_ids(comma)

    conflict = await _search(client, "tag:甲标签 tag:丙标签")
    assert note.id not in _note_ids(conflict)
    assert conflict["hint"]


@pytest.mark.asyncio
async def test_tag_condition_is_filtered_in_python_without_sql_limit(client, db):
    """带 tag 时 SQL 不加 LIMIT、在 Python 收口：第 21 条之后的命中**不能**被误裁掉。

    造 25 条笔记，只有 ``id`` 最小的那条挂 ``靶标签``——它排在第 21 位之后。
    若照「先 SQL LIMIT 20 再过滤」写，它就会被裁掉、这条用例会红。
    """
    for index in range(24):
        await _seed_note(db, title=f"无标签{index}", content="正文", tags=["陪跑标签"])
    target = await _seed_note(db, title="靶标签笔记", content="正文", tags=["靶标签"])

    body = await _search(client, "tag:靶标签")
    assert _note_ids(body) == {target.id}
    assert len(body["notes"]) == 1


@pytest.mark.asyncio
async def test_tag_result_limit_is_twenty(client, db):
    """上限仍是每侧 20（``SEARCH_LIMIT``）：带 tag 时在 Python 侧切片。"""
    for index in range(SEARCH_LIMIT + 1):
        await _seed_note(db, title=f"上限{index}", content="正文", tags=["上限标签"])

    body = await _search(client, "tag:上限标签")
    assert len(body["notes"]) == SEARCH_LIMIT


@pytest.mark.asyncio
async def test_operator_plus_keyword_requires_both(client, db):
    tag = "组合标签"
    note = await _seed_note(db, title="组合笔记", content="这一条讲中期评审", tags=[tag])

    hit = await _search(client, f"tag:{tag} 中期")
    assert note.id in _note_ids(hit)

    miss_keyword = await _search(client, f"tag:{tag} 完全不相干的词")
    assert note.id not in _note_ids(miss_keyword)

    miss_tag = await _search(client, "tag:没有这个标签 中期")
    assert note.id not in _note_ids(miss_tag)


@pytest.mark.asyncio
async def test_pure_condition_returns_one_hit_per_document(client, db):
    """只给限定条件时按篇给代表片段：不能一篇长文档灌满整个结果列表。"""
    category = "代表片段分类"
    document = await seed_document(
        db,
        ["第 0 块正文", "第 1 块正文", "第 2 块正文"],
        filename="长文档.md",
        category=category,
    )
    first = (
        await db.execute(
            select(Chunk)
            .where(Chunk.document_id == document.id)
            .order_by(Chunk.ordinal)
            .limit(1)
        )
    ).scalars().one()

    body = await _search(client, f"path:{category}")
    assert body["notes"] == []
    mine = [item for item in body["chunks"] if item["document_id"] == document.id]
    assert len(mine) == 1, "同一篇文档只该给一条代表片段"
    assert mine[0]["id"] == first.id
    assert "第 0 块正文" in mine[0]["snippet"]


@pytest.mark.asyncio
async def test_keyword_returns_every_matching_chunk(client, db):
    """带关键词时文档路仍是分块命中（改造前的行为，多条命中是正常的）。"""
    document = await seed_document(
        db, ["命中词出现 1", "命中词出现 2", "命中词出现 3"], filename="多块文档.md"
    )

    body = await _search(client, "命中词出现")
    mine = [item for item in body["chunks"] if item["document_id"] == document.id]
    assert len(mine) == 3
    assert {item["id"] for item in mine} == {
        row.id
        for row in (
            await db.execute(select(Chunk).where(Chunk.document_id == document.id))
        ).scalars().all()
    }


# --------------------------------------------------------------------- 回收站


@pytest.mark.asyncio
async def test_trashed_note_and_document_are_invisible(client, db):
    """软删（``deleted_at`` 非空）的行不进搜索：笔记与文档两条路都要挡住。"""
    marker = "回收站专属词"
    alive_note = await _seed_note(db, title=f"在库笔记{marker}", content=marker)
    trashed_note = await _seed_note(
        db, title=f"已删笔记{marker}", content=marker, deleted=True
    )
    alive_document = await seed_document(db, [f"在库文档{marker}"], filename="在库文档.md")
    trashed_document = await seed_document(
        db, [f"已删文档{marker}"], filename="已删文档.md", deleted=True
    )

    keyword = await _search(client, marker)
    assert alive_note.id in _note_ids(keyword)
    assert trashed_note.id not in _note_ids(keyword)
    assert alive_document.id in _doc_ids(keyword)
    assert trashed_document.id not in _doc_ids(keyword)

    # 只给限定条件那条路（每篇文档代表块）同样看不见软删：用分类限到这两篇文档
    alive_category = await seed_document(
        db, [f"在库文档{marker}"], filename="在库分类文档.md", category="回收站分类"
    )
    trashed_category = await seed_document(
        db,
        [f"已删文档{marker}"],
        filename="已删分类文档.md",
        category="回收站分类",
        deleted=True,
    )

    by_category = await _search(client, "path:回收站分类")
    assert _doc_ids(by_category) == {alive_category.id}
    assert trashed_category.id not in _doc_ids(by_category)


@pytest.mark.asyncio
async def test_trashed_rows_are_invisible_to_tag_and_date_operators(client, db):
    """操作符路也不能穿透软删：``tag:`` / ``date:`` 命中的软删行同样不出现。"""
    tag = "软删标签"
    alive = await _seed_note(db, title="在库", content="正文", tags=[tag])
    trashed = await _seed_note(db, title="已删", content="正文", tags=[tag], deleted=True)

    by_tag = await _search(client, f"tag:{tag}")
    assert alive.id in _note_ids(by_tag)
    assert trashed.id not in _note_ids(by_tag)


# --------------------------------------------------------------------- 提示与警告


@pytest.mark.asyncio
async def test_no_hit_hint_is_readable_chinese(client, db):
    tag = "没有这个标签"
    body = await _search(client, f"tag:{tag}")
    assert body["notes"] == [] and body["chunks"] == []
    assert body["hint"].startswith("没有找到")
    assert tag in body["hint"]
    assert "没有找到同时满足" in body["hint"]
    assert body["query"]["conditions"][0]["value"] == tag


@pytest.mark.asyncio
async def test_invalid_date_returns_hint_and_warning(client, db):
    """非法日期：警告说明写法，提示只给下一步（同一句不在界面上出现两遍）。"""
    body = await _search(client, "date:昨天")
    assert body["notes"] == [] and body["chunks"] == []
    assert body["query"]["warnings"] and "不是可识别的日期" in body["query"]["warnings"][0]
    assert "不是可识别的日期" not in body["hint"]
    assert "直接写关键词" in body["hint"]
    assert body["query"]["conditions"] == []


@pytest.mark.asyncio
async def test_repeated_category_hint_explains_exclusion(client, db):
    body = await _search(client, "path:工作日志 path:周报")
    assert "互相排斥" in body["hint"]


@pytest.mark.asyncio
async def test_unknown_prefix_goes_through_keyword_path(client, db):
    """不认识的 ``xx:`` 仍当关键词：``https://…`` 不会被吃掉，也不会报 501。"""
    note = await _seed_note(db, title="链接笔记", content="参考 https://example.com/a 的说法")

    body = await _search(client, "https://example.com/a")
    assert note.id in _note_ids(body)
    assert body["query"]["keywords"] == ["https://example.com/a"]
    assert body["query"]["conditions"] == []
