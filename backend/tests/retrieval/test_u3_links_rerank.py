"""U3.6：新扩链有正文依据，同一窗口批量重排，最终主命中优先。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

import app.retrieval as retrieval
from app.core.config import settings
from app.models import Chunk, Note, WikiLink
from app.retrieval import keyword, link, rerank, vector, windows
from app.retrieval.types import Material
from tests.retrieval.conftest import seed_document


@pytest.fixture(autouse=True)
def offline_settings(monkeypatch):
    monkeypatch.setattr(settings, "RAG_LINK_EXPAND", True)
    monkeypatch.setattr(settings, "RAG_LINK_MAX", 4)
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", False)
    monkeypatch.setattr(settings, "RAG_MIN_RERANK_SCORE", 0.2)
    monkeypatch.setattr(settings, "GRAPHRAG_ENABLE", False)
    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", "")


def material(serial, body="alpha 保修"):
    return Material(
        chunk_id=serial, document_id=serial, content=body, score=1 / serial,
        filename=f"材料{serial}.md",
    )


def pipeline(monkeypatch, main, extra, *, scores=None, fail=False, enabled=True):
    seen = {"rerank": [], "windows": [], "links": []}

    async def keywords(*args, **kwargs):
        return main

    async def vectors(*args, **kwargs):
        return [], None

    async def expand(session, materials, **kwargs):
        seen["links"].append(kwargs)
        stats = kwargs["stats"]
        stats.links = {
            "enabled": True, "candidates": len(extra), "kept": len(extra),
            "gate_removed": 0, "target_skipped": 0, "degraded": False,
            "gate": "keyword", "candidate_cap": kwargs["cap"],
        }
        return materials + extra

    async def expand_windows(session, materials, question, filters=None):
        seen["windows"].append((question, filters, [item.chunk_id for item in materials]))
        return [
            replace(item, content=item.content + "\n共同窗口", covered_ordinals=(item.block_ordinal,))
            for item in materials
        ]

    async def call(config, question, documents):
        seen["rerank"].append((question, documents))
        if fail:
            raise RuntimeError("离线模拟重排失败")
        return scores

    monkeypatch.setattr(keyword, "search_keywords", keywords)
    monkeypatch.setattr(vector, "search_vectors", vectors)
    monkeypatch.setattr(link, "expand_materials", expand)
    monkeypatch.setattr(windows, "expand_windows", expand_windows)
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", enabled)
    monkeypatch.setattr(rerank, "resolve_rerank_config", lambda: SimpleNamespace(model="offline"))
    monkeypatch.setattr(rerank, "_call_rerank", call)
    return seen


@pytest.mark.asyncio
async def test_all_candidates_share_one_window_and_rerank_but_links_cannot_displace_main(monkeypatch):
    main = [material(serial) for serial in range(1, 13)]
    extra = [material(serial) for serial in range(20, 24)]
    seen = pipeline(monkeypatch, main, extra, scores=[0.3] * 12 + [0.99] * 4)
    stats = rerank.RerankStats()
    scope = {"kb_ids": [1], "tags": ["保修"]}

    result, text, _ = await retrieval.retrieve(object(), "alpha", scope, stats=stats)

    assert [item.chunk_id for item in result] == list(range(1, 13))
    assert len(seen["windows"]) == len(seen["rerank"]) == 1
    assert len(seen["rerank"][0][1]) == 16
    assert all(body.endswith("共同窗口") for body in seen["rerank"][0][1])
    assert seen["links"][0]["question"] == "alpha"
    assert seen["links"][0]["filters"] == scope
    assert seen["links"][0]["cap"] == 16
    assert seen["windows"][0][1] == scope
    assert stats.threshold_applied is True
    assert stats.links["candidates"] == 4 and stats.links["kept"] == 0
    assert "[12]" in text and "[13]" not in text


@pytest.mark.asyncio
async def test_links_obey_the_same_threshold_as_main(monkeypatch):
    pipeline(monkeypatch, [material(1)], [material(20)], scores=[0.8, 0.1])
    stats = rerank.RerankStats()

    result, _, _ = await retrieval.retrieve(object(), "alpha", stats=stats)

    assert [item.chunk_id for item in result] == [1]
    assert stats.removed == 1 and stats.threshold_applied is True
    assert stats.links["candidates"] == 1 and stats.links["kept"] == 0


@pytest.mark.asyncio
async def test_success_below_threshold_returns_empty_without_a_floor(monkeypatch):
    pipeline(monkeypatch, [material(1)], [material(20)], scores=[0.1, 0.1])
    stats = rerank.RerankStats()

    result, text, _ = await retrieval.retrieve(object(), "alpha", stats=stats)

    assert result == [] and text == ""
    assert stats.empty_reason == "below_threshold"
    assert stats.links["kept"] == 0


@pytest.mark.asyncio
async def test_prefetched_links_fill_only_the_slots_released_by_main_threshold(monkeypatch):
    main = [material(serial) for serial in range(1, 13)]
    extra = [material(serial) for serial in range(20, 24)]
    seen = pipeline(monkeypatch, main, extra, scores=[0.8] * 10 + [0.1] * 2 + [0.99] * 4)
    stats = rerank.RerankStats()

    result, _, _ = await retrieval.retrieve(object(), "alpha", stats=stats)

    assert [item.chunk_id for item in result] == list(range(1, 11)) + [20, 21]
    assert [item.ordinal for item in result] == list(range(1, 13))
    assert len(seen["rerank"]) == 1 and stats.links["kept"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True], ids=["disabled", "failed"])
async def test_rerank_degradation_preserves_main_but_requires_link_lexical_evidence(monkeypatch, fail):
    main = [material(1, "向量主命中"), material(2, "另一个主命中")]
    extra = [material(20, "alpha 保修"), material(21, "完全无关的天气")]
    seen = pipeline(monkeypatch, main, extra, fail=fail, enabled=fail)
    stats = rerank.RerankStats()

    result, _, _ = await retrieval.retrieve(object(), "alpha", stats=stats)

    assert [item.chunk_id for item in result] == [1, 2, 20]
    assert stats.status == ("failed" if fail else "disabled")
    assert stats.threshold_applied is False
    assert stats.links["gate"] == "keyword" and stats.links["gate_removed"] == 1
    assert stats.links["degraded"] is fail
    assert len(seen["rerank"]) == int(fail)


async def edge(db, source, target):
    is_note = isinstance(target, Note)
    db.add(WikiLink(
        source_type="document", source_id=source.id, kb_id=1, resolved=True,
        target_title=target.title if is_note else target.filename,
        target_note_id=target.id if is_note else None,
        target_document_id=None if is_note else target.id,
    ))
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["document", "note"])
async def test_production_links_select_real_middle_hits_and_reject_edge_only_targets(db, target_kind):
    source = await seed_document(db, ["alpha 种子"], filename="种子.md", start_ordinal=0)
    # 较早的强命中不能抢走目标来源在 LIMIT 前的名额。
    await seed_document(db, ["alpha " * 20], filename="干扰源.md", start_ordinal=0)
    if target_kind == "document":
        target = await seed_document(
            db, ["没有关键词的开头", "alpha 保修证据在第二块"],
            filename="目标.md", start_ordinal=0,
        )
    else:
        target = Note(
            title="目标", kb_id=1,
            content=("无关说明。\n" * 200) + "\nalpha 保修证据在笔记中部\n" + ("无关结尾。\n" * 200),
        )
        db.add(target)
        await db.commit()
    irrelevant = await seed_document(db, ["边指向此文但内容无关"], filename="无关.md", start_ordinal=0)
    await edge(db, source, target)
    await edge(db, source, irrelevant)
    seed = Material(chunk_id=1, document_id=source.id, content="alpha 种子", score=0.8)
    stats = rerank.RerankStats()

    result = await link.expand_materials(db, [seed], question="alpha", stats=stats)

    assert len(result) == 2 and result[0] is seed
    hit = result[1]
    assert hit.source_ref == (target_kind, target.id)
    assert "alpha" in hit.content and hit.keyword_score > 0
    assert hit.score == pytest.approx(0.4)
    if target_kind == "document":
        assert hit.block_ordinal == 1
        actual_id = (await db.execute(
            select(Chunk.id).where(Chunk.document_id == target.id, Chunk.ordinal == 1)
        )).scalar_one()
        assert hit.chunk_id == actual_id
    else:
        assert hit.fragment_ordinal > 0 and hit.span_start > 0
    assert stats.links["candidates"] == 1 and stats.links["target_skipped"] == 1
    legacy = await link.expand_materials(db, [seed], question="", max_links=1)
    assert len(legacy) == 2 and "alpha" not in legacy[1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["document", "note"])
async def test_single_character_source_filter_is_applied_before_limit(db, kind):
    await seed_document(db, ["字干扰"], filename="先到文档.md", start_ordinal=0)
    first = Note(title="先到笔记", content="字干扰", kb_id=1)
    db.add(first)
    await db.commit()
    if kind == "document":
        target = await seed_document(db, ["字目标"], filename="目标.md", start_ordinal=0)
    else:
        target = Note(title="目标笔记", content="字目标", kb_id=1)
        db.add(target)
        await db.commit()

    scope = {"source_types": [kind], "_link_source": (kind, target.id)}
    result = await keyword.search_keywords(db, "字", scope, limit=1)

    assert len(result) == 1 and result[0].source_ref == (kind, target.id)
    assert link.relevant_materials("字", result) == result


@pytest.mark.asyncio
async def test_link_hit_uses_current_version_and_keeps_the_original_scope(db):
    target = await seed_document(db, ["alpha 历史正文"], filename="版本.md", start_ordinal=0)
    target.content_build_id = "current"
    db.add(Chunk(
        document_id=target.id, build_id="current", ordinal=0,
        content="alpha 当前正文", page=7,
    ))
    await db.commit()
    scope = {"kb_ids": [1], "source_types": ["document"]}
    snapshot = {key: list(value) for key, value in scope.items()}

    result = await link._document_fragment(
        db, target.id, question="alpha", seed_score=0.8, filters=scope,
    )

    assert result is not None and result.content == "alpha 当前正文"
    assert result.page == 7 and scope == snapshot
    assert await link._document_fragment(
        db, target.id, question="alpha", seed_score=0.8, filters={"kb_ids": [2]},
    ) is None


@pytest.mark.asyncio
async def test_high_degree_links_batch_all_targets_and_keep_a_late_scoped_middle_hit(db, monkeypatch):
    source = await seed_document(db, ["alpha 种子"], filename="种子.md", start_ordinal=0)
    unrelated = [
        Note(title=f"alpha 仅标题{index}", content="天气旅游，与问题无关。", kb_id=1)
        for index in range(140)
    ]
    outside = Note(title="范围外", content="alpha 正文命中但属于别库。", kb_id=2)
    target = Note(
        title="后部相关目标", kb_id=1,
        content=("无关前文。\n" * 180) + "alpha 保修的中段证据。\n" + ("无关尾文。\n" * 180),
    )
    targets = unrelated + [outside, target]
    db.add_all(targets)
    await db.commit()
    db.add_all([
        WikiLink(
            source_type="document", source_id=source.id, kb_id=1, resolved=True,
            target_title=note.title, target_note_id=note.id,
        )
        for note in targets
    ])
    await db.commit()
    real_batch = keyword.search_source_keywords
    batches = []

    async def batch(session, question, sources, filters=None):
        batches.append((list(sources), filters))
        return await real_batch(session, question, sources, filters)

    async def no_single_source_search(*args, **kwargs):
        raise AssertionError("高连接度扩链不得为每个目标重复发起独立词项检索")

    statements = []

    def count_sql(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    monkeypatch.setattr(keyword, "search_source_keywords", batch)
    monkeypatch.setattr(keyword, "search_keywords", no_single_source_search)
    sync_engine = db.get_bind()
    event.listen(sync_engine, "before_cursor_execute", count_sql)
    scope = {"kb_ids": [1], "source_types": ["note"]}
    stats = rerank.RerankStats()
    seed = Material(
        chunk_id=1, document_id=source.id, content="alpha 种子", kb_id=1, score=0.8,
    )
    try:
        result = await link.expand_materials(
            db, [seed], question="alpha", filters=scope, max_links=1, stats=stats,
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", count_sql)

    assert len(batches) == 1
    assert batches[0] == ([("note", note.id) for note in targets], scope)
    assert scope == {"kb_ids": [1], "source_types": ["note"]}
    assert len(statements) <= 20, "无命中目标数不能变成同数量的数据库往返"
    assert len(result) == 2 and result[0] is seed
    hit = result[1]
    assert hit.source_ref == ("note", target.id)
    assert hit.fragment_ordinal > 0 and hit.span_start > 0
    assert "alpha 保修的中段证据" in hit.content and hit.keyword_score > 0
    assert hit.score == pytest.approx(0.4)
    assert stats.links["candidates"] == stats.links["kept"] == 1
    assert stats.links["target_skipped"] == 141 and stats.links["degraded"] is False
