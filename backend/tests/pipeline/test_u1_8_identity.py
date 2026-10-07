"""U1.8：统一身份、同库唯一候选、跨库 ghost 和 Markdown 业务建链。"""

import pytest
from sqlalchemy import select

from app.models import Document, Note, WikiLink
from app.pipeline import knowledge_graph, wikilink
from app.retrieval import graph, link
from app.session import repository


async def _note(db, title, *, kb_id=1, content="正文"):
    row = Note(title=title, content=content, kb_id=kb_id)
    db.add(row)
    await db.commit()
    return row


async def _document(db, filename, *, kb_id=1):
    row = Document(
        filename=filename, file_type="md", kb_id=kb_id, raw_content="正文",
        status="ready", index_status="ready",
    )
    db.add(row)
    await db.commit()
    return row


async def _edges(db, source):
    return list(
        (
            await db.execute(
                select(WikiLink).where(
                    WikiLink.source_type == "note", WikiLink.source_id == source.id,
                )
            )
        ).scalars().all()
    )


def test_code_containers_inline_runs_embed_and_title_boundary():
    text = (
        "[[正文#锚]] [[正文]] ![[嵌入]]\n"
        "`[[行内]]` ``含 ` [[多反引号]]`` [[其他]]\n"
        "`多行\n[[跨行行内]]`\n\n"
        "```python\n[[围栏]]\n```\n"
        "~~~~md\n[[波浪]]\n~~~\n[[短闭合仍代码]]\n~~~~\n\n"
        "    [[缩进]]\n\t[[制表代码]]\n\n"
        "> [[引用]]\n> ```md\n> [[引用代码]]\n> ```\n\n"
        "- ```md\n  [[列表代码]]\n  ```\n\n"
        "| [[表格]] |\n| --- |\n\n"
        "\\` [[转义反引号]]\n\n"
        "\\\\`[[偶数转义代码]]`\n\n"
        "`[[未闭合]]"
    )
    assert wikilink.extract_links(text) == ["正文", "其他", "引用", "表格", "转义反引号", "未闭合"]
    prefix = "😀" * 255
    assert wikilink.normalize_title("\ufeff\u0085 名称 #锚 ") == "名称"
    assert wikilink.normalize_title("#只有锚") == ""
    assert wikilink.extract_links(f"[[{prefix}甲]] [[{prefix}乙#锚]] [[#只有锚]]") == [prefix]
    boundary = wikilink.normalize_title("A" * 254 + " B")
    assert boundary == "A" * 254 == wikilink.normalize_title(boundary)


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["note", "document"])
async def test_ambiguity_unbinds_click_backlink_and_retrieval_then_soft_delete_restores_unique(db, collision):
    source = await _note(db, "来源", content="[[目标#段落]]")
    first = await _note(db, "目标")
    assert await wikilink.maintain_source_links(db, "note", source.id) is True
    assert await wikilink.resolve_title(db, 1, " 目标 #段落") == ("note", first.id)
    assert await wikilink.list_backlinks(db, "note", first.id)

    duplicate = (
        await _note(db, "目标") if collision == "note" else await _document(db, "目标.md")
    )
    assert await wikilink.maintain_source_links(db, collision, duplicate.id, text="正文") is True
    edge, = await _edges(db, source)
    assert (edge.target_note_id, edge.target_document_id, edge.resolved) == (None, None, False)
    assert await wikilink.resolve_title(db, 1, "目标") == (None, None)
    assert await wikilink.list_backlinks(db, "note", first.id) == []
    assert await wikilink.list_related(db, "note", source.id) == []
    assert await link.load_wiki_adjacency(db, [(1, "note", source.id)]) == {}
    node = graph._Node(kind="note", source_id=source.id, kb_id=1, title="来源", category="")
    assert await graph._wiki_neighbors(db, node) == []
    browser = await knowledge_graph.build_graph(db)
    assert any(item["id"] == "ghost:1:目标" for item in browser["nodes"])
    assert not any(item["target"] == f"note:{first.id}" for item in browser["edges"])

    duplicate.deleted_at = repository.utc_now()
    await db.commit()
    assert await wikilink.maintain_source_links(db, collision, duplicate.id) is True
    edge, = await _edges(db, source)
    assert (edge.target_note_id, edge.target_document_id, edge.resolved) == (first.id, None, True)
    assert await wikilink.resolve_title(db, 1, "目标") == ("note", first.id)
    duplicate.deleted_at = None
    await db.commit()
    assert await wikilink.maintain_source_links(db, collision, duplicate.id) is True
    assert await wikilink.resolve_title(db, 1, "目标") == (None, None)
    duplicate_id = int(duplicate.id)
    old_title = duplicate.title if collision == "note" else wikilink._stem(duplicate.filename)
    old_kb_id = int(duplicate.kb_id)
    await db.delete(duplicate)
    await db.commit()
    assert await wikilink.remove_links_for_deleted(
        db, collision, duplicate_id, old_kb_id=old_kb_id, old_title=old_title,
    ) is True
    assert await wikilink.resolve_title(db, 1, "目标") == ("note", first.id)
    edge, = await _edges(db, source)
    assert edge.target_note_id == first.id and edge.resolved is True
    assert await wikilink.list_backlinks(db, "note", first.id)


@pytest.mark.asyncio
async def test_same_title_is_scoped_before_resolution_and_ghost_identity(db):
    source1 = await _note(db, "来源一", kb_id=1, content="[[同名]] [[未创建]]")
    source2 = await _note(db, "来源二", kb_id=2, content="[[同名]] [[未创建#锚]]")
    target1 = await _note(db, "同名", kb_id=1)
    target2 = await _document(db, "同名.md", kb_id=2)
    for source in (source1, source2):
        assert await wikilink.maintain_source_links(db, "note", source.id) is True
    assert await wikilink.resolve_title(db, 1, "同名") == ("note", target1.id)
    assert await wikilink.resolve_title(db, 2, "同名") == ("document", target2.id)
    assert {item["id"] for item in await wikilink.list_backlinks(db, "note", target1.id)} == {source1.id}
    assert {item["id"] for item in await wikilink.list_backlinks(db, "document", target2.id)} == {source2.id}
    browser = await knowledge_graph.build_graph(db)
    ghosts = [item for item in browser["nodes"] if item["type"] == "ghost"]
    assert {(item["id"], item["kb_id"], item["title"]) for item in ghosts} == {
        ("ghost:1:未创建", 1, "未创建"), ("ghost:2:未创建", 2, "未创建"),
    }
    assert browser["stats"]["ghosts"] == 2


@pytest.mark.asyncio
async def test_truncated_titles_dedupe_before_storage_and_collision_is_unresolved(db):
    prefix = "😀" * 255
    first = await _note(db, prefix)
    source = await _note(db, "来源", content=f"[[{prefix}甲]] [[{prefix}乙#锚]]")
    assert await wikilink.maintain_source_links(db, "note", source.id) is True
    edge, = await _edges(db, source)
    assert (edge.target_title, edge.target_note_id, edge.resolved) == (prefix, first.id, True)
    second = await _note(db, prefix)
    assert await wikilink.maintain_source_links(db, "note", second.id) is True
    edge, = await _edges(db, source)
    assert (edge.target_title, edge.target_note_id, edge.resolved) == (prefix, None, False)
    assert await wikilink.maintain_source_links(db, "note", source.id) is True
    edge, = await _edges(db, source)
    assert await wikilink.resolve_title(db, 1, f"{prefix}甲#锚") == (None, None)
    assert edge.target_note_id is None and edge.resolved is False
