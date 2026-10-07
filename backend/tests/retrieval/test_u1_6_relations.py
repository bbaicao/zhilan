"""U1.6: four entity pairings, both directions, and strongest duplicate paths."""

from itertools import product

import pytest

from app.core.config import settings
from app.models import Note, WikiLink
from app.pipeline import knowledge_graph, wikilink
from app.retrieval import graph, link
from app.retrieval.types import Material
from tests.retrieval.conftest import seed_document


async def _source(db, kind, title, *, kb_id=1):
    if kind == "document":
        return await seed_document(db, [f"{title}正文"], filename=f"{title}.md", kb_id=kb_id, start_ordinal=0)
    note = Note(title=title, content=f"{title}正文", kb_id=kb_id)
    db.add(note)
    await db.commit()
    return note


async def _edge(db, source, target, *, kb_id=1, resolved=True):
    edge = WikiLink(
        source_type="note" if isinstance(source, Note) else "document",
        source_id=source.id,
        kb_id=kb_id,
        target_title=target.title if isinstance(target, Note) else target.filename,
        target_note_id=target.id if isinstance(target, Note) else None,
        target_document_id=target.id if not isinstance(target, Note) else None,
        resolved=resolved,
    )
    db.add(edge)
    await db.commit()
    return edge


async def _material(db, source, *, score=1.0):
    fragment = link._note_fragment if isinstance(source, Note) else link._document_fragment
    material = await fragment(db, source.id, seed_score=score / wikilink.LINK_SCORE_FACTOR)
    assert material is not None
    return material


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_kind", "target_kind", "incoming"),
    list(product(("note", "document"), ("note", "document"), (False, True))),
)
async def test_every_pair_and_direction_uses_same_neighbor(db, monkeypatch, source_kind, target_kind, incoming):
    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", True)
    source = await _source(db, source_kind, "来源")
    target = await _source(db, target_kind, "目标")
    await _edge(db, source, target)
    seed, neighbor = (target, source) if incoming else (source, target)
    seed_kind, neighbor_kind = (target_kind, source_kind) if incoming else (source_kind, target_kind)
    material = await _material(db, seed)
    neighbor_ref = (neighbor_kind, neighbor.id)
    expanded = await link.expand_materials(db, [material], max_links=1)
    assert expanded[0] is material
    assert [item.source_ref for item in expanded] == [material.source_ref, neighbor_ref]
    assert expanded[1].score == pytest.approx(material.score * wikilink.LINK_SCORE_FACTOR)
    node = graph._Node(seed_kind, seed.id, 1, "种子", "")
    assert await graph._wiki_neighbors(db, node) == [neighbor_ref]
    related = await wikilink.list_related(db, seed_kind, seed.id)
    assert [(item["type"], item["id"], item["direction"]) for item in related] == [
        (*neighbor_ref, "incoming" if incoming else "outgoing")
    ]
    backlinks = await wikilink.list_backlinks(db, seed_kind, seed.id)
    assert [(item["type"], item["id"]) for item in backlinks] == ([neighbor_ref] if incoming else [])
    visible = await knowledge_graph.build_graph(db)
    assert visible["edges"] == [{
        "source": f"{source_kind}:{source.id}",
        "target": f"{target_kind}:{target.id}",
        "kind": "wiki",
    }]


@pytest.mark.asyncio
async def test_unresolved_and_other_library_edges_do_not_leak_into_shared_query(db):
    seed = await _source(db, "note", "种子")
    unresolved = await _source(db, "document", "未解析来源")
    other_library = await _source(db, "note", "另一库来源", kb_id=2)
    await _edge(db, unresolved, seed, resolved=False)
    await _edge(db, other_library, seed, kb_id=2)
    assert await wikilink.incident_edges(db, "note", seed.id, 1) == []
    assert await wikilink.list_backlinks(db, "note", seed.id) == []
    assert await wikilink.list_related(db, "note", seed.id) == []


@pytest.mark.asyncio
async def test_link_takes_strongest_seed_before_spending_one_slot(db, monkeypatch):
    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", True)
    first = await _source(db, "note", "弱种子")
    second = await _source(db, "document", "强种子")
    target = await _source(db, "note", "共同目标")
    await _edge(db, first, target)
    await _edge(db, second, target)
    main = [await _material(db, first, score=0.2), await _material(db, second, score=0.8)]
    expanded = await link.expand_materials(db, main, cap=3, max_links=1)
    assert expanded[:2] == main
    assert len(expanded) == 3
    assert expanded[-1].source_ref == ("note", target.id)
    assert expanded[-1].score == pytest.approx(0.8 * wikilink.LINK_SCORE_FACTOR)
    assert await link.expand_materials(db, main, cap=2, max_links=1) == main


@pytest.mark.asyncio
async def test_graph_stronger_co_path_updates_existing_wiki_neighbor(monkeypatch):
    weak = graph._Node("note", 1, 1, "弱种子", "", strong=["wiki-only"], weak=["query"])
    strong = graph._Node("note", 2, 1, "强种子", "", strong=["query", "shared"])
    neighbor = graph._Node("note", 3, 1, "邻居", "", strong=["shared"])
    loads = iter(([weak, strong], [neighbor]))

    async def load_nodes(*args, **kwargs):
        return next(loads)

    async def wiki_neighbors(session, node, adjacency=None):
        return [neighbor.key] if node.key == weak.key else []

    async def load_adjacency(*args, **kwargs):
        return {}

    async def node_by_key(*args, **kwargs):
        return neighbor

    async def node_materials(session, node, score, room, exclude_ids):
        return [Material(chunk_id=-node.source_id, document_id=0, note_id=node.source_id, content="正文", score=score)]

    monkeypatch.setattr(graph, "_load_nodes", load_nodes)
    monkeypatch.setattr(graph, "load_wiki_adjacency", load_adjacency)
    monkeypatch.setattr(graph, "_wiki_neighbors", wiki_neighbors)
    monkeypatch.setattr(graph, "_node_by_key", node_by_key)
    monkeypatch.setattr(graph, "_node_materials", node_materials)
    monkeypatch.setattr(graph, "_weak_alias_weight", lambda: 0.5)
    monkeypatch.setattr(graph, "_hop_decay", lambda: 0.5)
    monkeypatch.setattr(graph, "_score_factor", lambda: 0.5)
    result = await graph._run(None, ["query"], None, 3, set())
    assert next(item for item in result if item.note_id == neighbor.source_id).score == pytest.approx(0.25)
