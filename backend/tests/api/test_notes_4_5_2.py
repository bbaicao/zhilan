"""阶段 4 · 条目 4.5.2（notes · wiki · graph · knowledge-bases 域）的用例。

覆盖四组：

1. **字段表逐字**：把 02 §7.4 的字段表当**字面量**写死在用例里（不是从模型反射生成），
   任何一侧偷偷加/删/改名都会红；响应体的键集合另有一层端到端断言。
2. **端点语义**：空态、404 / 400 / 422、软删（``Note.deleted_at``）、kb 创建边界。
3. **``回归修复`` 的 B-lite**：笔记进关键词路（``Material.source_type="note"`` /
   ``Ref.type="note"``）、id 空间不与 ``chunks.id`` 串号、「范围只含笔记」不再静默。
4. **``回归修复``**：边写入（笔记保存 / 文档入库）+ 一跳扩展 + 材料上限口径。

夹具复用 ``tests/api/conftest.py`` 的 autouse 隔离（向量路假件 / 重排关闭 / 联网关闭）与
``tests.conftest`` 的 ``db`` / ``engine``；``client`` / ``use_faux`` 从 ``test_chat_contract``
导入（不改公共 conftest）。**``notes`` / ``wiki_links`` / ``knowledge_bases`` 不在
``tests/conftest.py::_CLEAN_ORDER`` 里**（那份归主会话），故本文件自带 autouse 清理。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from app.api.note_routes import (
    GraphEdge,
    GraphNode,
    GraphOut,
    GraphStats,
    KnowledgeBaseOut,
    NoteOut,
    WikiLinkHit,
    WikiLinksOut,
)
from app.models import Chunk, Document, KnowledgeBase, Note, WikiLink
from tests.api.test_chat_contract import client, faux_model  # noqa: F401

# 本域自建的三张表不在 tests/conftest.py::_CLEAN_ORDER 里，夹具只能落在本文件。
_DOMAIN_TABLES = ("wiki_links", "notes", "knowledge_bases")


@pytest_asyncio.fixture(autouse=True)
async def clean_domain_tables(db):
    """用例前清空本域的三张表（顺序：先边后端点，避免逻辑引用残留）。"""
    for table in _DOMAIN_TABLES:
        await db.execute(text(f"DELETE FROM {table}"))
    await db.commit()
    yield


@pytest.fixture
def use_faux(monkeypatch):
    """把端点里的模型装配换成 Faux，并让分类器可指定（默认判「库外」）。"""
    from app.api import chat_routes

    class _StubClassifier:
        def __init__(self, label: str | None) -> None:
            self.label = label

        async def classify(self, text: str) -> str | None:  # noqa: ANN001
            return self.label

    def _install(reply: str, *, label: str = "external"):
        model = faux_model(reply)
        monkeypatch.setattr(chat_routes, "resolve_chat_model", lambda: model)
        monkeypatch.setattr(
            chat_routes, "resolve_routing_classifier", lambda: _StubClassifier(label)
        )
        return model

    return _install


async def _add_document(
    db,
    filename: str,
    *,
    content: str = "正文",
    kb_id: int = 1,
    category: str = "",
    index_status: str = "ready",
    deleted_at=None,
) -> Document:
    """插一个带单个分块的文档（``ready`` 才可检索）。返回文档行。"""
    doc = Document(
        filename=filename,
        file_type="md",
        status=index_status,
        index_status=index_status,
        kb_id=kb_id,
        category=category,
        chunk_count=1,
        deleted_at=deleted_at,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    db.add(Chunk(document_id=doc.id, content=content, ordinal=0))
    await db.commit()
    return doc


async def _add_note(db, title: str, content: str, *, category: str = "", kb_id: int = 1) -> Note:
    note = Note(title=title, content=content, category=category, kb_id=kb_id)
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


# ===========================================================================
# 一、字段表逐字（字面量写死，不从模型反射）
# ===========================================================================
#
# 来源：`接口说明/02-API契约冻结清单.md` §7.4（NoteOut :234-235 / WikiLinkHit :270-271 /
# WikiLinksOut :273-274 / GraphNode :258-259 / GraphEdge :261-262 / GraphStats :264-265 /
# GraphOut :267-268 / KnowledgeBaseOut :276-277），与老基线 `api/__init__.py` 的类定义逐字对照。
# (字段名, 是否必填) —— 顺序也钉住：pydantic 的声明顺序就是 JSON 键序。

_NOTE_OUT_FIELDS = [
    ("id", True),
    ("title", True),
    ("content", True),
    ("category", True),
    ("created_at", True),
    ("updated_at", True),
    ("source_document_id", False),
    ("tags", False),
    ("generation_meta", False),
]

_WIKI_LINK_HIT_FIELDS = [
    ("type", True),
    ("id", True),
    ("title", True),
    ("snippet", False),
    ("category", False),
    ("direction", False),
]

_GRAPH_NODE_FIELDS = [
    ("id", True),
    ("type", True),
    ("source_id", False),
    ("title", True),
    ("category", False),
    ("tags", False),
    ("kb_id", False),
    ("degree", False),
]

_GRAPH_EDGE_FIELDS = [("source", True), ("target", True), ("kind", False)]

_GRAPH_STATS_FIELDS = [
    ("notes", False),
    ("documents", False),
    ("ghosts", False),
    ("edges", False),
    ("isolated", False),
]


def _shape(model) -> list[tuple[str, bool]]:
    return [(name, field.is_required()) for name, field in model.model_fields.items()]


def test_note_out_field_table_is_verbatim():
    """``NoteOut`` 九字段：02 §7.4 的字段表是**契约**，顺序与必填性都不许漂。"""
    assert _shape(NoteOut) == _NOTE_OUT_FIELDS


def test_wiki_models_field_tables_are_verbatim():
    assert _shape(WikiLinkHit) == _WIKI_LINK_HIT_FIELDS
    assert _shape(WikiLinksOut) == [("items", False)]


def test_graph_models_field_tables_are_verbatim():
    assert _shape(GraphNode) == _GRAPH_NODE_FIELDS
    assert _shape(GraphEdge) == _GRAPH_EDGE_FIELDS
    assert _shape(GraphStats) == _GRAPH_STATS_FIELDS
    assert _shape(GraphOut) == [("nodes", False), ("edges", False), ("stats", False)]


def test_knowledge_base_out_field_table_is_verbatim():
    assert _shape(KnowledgeBaseOut) == [
        ("id", True),
        ("name", True),
        ("kind", True),
        ("path_or_url", False),
        ("collection", False),
        ("read_only", False),
        ("status", False),
        ("last_sync_at", False),
    ]


def test_wiki_hit_literals_are_narrowed_choices():
    """两个 ``Literal`` 是契约的一部分（老基线 L544/L550）：取值集合不许放宽。"""
    import typing

    assert set(typing.get_args(WikiLinkHit.model_fields["type"].annotation)) == {"note", "document"}
    assert set(typing.get_args(WikiLinkHit.model_fields["direction"].annotation)) == {"incoming", "outgoing"}
    assert set(typing.get_args(GraphNode.model_fields["type"].annotation)) == {"note", "document", "ghost"}
    assert set(typing.get_args(GraphEdge.model_fields["kind"].annotation)) == {"wiki", "source"}


@pytest.mark.asyncio
async def test_note_response_body_has_exactly_the_nine_contract_keys(client):
    """端到端：``POST /api/notes`` 的响应键集合**恰好**是那九个（不多不少）。"""
    resp = await client.post("/api/notes", json={"title": "标题", "content": "正文", "category": "分类"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert list(body.keys()) == [name for name, _ in _NOTE_OUT_FIELDS]
    assert body["source_document_id"] is None
    assert body["tags"] is None
    assert body["generation_meta"] is None


# ===========================================================================
# 二、notes CRUD · 空态 · 软删 · 错误语义
# ===========================================================================


@pytest.mark.asyncio
async def test_notes_list_is_empty_by_default(client):
    resp = await client.get("/api/notes")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_note_crud_roundtrip_and_ordering(client):
    first = (await client.post("/api/notes", json={"title": "甲", "content": "内容甲"})).json()
    second = (await client.post("/api/notes", json={"title": "乙", "content": "内容乙"})).json()

    listed = (await client.get("/api/notes")).json()
    assert [row["id"] for row in listed] == [second["id"], first["id"]], "id 倒序（新的在前）"

    updated = await client.put(f"/api/notes/{first['id']}", json={"title": "甲改", "category": "新类"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["title"] == "甲改"
    assert updated.json()["category"] == "新类"
    assert updated.json()["content"] == "内容甲", "未给的字段不动"

    filtered = (await client.get("/api/notes", params={"category": "新类"})).json()
    assert [row["id"] for row in filtered] == [first["id"]]

    deleted = await client.delete(f"/api/notes/{first['id']}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["ok"] is True
    assert "回收站" in deleted.json()["message"]

    assert [row["id"] for row in (await client.get("/api/notes")).json()] == [second["id"]]


@pytest.mark.asyncio
async def test_delete_is_soft_and_the_row_survives(client, db):
    """删除走**软删**：行还在、``deleted_at`` 非空；再删 / 再改都按不存在处理。"""
    note_id = (await client.post("/api/notes", json={"title": "甲", "content": "内容"})).json()["id"]

    assert (await client.delete(f"/api/notes/{note_id}")).status_code == 200

    # 跨会话读：`db` 上一条 SELECT（refresh）已开过事务，先结束它再读，
    # 否则 MySQL 的 REPEATABLE READ 快照会看不到 client 会话刚提交的那次软删。
    await db.rollback()
    row = await db.get(Note, note_id)
    assert row is not None, "软删不删行"
    assert row.deleted_at is not None, "Note.deleted_at 必须被置上"

    again = await client.delete(f"/api/notes/{note_id}")
    assert again.status_code == 404
    assert again.json()["detail"] == "笔记不存在"

    stale_update = await client.put(f"/api/notes/{note_id}", json={"title": "改"})
    assert stale_update.status_code == 404, "回收站里的笔记对笔记域不可见"


@pytest.mark.asyncio
async def test_missing_note_updates_and_deletes_404(client):
    assert (await client.put("/api/notes/999999", json={"title": "x"})).status_code == 404
    assert (await client.delete("/api/notes/999999")).status_code == 404
    body = (await client.put("/api/notes/999999", json={"title": "x"})).json()
    assert body["detail"] == "笔记不存在"


@pytest.mark.asyncio
async def test_notes_validation_422_for_bad_bodies(client):
    """空标题 / 空正文 / 超长标题 / 非法 id 段 → 422（不是 500，也不是静默接受）。"""
    assert (await client.post("/api/notes", json={"title": "", "content": "x"})).status_code == 422
    assert (await client.post("/api/notes", json={"title": "x", "content": ""})).status_code == 422
    assert (
        await client.post("/api/notes", json={"title": "x" * 256, "content": "x"})
    ).status_code == 422
    assert (await client.put("/api/notes/1", json={"title": ""})).status_code == 422
    # 契约用例 tests/contract/test_error_semantics.py::test_notes_invalid_id_422_or_404 钉的就是这条
    assert (await client.put("/api/notes/not_a_number", json={"title": "x"})).status_code in (404, 422)


@pytest.mark.asyncio
async def test_note_tags_and_generation_meta_are_persisted_only_when_given(client, db):
    """``tags`` / ``generation_meta`` / ``source_document_id``：给了就存，不给留 SQL NULL。"""
    created = (
        await client.post(
            "/api/notes",
            json={
                "title": "AI 草稿",
                "content": "内容",
                "source_document_id": 42,
                "tags": ["甲", "乙"],
                "generation_meta": {"model": "m"},
            },
        )
    ).json()
    assert created["source_document_id"] == 42
    assert created["tags"] == ["甲", "乙"]
    assert created["generation_meta"] == {"model": "m"}

    plain = (await client.post("/api/notes", json={"title": "手写", "content": "内容"})).json()
    assert plain["tags"] is None
    assert plain["generation_meta"] is None
    assert plain["source_document_id"] is None


# ===========================================================================
# 三、回归修复 的 B-lite：笔记进关键词路 + 范围判定
# ===========================================================================


def test_note_material_identity_never_collides_with_chunk_ids():
    """笔记材料的 ``chunk_id`` 是**负数**：与 ``chunks.id``（正自增）不可能撞号。

    这是「不动向量路」之外的**第二道**防护——即便只在关键词路里，融合的同源去重键
    （``fuse`` 按 int 去重）也会让同号的 note/chunk 合成一条、正文被顶包。
    """
    from app.retrieval.fuse import fuse
    from app.retrieval.types import Material, note_chunk_id

    note = Material(
        chunk_id=note_chunk_id(5), document_id=0, content="笔记正文", filename="笔记", note_id=5
    )
    chunk = Material(chunk_id=5, document_id=7, content="文档正文", filename="a.md", block_ordinal=0)
    assert note.chunk_id < 0 < chunk.chunk_id

    fused = fuse([chunk], [note], limit=10)
    assert len(fused) == 2, "同号会合成一条（这条断言就是防那种串号）"
    assert {item.content for item in fused} == {"笔记正文", "文档正文"}


def test_material_source_type_and_chunk_key_split_three_ways():
    """``source_type`` / ``chunk_key`` 三分支：文档 / 笔记 / 网页，逐字段钉住。"""
    from app.retrieval.types import Material

    doc = Material(chunk_id=3, document_id=7, content="x", filename="a.md", block_ordinal=2, kb_id=4)
    assert (doc.source_type, doc.chunk_key, doc.source_ref) == (
        "document",
        "kb:4:document:7:2",
        ("document", 7),
    )

    note = Material(chunk_id=-5, document_id=0, content="x", filename="n", note_id=5, kb_id=4)
    assert (note.source_type, note.chunk_key, note.source_ref) == ("note", "kb:4:note:5:0", ("note", 5))

    web = Material(chunk_id=-1, document_id=0, content="x", filename="w", url="https://e.com")
    assert web.source_type == "web"
    assert web.chunk_key.startswith("web:")
    assert web.source_ref is None, "网页材料不是库内来源，没有链接边可走"


def test_render_materials_labels_notes_not_as_documents():
    """资料区标记行按来源分流（老基线 ``build_context`` 三分支）：笔记不能标成「文档」。"""
    from app.retrieval.types import Material, number, render_materials

    items = number(
        [
            Material(chunk_id=1, document_id=7, content="a", filename="a.md", block_ordinal=0),
            Material(chunk_id=-5, document_id=0, content="b", filename="我的笔记", note_id=5),
        ]
    )
    rendered = render_materials(items)
    assert "[1]（文档：a.md）" in rendered
    assert "[2]（笔记：我的笔记）" in rendered


@pytest.mark.asyncio
async def test_keyword_path_finds_notes_when_in_scope(db):
    """关键词路取笔记的动作在**范围含笔记**时发生；范围只要文档时不取。"""
    from app.retrieval import keyword

    await _add_note(db, "向量库选型", "知澜用 Qdrant 做向量库，中文短词走 2-gram。")

    hits = await keyword.search_keywords(db, "向量库", None)
    assert [item.source_type for item in hits] == ["note"]
    assert hits[0].chunk_key.startswith("kb:1:note:")
    assert hits[0].filename == "向量库选型"

    only_docs = await keyword.search_keywords(db, "向量库", {"source_types": ["document"]})
    assert only_docs == [], "范围只要文档 ⇒ 笔记不参与（避免越范围召回）"

    only_notes = await keyword.search_keywords(db, "向量库", {"source_types": ["note"]})
    assert [item.source_type for item in only_notes] == ["note"]


@pytest.mark.asyncio
async def test_keyword_path_skips_empty_and_trashed_notes(db):
    """空正文笔记不进语料（老基线 ``bm25.py:120``）；软删笔记不进语料（条目 25）。"""
    from app.retrieval import keyword

    await _add_note(db, "空的", "   ")
    trashed = await _add_note(db, "删掉的", "向量库 相关内容")
    trashed.deleted_at = trashed.created_at
    await db.commit()

    assert await keyword.search_keywords(db, "向量库", None) == []


@pytest.mark.asyncio
async def test_single_char_query_also_reaches_notes(db):
    """1 字查询的 ``LIKE`` 兜底也要覆盖笔记——否则「仅笔记 + 1 字」会静默零命中。"""
    from app.retrieval import keyword
    from app.retrieval.keyword import FALLBACK_SCORE

    await _add_note(db, "笔记", "向量库资料")

    hits = await keyword.search_keywords(db, "向", {"source_types": ["note"]})
    assert len(hits) == 1
    assert hits[0].source_type == "note"
    assert hits[0].keyword_score == FALLBACK_SCORE


@pytest.mark.asyncio
async def test_note_only_scope_is_no_longer_a_silent_zero(client, db, use_faux):
    """``回归修复`` 的验收面：范围只含笔记时**真去检索**，笔记带 ``[n]`` 进引用。

    改造前这里必然零命中、且静默落库外直答（那才是缺陷）。现在：命中的笔记以
    ``type="note"`` 出现在 refs 里，``kind="normal"`` / ``answer_source="kb"``。
    """
    use_faux("库内回答[1]。", label="internal_db")
    note = await _add_note(db, "双链笔记", "知澜的双链用 wiki_links 表存边，标题同库解析。")

    resp = await client.post(
        "/api/chat",
        json={
            "message": "双链用 wiki_links 存边吗",
            "filters": {"source_types": ["note"]},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "normal", body
    assert body["answer_source"] == "kb"
    refs = body["refs"]
    assert refs, "范围只含笔记时必须有命中（这正是 B-lite 要消掉的静默零命中）"
    assert refs[0]["type"] == "note"
    assert refs[0]["chunk_id"] == f"kb:1:note:{note.id}:0"
    assert refs[0]["ordinal"] == 1


@pytest.mark.asyncio
async def test_unsupported_source_type_still_refuses_with_a_reason(client, use_faux):
    """不可满足的范围（库内根本没有的来源）仍按 §3.8「范围空」拒答，且说清是哪一类。

    ``回归修复`` 的 A+C 没有被 B-lite 推翻——被推翻的只是「``note`` 不可满足」这一条。
    问句带「笔记里」会命中路由正则（`doc_scope`）判库内，与老基线那条 A+C 用例同形。
    """
    use_faux("不该被调用", label="internal_db")

    resp = await client.post(
        "/api/chat", json={"message": "笔记里怎么说？", "filters": {"source_types": ["web"]}}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "refuse", body
    assert body["answer_source"] == "empty_scope"
    assert body["refs"] == []
    assert "文档" in body["message"] and "笔记" in body["message"]


@pytest.mark.asyncio
async def test_note_scope_with_a_genuine_miss_is_not_a_refusal(client, db, use_faux):
    """「范围可满足但没命中」走 §3.8 的**库内未命中**行（direct），不是「范围空」拒答。"""
    use_faux("库外直答", label="internal_db")
    await _add_note(db, "无关笔记", "完全不同的内容")

    resp = await client.post(
        "/api/chat",
        json={"message": "量子纠缠的哈密顿量", "filters": {"source_types": ["note"]}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["kind"] == "direct", resp.text


def test_includes_supported_source_only_knows_document_and_note():
    from app.retrieval.types import includes_notes, includes_supported_source

    assert includes_supported_source(None) is True
    assert includes_supported_source({}) is True
    assert includes_supported_source({"source_types": ["note"]}) is True
    assert includes_supported_source({"source_types": ["document"]}) is True
    assert includes_supported_source({"source_types": ["web"]}) is False
    assert includes_supported_source({"source_types": ["web", "note"]}) is True
    assert includes_notes({"source_types": ["document"]}) is False
    assert includes_notes({"source_types": ["note"]}) is True


# ===========================================================================
# 四、回归修复：边写入 + 一跳扩展 + 材料上限口径
# ===========================================================================


@pytest.mark.asyncio
async def test_saving_a_note_writes_wiki_edges(client, db):
    """**边写入调用点**：``POST /api/notes`` 正文里的 ``[[标题]]`` 真的落进 ``wiki_links``。

    缺陷登记的根因是「``wiki_links`` 没有任何写入者」——只补检索侧接线就等于零效果，
    所以这条断言盯的是**表里有没有行**，不是「函数被调用了」。
    """
    target = await _add_note(db, "乙", "乙的正文")
    target_id = int(target.id)  # rollback 会让 ORM 属性过期，先取出成 int

    created = await client.post("/api/notes", json={"title": "甲", "content": "详见 [[乙]] 与 [[还没写]]。"})
    assert created.status_code == 200, created.text
    source_id = created.json()["id"]

    await db.rollback()  # 结束旧事务快照，才看得到 client 会话刚提交的边
    edges = list(
        (
            await db.execute(
                select(WikiLink).where(WikiLink.source_type == "note", WikiLink.source_id == source_id)
            )
        )
        .scalars()
        .all()
    )
    by_title = {edge.target_title: edge for edge in edges}
    assert set(by_title) == {"乙", "还没写"}
    assert by_title["乙"].resolved is True
    assert by_title["乙"].target_note_id == target_id
    assert by_title["还没写"].resolved is False, "未创建的目标是「未创建」边，不是丢弃"


@pytest.mark.asyncio
async def test_renaming_a_note_re_resolves_incoming_edges(client, db):
    """改名后指向旧标题的入边按新事实重判（老基线 ``maintain_note_links(old_title=...)``）。"""
    target = await _add_note(db, "旧名", "正文")
    await client.post("/api/notes", json={"title": "引用者", "content": "见 [[旧名]]"})

    resp = await client.put(f"/api/notes/{target.id}", json={"title": "新名"})
    assert resp.status_code == 200, resp.text

    await db.rollback()  # 同上：换快照才看得到 client 会话提交的重判结果
    edges = list((await db.execute(select(WikiLink))).scalars().all())
    edge = next(edge for edge in edges if edge.target_title == "旧名")
    assert edge.resolved is False, "旧标题已无人认领 ⇒ 回到「未创建」"
    assert edge.target_note_id is None


@pytest.mark.asyncio
async def test_document_ingest_writes_its_own_edges(db):
    """文档侧边写入：入库收口（``ingest.rebuild_document_links``）把出链落库。"""
    from app.pipeline import ingest

    target = await _add_note(db, "被引用的笔记", "正文")
    doc = await _add_document(db, "a.md", content="占位")

    await ingest.rebuild_document_links(db, doc, "本文引用 [[被引用的笔记]]。")

    edges = list(
        (
            await db.execute(
                select(WikiLink).where(WikiLink.source_type == "document", WikiLink.source_id == doc.id)
            )
        )
        .scalars()
        .all()
    )
    assert [edge.target_title for edge in edges] == ["被引用的笔记"]
    assert edges[0].resolved is True and edges[0].target_note_id == target.id


@pytest.mark.asyncio
async def test_retrieve_actually_wires_the_one_hop_expansion(db, monkeypatch):
    """**接线钉**：``retrieval.retrieve()`` 真的把一跳片段并进快照。

    ``回归修复`` 原件抱怨的正是「函数在、开关在、**没有一个调用点**」——
    所以只测 ``expand_materials`` 不够，必须从 ``retrieve()`` 这个唯一入口断言一次。
    """
    from app.core.config import settings
    from app.pipeline import ingest
    from app.retrieval import retrieve

    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", True)
    monkeypatch.setattr(settings, "RAG_LINK_MAX", 4)

    source_doc = await _add_document(db, "甲.md", content="向量库选型说明，详见 [[乙]]。")
    await _add_document(db, "乙.md", content="乙的首块正文，补充向量打分细节。")
    await ingest.rebuild_document_links(db, source_doc, "向量库选型说明，详见 [[乙]]。")

    materials, rendered, warning = await retrieve(db, "向量库选型说明")
    assert warning is None
    titles = [item.filename for item in materials]
    assert titles[0] == "甲.md", "主命中在前"
    assert "乙.md" in titles, "一跳片段必须真的并进快照（这就是 023 的验收面）"

    fragment = next(item for item in materials if item.filename == "乙.md")
    seed = next(item for item in materials if item.filename == "甲.md")
    assert fragment.score < seed.score, "链接片段降权，不挤占主命中"

    # 编号连续 1..N，且资料区与快照同源（I3）
    assert [item.ordinal for item in materials] == list(range(1, len(materials) + 1))
    assert f"[{fragment.ordinal}]（文档：乙.md）" in rendered
    assert len(materials) <= 12, "总数不超过快照上限 FUSE_LIMIT"


def test_link_budget_never_pushes_materials_past_the_snapshot_cap():
    """材料上限口径：``min(RAG_LINK_MAX, FUSE_LIMIT − 主命中条数)``，主命中永不被挤掉。"""
    from app.retrieval.link import link_budget

    assert link_budget(0, cap=12, max_links=4) == 4
    assert link_budget(8, cap=12, max_links=4) == 4
    assert link_budget(10, cap=12, max_links=4) == 2, "只剩 2 个名额"
    assert link_budget(12, cap=12, max_links=4) == 0, "名额用完就不再扩展"
    assert link_budget(20, cap=12, max_links=4) == 0, "超上限不会变负"
    assert link_budget(0, cap=12, max_links=0) == 0, "开关给 0 个名额时同样不扩展"


@pytest.mark.asyncio
async def test_expand_materials_one_hop_end_to_end(db, monkeypatch):
    """一跳扩展的端到端：主命中 -> 沿边找到目标文档首块 -> 并到尾部且降权。"""
    from app.core.config import settings
    from app.pipeline import ingest
    from app.pipeline.wikilink import LINK_SCORE_FACTOR
    from app.retrieval import link as link_mod
    from app.retrieval.types import Material

    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", True)
    monkeypatch.setattr(settings, "RAG_LINK_MAX", 4)

    source_doc = await _add_document(db, "甲.md", content="正文提到 [[乙]]。")
    target_doc = await _add_document(db, "乙.md", content="乙的首块正文")
    # 手工建边（等价于入库收口写下的那条）
    await ingest.rebuild_document_links(db, source_doc, "正文提到 [[乙]]。")

    seed = Material(
        chunk_id=1,
        document_id=source_doc.id,
        content="正文提到 [[乙]]。",
        filename="甲.md",
        block_ordinal=0,
        kb_id=1,
        score=0.4,
    )
    expanded = await link_mod.expand_materials(db, [seed])
    assert len(expanded) == 2
    assert expanded[0] is seed, "主命中逐条不变"
    fragment = expanded[1]
    assert fragment.filename == "乙.md"
    assert fragment.document_id == target_doc.id
    assert fragment.content == "乙的首块正文"
    assert round(fragment.score, 6) == round(0.4 * LINK_SCORE_FACTOR, 6)


@pytest.mark.asyncio
async def test_expand_materials_respects_cap_and_switch(db, monkeypatch):
    from app.core.config import settings
    from app.retrieval import link as link_mod
    from app.retrieval.types import Material

    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", True)
    seed = Material(chunk_id=1, document_id=1, content="x", filename="a.md", block_ordinal=0)

    assert await link_mod.expand_materials(db, [seed], max_links=0) == [seed], "0 名额不扩展"
    assert await link_mod.expand_materials(db, [seed, seed], cap=2, max_links=4) == [seed, seed], "名额为 0"

    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", False)
    assert await link_mod.expand_materials(db, [seed], max_links=4) == [seed], "开关关时原样返回"


@pytest.mark.asyncio
async def test_expand_materials_is_silent_on_failure(db, monkeypatch):
    """扩展是纯增益：抛错也只记日志，主命中照常返回（I5）。"""
    from app.core.config import settings
    from app.retrieval import link as link_mod
    from app.retrieval.types import Material

    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", True)

    async def _boom(*args, **kwargs):
        raise RuntimeError("炸")

    monkeypatch.setattr(link_mod, "load_wiki_adjacency", _boom)
    seed = Material(chunk_id=1, document_id=1, content="x", filename="a.md", block_ordinal=0)
    assert await link_mod.expand_materials(db, [seed], max_links=4) == [seed]


# ===========================================================================
# 五、wiki：空与有
# ===========================================================================


@pytest.mark.asyncio
async def test_wiki_endpoints_return_empty_lists_without_parameters(client):
    """既没 id 也没 title ⇒ 空列表 + 200（不是 404：这两个参数本就都可省）。"""
    for path in ("/api/wiki/backlinks", "/api/wiki/related"):
        resp = await client.get(path)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"items": []}


@pytest.mark.asyncio
async def test_wiki_endpoints_404_for_a_missing_positive_id(client):
    assert (await client.get("/api/wiki/backlinks", params={"type": "note", "id": 999999})).status_code == 404
    body = (await client.get("/api/wiki/related", params={"type": "document", "id": 999999})).json()
    assert body["detail"] == "来源不存在"


@pytest.mark.asyncio
async def test_backlinks_lists_who_links_to_me(client, db):
    target = await _add_note(db, "乙", "乙的正文")
    await client.post("/api/notes", json={"title": "甲", "content": "详见 [[乙]]，很详细。"})

    resp = await client.get("/api/wiki/backlinks", params={"type": "note", "id": target.id})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    hit = items[0]
    assert list(hit.keys()) == [name for name, _ in _WIKI_LINK_HIT_FIELDS]
    assert hit["type"] == "note"
    assert hit["title"] == "甲"
    assert hit["direction"] == "incoming"
    assert "详见" in hit["snippet"]


@pytest.mark.asyncio
async def test_related_returns_outgoing_then_incoming(client, db):
    """一跳邻居：出链排在入链之前。

    两种入链要分开测（``wikilink.list_related`` 的去重是「出链优先」）：

    - **单向**：丙 → 枢纽（枢纽不链回）⇒ 只有 ``incoming``；
    - **互链**：枢纽 ↔ 叶子 ⇒ 两边都拉到同一个邻居，去重后**只留一条 outgoing**
      （「出链优先于入链」的老基线口径），不是两条。
    """
    from app.pipeline import wikilink

    hub = await _add_note(db, "枢纽", "枢纽正文")
    leaf = await _add_note(db, "叶子", "叶子正文")
    other = await _add_note(db, "丙", "丙正文")
    await wikilink.rebuild_source_links(db, "note", hub.id, 1, "链到 [[叶子]]")
    await wikilink.rebuild_source_links(db, "note", leaf.id, 1, "链到 [[枢纽]]")
    await wikilink.rebuild_source_links(db, "note", other.id, 1, "链到 [[枢纽]]")
    await db.commit()

    resp = await client.get("/api/wiki/related", params={"type": "note", "id": hub.id})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [(item["title"], item["direction"]) for item in items] == [
        ("叶子", "outgoing"),
        ("丙", "incoming"),
    ]


@pytest.mark.asyncio
async def test_related_dedupes_a_mutual_link_into_one_outgoing(client, db):
    """A ↔ B 互链：邻居只出现一次，方向取 ``outgoing``（出链优先）。"""
    from app.pipeline import wikilink

    hub = await _add_note(db, "枢纽", "正文")
    leaf = await _add_note(db, "叶子", "正文")
    await wikilink.rebuild_source_links(db, "note", hub.id, 1, "链到 [[叶子]]")
    await wikilink.rebuild_source_links(db, "note", leaf.id, 1, "链到 [[枢纽]]")
    await db.commit()

    items = (await client.get("/api/wiki/related", params={"type": "note", "id": hub.id})).json()["items"]
    assert [(item["title"], item["direction"]) for item in items] == [("叶子", "outgoing")]


@pytest.mark.asyncio
async def test_backlinks_hides_trashed_sources_and_targets(client, db):
    from app.pipeline import wikilink
    from app.session import repository as repo

    target = await _add_note(db, "目标", "正文")
    source = await _add_note(db, "来源", "正文")
    await wikilink.rebuild_source_links(db, "note", source.id, 1, "链到 [[目标]]")
    await db.commit()

    source.deleted_at = repo.utc_now()
    await db.commit()
    assert (await client.get("/api/wiki/backlinks", params={"type": "note", "id": target.id})).json() == {
        "items": []
    }, "回收站里的来源不进列表"

    source.deleted_at = None
    await db.commit()
    target.deleted_at = repo.utc_now()
    await db.commit()
    trashed = await client.get("/api/wiki/backlinks", params={"type": "note", "id": target.id})
    assert trashed.status_code == 404, "回收站里的目标按不存在处理"


@pytest.mark.asyncio
async def test_wiki_endpoints_resolve_by_title_when_the_id_is_zero(client, db):
    """适配点：``id=0`` 视为「没给 id」，退回标题解析（冻结前端对笔记来源就是传 0）。"""
    target = await _add_note(db, "按标题找", "正文")

    resp = await client.get(
        "/api/wiki/backlinks", params={"type": "note", "id": 0, "title": "按标题找"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": []}

    # 目标在库：标题解析真的命中了（否则上面那条会因「解析不到」而假绿）
    from app.pipeline.wikilink import resolve_title

    assert await resolve_title(db, 1, "按标题找") == ("note", target.id)


# ===========================================================================
# 六、graph：空与有
# ===========================================================================


@pytest.mark.asyncio
async def test_graph_is_empty_but_valid_on_an_empty_library(client):
    resp = await client.get("/api/graph")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "nodes": [],
        "edges": [],
        "stats": {"notes": 0, "documents": 0, "ghosts": 0, "edges": 0, "isolated": 0},
    }


@pytest.mark.asyncio
async def test_graph_builds_nodes_edges_ghosts_and_degrees(client, db):
    from app.pipeline import wikilink

    note = await _add_note(db, "甲笔记", "链到 [[乙笔记]] 与 [[从未创建]]", category="研究")
    other = await _add_note(db, "乙笔记", "乙正文")
    doc = await _add_document(db, "论文.md", content="正文")
    await wikilink.rebuild_source_links(db, "note", note.id, 1, "链到 [[乙笔记]] 与 [[从未创建]]")
    await db.commit()

    body = (await client.get("/api/graph")).json()
    nodes = {node["id"]: node for node in body["nodes"]}
    assert nodes[f"note:{note.id}"]["type"] == "note"
    assert nodes[f"note:{note.id}"]["title"] == "甲笔记"
    assert nodes[f"note:{note.id}"]["category"] == "研究"
    assert nodes[f"document:{doc.id}"]["type"] == "document"
    ghost = next(node for node in body["nodes"] if node["type"] == "ghost")
    assert ghost["title"] == "从未创建"
    assert ghost["source_id"] is None

    kinds = sorted(edge["kind"] for edge in body["edges"])
    assert kinds == ["wiki", "wiki"]
    assert body["stats"] == {
        "notes": 2,
        "documents": 1,
        "ghosts": 1,
        "edges": 2,
        "isolated": 1,
    }
    assert nodes[f"note:{note.id}"]["degree"] == 2
    assert nodes[f"note:{other.id}"]["degree"] == 1


@pytest.mark.asyncio
async def test_graph_keeps_soft_deleted_out_of_the_picture(client, db):
    """回收站里的内容不进图，且指向它的边**不转幽灵**（条目 25）。"""
    from app.pipeline import wikilink
    from app.session import repository as repo

    source = await _add_note(db, "来源", "正文")
    target = await _add_note(db, "删掉的目标", "正文")
    await wikilink.rebuild_source_links(db, "note", source.id, 1, "链到 [[删掉的目标]]")
    await db.commit()

    target.deleted_at = repo.utc_now()
    await db.commit()

    body = (await client.get("/api/graph")).json()
    assert {node["id"] for node in body["nodes"]} == {f"note:{source.id}"}
    assert body["edges"] == [], "指向回收站的边不画，也不转幽灵"
    assert body["stats"]["ghosts"] == 0
    assert body["stats"]["isolated"] == 1


# ===========================================================================
# 七、knowledge-bases：空态自举与创建边界
# ===========================================================================


@pytest.mark.asyncio
async def test_listing_knowledge_bases_seeds_the_local_one_once(client, db):
    resp = await client.get("/api/knowledge-bases")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert list(rows[0].keys()) == [name for name, _ in KnowledgeBaseOut.model_fields.items()]
    assert rows[0]["id"] == 1
    assert rows[0]["name"] == "本机知识库"
    assert rows[0]["kind"] == "local"
    assert rows[0]["read_only"] is False
    assert rows[0]["status"] == "ready"

    again = await client.get("/api/knowledge-bases")
    assert len(again.json()) == 1, "自举是惰性且幂等的，不会越列越多"
    await db.rollback()  # 换快照才数得准 client 会话刚自举出来的那一行
    total = (await db.execute(select(func.count(KnowledgeBase.id)))).scalar_one()
    assert total == 1


@pytest.mark.asyncio
async def test_create_knowledge_base_accepts_imported_and_external(client):
    created = await client.post(
        "/api/knowledge-bases",
        json={"name": "团队库", "kind": "imported", "path_or_url": "D:/kb", "collection": "team"},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["name"] == "团队库"
    assert body["kind"] == "imported"
    assert body["path_or_url"] == "D:/kb"
    assert body["collection"] == "team"
    assert body["read_only"] is True, "登记进来的库一律只读（老基线口径）"
    assert body["status"] == "ready"
    assert body["last_sync_at"] is None

    external = await client.post(
        "/api/knowledge-bases",
        json={"name": "远端库", "kind": "EXTERNAL", "path_or_url": "http://x"},
    )
    assert external.status_code == 200, external.text
    assert external.json()["kind"] == "external", "kind 大小写归一"
    assert external.json()["collection"] == "", "collection 可省 ⇒ 空串"


@pytest.mark.asyncio
async def test_create_knowledge_base_rejects_local_and_unknown_kinds(client):
    for kind in ("local", "unknown", ""):
        resp = await client.post(
            "/api/knowledge-bases", json={"name": "x", "kind": kind, "path_or_url": "p"}
        )
        assert resp.status_code == 400, f"kind={kind!r} 应 400，实际 {resp.status_code}"
        assert resp.json()["detail"] == "只能登记导入库或外部库"


@pytest.mark.asyncio
async def test_create_knowledge_base_validation_422(client):
    assert (await client.post("/api/knowledge-bases", json={"kind": "imported", "path_or_url": "p"})).status_code == 422
    assert (
        await client.post("/api/knowledge-bases", json={"name": "", "kind": "imported", "path_or_url": "p"})
    ).status_code == 422
    assert (
        await client.post(
            "/api/knowledge-bases", json={"name": "x", "kind": "imported", "path_or_url": ""}
        )
    ).status_code == 422
