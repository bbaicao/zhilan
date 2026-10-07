"""U5.1：真实 SQL 投影／批量邻接、两跳中间点和全图生命周期。"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app.core import derived_counters
from app.models import Chunk, Document, Note, WikiLink
from app.pipeline import knowledge_graph as kg, wikilink
from app.retrieval import link
from app.session import repository


async def _source(db, kind, title, *, kb_id=1, body="正文"):
    row = (
        Note(title=title, content=body, kb_id=kb_id)
        if kind == "note" else
        Document(
            filename=f"{title}.md", file_type="md", status="ready", index_status="ready",
            raw_content=body, kb_id=kb_id,
        )
    )
    db.add(row)
    await db.flush()
    if kind == "document":
        db.add(Chunk(document_id=row.id, ordinal=0, content=body))
    await db.commit()
    return row


async def _edge(db, source, target=None, *, title="", kb_id=None, resolved=True):
    row = WikiLink(
        source_type="note" if isinstance(source, Note) else "document",
        source_id=source.id, kb_id=source.kb_id if kb_id is None else kb_id,
        target_title=title or (
            target.title if isinstance(target, Note) else target.filename if target else ""
        ),
        target_note_id=target.id if isinstance(target, Note) else None,
        target_document_id=target.id if isinstance(target, Document) else None,
        resolved=resolved,
    )
    db.add(row)
    await db.commit()
    return row


def _key(row):
    return (int(row.kb_id), "note" if isinstance(row, Note) else "document", int(row.id))


@contextmanager
def _queries(engine):
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


@pytest.mark.asyncio
async def test_full_graph_reads_only_projected_fields_and_keeps_all_four_directions(db, engine):
    n1 = await _source(db, "note", "N1", body="LONG_NOTE_BODY" * 1000)
    n2 = await _source(db, "note", "N2")
    d1 = await _source(db, "document", "D1", body="LONG_DOCUMENT_BODY" * 1000)
    d2 = await _source(db, "document", "D2")
    n1.source_document_id = d1.id
    await db.commit()
    pairs = [(n1, n2), (n2, d1), (d1, n1), (d1, d2)]
    for source, target in pairs:
        await _edge(db, source, target)
    await _edge(db, n1, title="Future", resolved=False)

    with _queries(engine) as sql:
        visible = await kg.build_graph(db)

    assert len(sql) == 5
    assert all(
        "notes.content" not in statement and "documents.raw_content" not in statement
        for statement in sql
    ), "全图不加载长正文或完整 ORM"
    assert visible["edges"] == [
        {"source": kg.node_id(_key(source)[1], source.id),
         "target": kg.node_id(_key(target)[1], target.id), "kind": "wiki"}
        for source, target in pairs
    ] + [
        {"source": f"note:{n1.id}", "target": "ghost:1:Future", "kind": "wiki"},
        {"source": f"note:{n1.id}", "target": f"document:{d1.id}", "kind": "source"},
    ]
    assert visible["stats"] == {"notes": 2, "documents": 2, "ghosts": 1, "edges": 6, "isolated": 0}
    assert {node["id"]: node["degree"] for node in visible["nodes"]} == {
        f"note:{n1.id}": 4, f"note:{n2.id}": 2,
        f"document:{d1.id}": 4, f"document:{d2.id}": 1, "ghost:1:Future": 1,
    }
    with _queries(engine) as sql:
        adjacency = await kg.load_wiki_adjacency(db, [_key(n1), _key(n2), _key(d1), _key(d2)])
    assert len(sql) == 1, "多个种子共用一次边查询"
    for source, target in pairs:
        assert _key(target) in adjacency[_key(source)]
        assert _key(source) in adjacency[_key(target)]


def test_two_hops_reuse_adjacency_with_cycles_duplicate_paths_and_library_boundary():
    def edge(sid, tid, *, kb_id=1, resolved=True):
        return SimpleNamespace(
            source_type="note", source_id=sid, target_note_id=tid,
            target_document_id=None, kb_id=kb_id, resolved=resolved,
        )

    adjacency = kg.wiki_adjacency([
        edge(1, 2), edge(1, 2), edge(2, 3), edge(3, 1), edge(3, 4),
        edge(1, 1), edge(1, 5, resolved=False), edge(1, 6, kb_id=2),
    ])
    seed = (1, "note", 1)
    assert kg.wiki_neighbors(adjacency, seed) == [(1, "note", 2), (1, "note", 3)]
    assert kg.wiki_neighbors(adjacency, seed, hops=2) == [
        (1, "note", 2), (1, "note", 3), (1, "note", 4),
    ]
    with pytest.raises(ValueError):
        kg.wiki_neighbors(adjacency, seed, hops=3)


@pytest.mark.asyncio
async def test_empty_seed_does_no_sql_and_two_hops_batch_each_frontier(db, engine):
    root = await _source(db, "note", "Root")
    middle = await _source(db, "document", "Middle")
    end = await _source(db, "note", "End")
    await _edge(db, root, middle)
    await _edge(db, middle, end)
    with _queries(engine) as sql:
        assert await kg.load_wiki_adjacency(db, []) == {}
    assert sql == []
    with _queries(engine) as sql:
        adjacency = await kg.load_wiki_adjacency(db, [_key(root), _key(root)], hops=2)
    assert len([statement for statement in sql if "FROM wiki_links" in statement]) == 2
    assert len(sql) == 4, "每跳一条边查询＋一条活跃身份投影"
    assert kg.wiki_neighbors(adjacency, _key(root), hops=2) == [_key(middle), _key(end)]


@pytest.mark.asyncio
@pytest.mark.parametrize("middle_kind", ["note", "document"])
async def test_two_hops_reject_stale_deleted_or_wrong_library_middle_and_restore(db, monkeypatch, middle_kind):
    monkeypatch.setattr(link.settings, "RAG_LINK_EXPAND", True)
    root = await _source(db, "note", "Root", body="[[Middle]]")
    middle = await _source(db, middle_kind, "Middle", body="[[End]]")
    end = await _source(db, "note", "End")
    await wikilink.maintain_source_links(db, "note", root.id)
    await wikilink.maintain_source_links(db, middle_kind, middle.id, text="[[End]]")
    seed = await link._note_fragment(db, root.id, seed_score=1)

    async def two_hops():
        adjacency = await kg.load_wiki_adjacency(db, [_key(root)], hops=2)
        return kg.wiki_neighbors(adjacency, _key(root), hops=2)

    assert await two_hops() == [_key(middle), _key(end)]
    expanded = await link.expand_materials(db, [seed], max_links=1)
    assert [item.source_ref for item in expanded] == [seed.source_ref, _key(middle)[1:]]

    middle.deleted_at = repository.utc_now()
    await db.commit()  # 故意留下旧边，验证扩第二跳前回表，而不是依赖维护已成功。
    assert await two_hops() == []
    assert await link.expand_materials(db, [seed], max_links=1) == [seed]
    middle.deleted_at = None
    await db.commit()
    assert await two_hops() == [_key(middle), _key(end)]

    middle.kb_id = 2
    await db.commit()  # 边表仍写 kb=1，实际中间点已移库。
    assert await two_hops() == []
    assert await kg.load_wiki_adjacency(db, [(1, middle_kind, middle.id)], hops=2) == {
        _key(root): [], _key(end): [],
    }
    middle.kb_id = 1
    await db.commit()
    assert await two_hops() == [_key(middle), _key(end)]


@pytest.mark.asyncio
async def test_full_graph_ghost_update_delete_restore_and_real_bad_edge_counts(db, monkeypatch):
    monkeypatch.setattr(derived_counters, "_COUNTERS", {})
    watcher = await _source(db, "note", "Watcher", body="[[Future]]")
    await wikilink.maintain_source_links(db, "note", watcher.id)
    visible = await kg.build_graph(db)
    assert visible["edges"] == [{
        "source": f"note:{watcher.id}", "target": "ghost:1:Future", "kind": "wiki",
    }]
    assert derived_counters.failure_counts() == {}
    target = await _source(db, "note", "Future")
    await wikilink.maintain_source_links(db, "note", target.id)
    assert (await kg.build_graph(db))["stats"]["ghosts"] == 0
    target.deleted_at = repository.utc_now()
    await db.commit()
    await wikilink.maintain_source_links(db, "note", target.id)
    visible = await kg.build_graph(db)
    assert visible["edges"] == [] and visible["stats"]["ghosts"] == 0
    assert derived_counters.failure_counts() == {}, "回收站目标不转ghost，也不是派生失败"
    target.deleted_at = None
    await db.commit()
    await wikilink.maintain_source_links(db, "note", target.id)
    assert (await kg.build_graph(db))["edges"][0]["target"] == f"note:{target.id}"
    watcher.content = "[[New]]"
    await db.commit()
    await wikilink.maintain_source_links(db, "note", watcher.id)
    assert (await kg.build_graph(db))["edges"][0]["target"] == "ghost:1:New"
    outside = await _source(db, "note", "Outside", kb_id=2)
    await _edge(db, watcher, outside, kb_id=1)
    await _edge(db, watcher, title="", resolved=False)
    visible = await kg.build_graph(db)
    assert visible["edges"] == [{
        "source": f"note:{watcher.id}", "target": "ghost:1:New", "kind": "wiki",
    }]
    assert derived_counters.failure_counts() == {"graph_view_edges": 2}


@pytest.mark.asyncio
async def test_source_document_relationship_does_not_cross_actual_libraries(db):
    source = await _source(db, "note", "Source")
    outside = await _source(db, "document", "Outside", kb_id=2)
    source.source_document_id = outside.id
    await db.commit()
    assert (await kg.build_graph(db))["edges"] == []
    inside = await _source(db, "document", "Inside")
    source.source_document_id = inside.id
    await db.commit()
    assert (await kg.build_graph(db))["edges"] == [{
        "source": f"note:{source.id}", "target": f"document:{inside.id}", "kind": "source",
    }]
