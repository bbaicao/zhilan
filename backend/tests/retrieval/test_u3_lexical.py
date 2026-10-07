"""U3 最小集成判据：词项维护、范围统计、片段身份和故障事务。"""

from collections import Counter
import zlib
import pytest
from sqlalchemy import delete, event, func, select

from app.core.lexical import pack_postings, posting_terms, term_key, tokenize, unpack_postings
from app.models import Chunk, LexicalMaterial, LexicalTerm, Note
from app.pipeline import lexical_index
from app.retrieval import keyword
from app.session import note_service, repository


def test_compact_postings_roundtrip_and_term_keys_preserve_full_tokens():
    rows = [(301, 2, 0), (305, 1000, 1), (70001, 0, 2)]
    payload = pack_postings(rows, owner_id=301)
    assert unpack_postings(payload, owner_id=301) == rows
    assert pack_postings([(301, 2, 0)], owner_id=301)[:1] == b"\x00"
    repeated = [(index, 1, 0) for index in range(1, 33)]
    compressed = pack_postings(repeated, owner_id=1)
    assert compressed == b"\x02\x00\x20\x01\x00"
    assert unpack_postings(compressed, owner_id=1) == repeated
    old_raw = b"\x00\x01\x00" + b"\x01\x01\x00" * 31
    assert unpack_postings(b"\x00" + old_raw, owner_id=1) == repeated
    assert unpack_postings(b"\x01" + zlib.compress(old_raw), owner_id=1) == repeated
    mixed = [(index, 2, 0) for index in range(301, 304)] + [
        (index, 0, 1000) for index in range(305, 308)
    ]
    assert unpack_postings(pack_postings(mixed, owner_id=301), owner_id=301) == mixed
    for term in ("向量", "budget2026", "x" * 600):
        key = term_key(term)
        assert len(key) <= 33
        assert posting_terms(key, term if key[:1] == b"\x01" else None) == term
    assert term_key("x" * 600) != term_key("x" * 599 + "y")
    with pytest.raises(ValueError, match="invalid lexical term key"):
        posting_terms(term_key("x" * 600), "different")
    with pytest.raises(ValueError, match="truncated lexical posting"):
        unpack_postings(b"\x00\x80", owner_id=1)
    with pytest.raises(ValueError, match="invalid lexical posting"):
        pack_postings([(1, 1, 0), (1, 2, 0)], owner_id=1)
    with pytest.raises(ValueError, match="invalid lexical posting"):
        unpack_postings(b"\x02\x00\x00\x01\x00", owner_id=1)
    with pytest.raises(ValueError, match="truncated lexical posting"):
        unpack_postings(b"\x02\x00\x20\x01", owner_id=1)
    with pytest.raises(ValueError, match="count exceeds limit"):
        unpack_postings(b"\x02\x00\x81\x80\x04\x01\x00", owner_id=1)
    with pytest.raises(ValueError, match="invalid lexical posting compression"):
        unpack_postings(b"\x01" + zlib.compress(old_raw) + b"tail", owner_id=1)


@pytest.mark.asyncio
async def test_index_score_matches_body_formula_and_query_does_not_retokenize(db, seed, monkeypatch):
    texts = ["向量检索 向量检索 2026", "向量 数据 检索 Qdrant", "完全无关的资料"]
    await seed(db, texts)
    tokens = [tokenize(text) for text in texts]
    dfs = Counter(term for row in tokens for term in set(row))
    avg = sum(map(len, tokens)) / len(tokens)
    query = set(tokenize("向量检索"))
    calls = []
    original = keyword.tokenize

    def only_query(text):
        calls.append(text)
        assert text == "向量检索", "查询期不允许对任何语料正文重新分词"
        return original(text)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("生产查询不允许退回全正文语料加载器")

    monkeypatch.setattr(keyword, "tokenize", only_query)
    monkeypatch.setattr(keyword, "_load_corpus", forbidden)
    monkeypatch.setattr(keyword, "_load_note_corpus", forbidden)
    hits = await keyword.search_keywords(db, "向量检索")
    assert calls == ["向量检索"]
    for hit in hits:
        index = texts.index(hit.content)
        raw = keyword._bm25_raw(query, Counter(tokens[index]), len(tokens[index]), avg, dfs, 3)
        expected = raw * keyword.weighted_overlap(query, set(tokens[index])) / keyword.query_weight(query)
        if keyword._has_phrase(hit.content, keyword.segments("向量检索")):
            expected *= keyword.PHRASE_BONUS
        assert hit.keyword_score == pytest.approx(expected, rel=1e-12)
    assert hits[0].content == texts[0]


@pytest.mark.asyncio
async def test_dense_chinese_query_reads_full_materials_only_for_final_limit(db, seed, monkeypatch):
    doc = await seed(db, ["检索证据🙂。" * 15] * 80 + ["检索 数据 证据。" * 15] * 20)
    original = keyword._candidate_materials
    read_ids = []

    async def final_only(session, ids, **kwargs):
        read_ids.extend(ids)
        assert len(ids) == 3
        assert kwargs == {"documents": True, "notes": False}
        return await original(session, ids, **kwargs)

    monkeypatch.setattr(keyword, "_candidate_materials", final_only)
    rank_plans = []

    def inspect_plan(connection, _cursor, statement, parameters, _context, _many):
        if (
            not statement.startswith("EXPLAIN")
            and "instr(" in statement and "lexical_materials.token_count" in statement
        ):
            rank_plans.append([
                row[3] for row in connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN " + statement, parameters,
                )
            ])

    sqlite_engine = db.bind.sync_engine if db.bind.dialect.name == "sqlite" else None
    if sqlite_engine is not None:
        event.listen(sqlite_engine, "before_cursor_execute", inspect_plan)
    try:
        hits = await keyword.search_keywords(
            db, "检索证据", {"source_types": ["document"]}, limit=3,
        )
    finally:
        if sqlite_engine is not None:
            event.remove(sqlite_engine, "before_cursor_execute", inspect_plan)
    if sqlite_engine is not None:
        assert rank_plans
        assert all(
            "SEARCH lexical_materials USING INTEGER PRIMARY KEY" in plan[0] for plan in rank_plans
        ), rank_plans
    assert len(hits) == len(set(read_ids)) == 3
    assert all(hit.document_id == doc.id and keyword._has_phrase(hit.content, ["检索证据"]) for hit in hits)
    first_ids = list((await db.execute(
        select(Chunk.id).where(Chunk.document_id == doc.id).order_by(Chunk.id).limit(3)
    )).scalars())
    assert [hit.chunk_id for hit in hits] == first_ids


@pytest.mark.asyncio
async def test_tag_scope_keeps_local_statistics_and_excludes_other_libraries(db, seed):
    await seed(db, ["向量检索"], kb_id=1, tags=["指定"], filename="only.md")
    baseline = await keyword.search_keywords(db, "向量检索", {"tags": ["指定"]})
    await seed(db, ["向量检索 干扰资料 " * 200] * 24, kb_id=2, tags=["其他"])
    same = await keyword.search_keywords(db, "向量检索", {"tags": ["指定"]})
    assert [(hit.chunk_id, hit.keyword_score) for hit in same] == [
        (hit.chunk_id, hit.keyword_score) for hit in baseline
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["document", "note"])
async def test_single_char_tags_are_applied_before_limit(db, seed, source):
    if source == "document":
        await seed(db, ["库"] * 30, tags=["其他"], filename="off-scope.md")
        target = await seed(db, ["库"], tags=["指定"], filename="in-scope.md")
        expected = target.id
    else:
        db.add_all([Note(title=f"其他{i}", content="库", tags=["其他"]) for i in range(30)])
        target = Note(title="指定", content="库", tags=["指定"])
        db.add(target)
        await db.commit()
        expected = target.id
    hits = await keyword.search_keywords(
        db, "库", {"source_types": [source], "tags": ["指定"]}, limit=1,
    )
    assert len(hits) == 1
    assert (hits[0].document_id if source == "document" else hits[0].note_id) == expected


@pytest.mark.asyncio
async def test_single_char_tag_id_batches_keep_global_stable_order(db):
    notes = [Note(title=str(i), content="库", tags=["指定"]) for i in range(402)]
    db.add_all(notes)
    await db.commit()
    hits = await keyword.search_keywords(
        db, "库", {"source_types": ["note"], "tags": ["指定"]}, limit=2,
    )
    assert [hit.note_id for hit in hits] == [notes[0].id, notes[1].id]


@pytest.mark.asyncio
async def test_long_note_returns_matching_source_fragment_with_unicode_coordinates(db):
    content = (
        "# 首节\n" + "普通叙述🙂。" * 200
        + "\n# 中段\n中段定位词 uniqueneedle 预算 2026。\n" + "补充事实。" * 180
        + "\n# 末节\n" + "最后说明。" * 200
    )
    note = Note(title="长笔记", content=content)
    short = Note(title="短笔记", content="短笔记正文保持原样。")
    db.add_all([note, short])
    await db.commit()
    hits = await keyword.search_keywords(db, "uniqueneedle", {"source_types": ["note"]})
    assert hits
    for hit in hits:
        assert hit.note_id == note.id and hit.chunk_id == -note.id
        assert hit.fragment_ordinal is not None
        assert 0 < hit.span_start < hit.span_end < len(content)
        assert hit.content == content[hit.span_start:hit.span_end]
        assert "uniqueneedle" in hit.content
        assert len(hit.content) < len(content)
    short_hit = (await keyword.search_keywords(db, "保持原样", {"source_types": ["note"]}))[0]
    assert short_hit.content == short.content
    assert (short_hit.fragment_ordinal, short_hit.span_start, short_hit.span_end) == (0, 0, len(short.content))


@pytest.mark.asyncio
async def test_note_title_is_bounded_and_cannot_create_bodyless_match(db):
    notes = [
        Note(title="向量检索", content="向量检索的正文说明"),
        Note(title="一般标题", content="向量检索的正文说明"),
        Note(title="向量检索", content="无关会议安排"),
    ]
    db.add_all(notes)
    await db.commit()
    hits = await keyword.search_keywords(db, "向量检索", {"source_types": ["note"]})
    assert [hit.note_id for hit in hits] == [notes[0].id, notes[1].id]
    assert hits[0].keyword_score / hits[1].keyword_score == pytest.approx(1 + keyword.NOTE_TITLE_WEIGHT)


@pytest.mark.asyncio
async def test_same_title_notes_have_separate_fragment_identity_and_scope(db):
    notes = [
        Note(title="同名", content="向量检索 " * 350, kb_id=kb_id)
        for kb_id in (1, 2)
    ]
    db.add_all(notes)
    await db.commit()
    for note in notes:
        hits = await keyword.search_keywords(
            db, "向量检索", {"source_types": ["note"], "kb_ids": [note.kb_id]},
        )
        assert {hit.note_id for hit in hits} == {note.id}
        assert len({hit.retrieval_key for hit in hits}) == len(hits) > 1


@pytest.mark.asyncio
async def test_note_lifecycle_replaces_terms_and_keeps_restore_available(db, monkeypatch):
    note = await note_service.create_note(db, title="before", content="oldsignal 正文")
    note_id = note.id
    assert await keyword.search_keywords(db, "oldsignal")
    await note_service.update_note(db, note_id, title="after", content="newsignal 新正文")
    assert not await keyword.search_keywords(db, "oldsignal")
    assert await keyword.search_keywords(db, "newsignal")
    before = (await db.execute(select(LexicalMaterial.id).where(
        LexicalMaterial.note_id == note_id
    ))).scalars().all()
    note = await db.get(Note, note_id)
    note.tags = ["新标签"]
    with monkeypatch.context() as patch:
        def no_metadata_rebuild(*_args, **_kwargs):
            raise AssertionError("仅标签变更不允许重建正文词项")
        patch.setattr(lexical_index, "index_note", no_metadata_rebuild)
        await db.commit()
    assert (await db.execute(select(LexicalMaterial.id).where(
        LexicalMaterial.note_id == note_id
    ))).scalars().all() == before, "仅元数据变更不重建正文词项"
    await note_service.soft_delete_note(db, note_id)
    assert not await keyword.search_keywords(db, "newsignal")
    assert await repository.restore_trashed_row(db, "note", note_id)
    await db.commit()
    assert await keyword.search_keywords(db, "newsignal", {"tags": ["新标签"]})
    await repository.hard_delete_note(db, note_id)
    await db.commit()
    assert (await db.scalar(select(func.count()).select_from(LexicalMaterial))) == 0
    assert (await db.scalar(select(func.count()).select_from(LexicalTerm))) == 0


@pytest.mark.asyncio
async def test_note_bucket_member_scope_batch_scores_and_owner_lifecycle(db, monkeypatch):
    owner = Note(id=1, title="同名", content="alpha beta 桶首正文", kb_id=2, tags=[])
    target = Note(
        id=2, title="同名", kb_id=1, tags=["目标"],
        content=("无关前文。\n" * 180) + "alpha beta 中段证据。\n" + ("无关尾文。\n" * 180),
    )
    sibling = Note(id=3, title="同名", content="alpha 仅部分正文", kb_id=1, tags=["目标"])
    survivor = Note(id=4, title="同名", content="alpha beta 第三篇保留", kb_id=1, tags=["目标"])
    next_bucket = Note(id=33, title="同名", content="alpha beta 另一桶", kb_id=1, tags=["目标"])
    db.add_all([owner, target, sibling, survivor, next_bucket])
    await db.commit()
    scope = {"source_types": ["note"], "kb_ids": [1], "tags": ["目标"]}
    sources = [("note", note.id) for note in (target, sibling, survivor, next_bucket)]
    hits = await keyword.search_keywords(db, "alpha beta", scope, limit=100)
    assert {hit.note_id for hit in hits} == {2, 3, 4, 33}
    assert any(hit.note_id == 2 and hit.span_start > 0 for hit in hits)
    assert set((await db.execute(select(LexicalMaterial.note_id).where(
        LexicalMaterial.id.in_(select(LexicalTerm.owner_material_id))
    ))).scalars()) == {1, 33}, "范围外桶首只作存储锚，不能让成员漏查"
    snapshot = dict(scope)
    for question in ("alpha beta", "文"):
        batched = await keyword.search_source_keywords(db, question, sources, scope)
        for key in sources:
            independent = await keyword.search_keywords(
                db, question, {**scope, "_link_source": key}, limit=1,
            )
            assert bool(independent) == (key in batched)
            if independent:
                expected, actual = independent[0], batched[key]
                assert (
                    actual.retrieval_key, actual.content, actual.keyword_score,
                    actual.span_start, actual.span_end,
                ) == (
                    expected.retrieval_key, expected.content, expected.keyword_score,
                    expected.span_start, expected.span_end,
                ), "批量目标不可合并 N/df/平均长度或改变单字首片段"
    assert scope == snapshot
    await note_service.soft_delete_note(db, owner.id)
    assert {hit.note_id for hit in await keyword.search_keywords(
        db, "alpha beta", scope, limit=100,
    )} == {2, 3, 4, 33}
    assert await repository.restore_trashed_row(db, "note", owner.id)
    await db.commit()
    original = lexical_index.tokenize
    calls = []

    def only_changed_note(text):
        calls.append(text)
        assert text in ("新锚标题", "ownersignal 独立正文"), "更新锚不可重新分词兄弟正文"
        return original(text)

    with monkeypatch.context() as patch:
        patch.setattr(lexical_index, "tokenize", only_changed_note)
        owner.title, owner.content = "新锚标题", "ownersignal 独立正文"
        await db.commit()
    assert len(calls) == 2
    await repository.hard_delete_note(db, owner.id)
    await db.commit()
    assert {hit.note_id for hit in await keyword.search_keywords(
        db, "alpha beta", scope, limit=100,
    )} == {2, 3, 4, 33}
    # 同一 ORM flush 删除当前锚和第二篇；不能重锚到已待删行而吞第三篇词项。
    await db.delete(target)
    await db.delete(sibling)
    await db.commit()
    assert {hit.note_id for hit in await keyword.search_keywords(
        db, "alpha beta", scope, limit=100,
    )} == {4, 33}
    valid_ids = set((await db.execute(select(LexicalMaterial.id))).scalars())
    for group_owner, payload in (await db.execute(
        select(LexicalTerm.owner_material_id, LexicalTerm.postings)
    )):
        assert group_owner in valid_ids
        assert all(member in valid_ids for member, _body, _title in unpack_postings(
            payload, owner_id=group_owner,
        ))
    before = [
        (hit.retrieval_key, hit.keyword_score)
        for hit in await keyword.search_keywords(db, "alpha beta", scope)
    ]
    for _ in range(2):
        rebuilt = await lexical_index.backfill(db, batch_size=1)
        await db.commit()
        assert rebuilt["note_count"] == rebuilt["fragment_count"] == 2
        assert [
            (hit.retrieval_key, hit.keyword_score)
            for hit in await keyword.search_keywords(db, "alpha beta", scope)
        ] == before


@pytest.mark.asyncio
async def test_chunk_updates_and_hard_delete_rebuild_and_cascade(db, seed):
    doc = await seed(db, ["oldsignal 原正文"])
    chunk = await db.scalar(select(Chunk).where(Chunk.document_id == doc.id))
    chunk.content = "newsignal 新正文"
    await db.commit()
    assert not await keyword.search_keywords(db, "oldsignal")
    assert await keyword.search_keywords(db, "newsignal")
    await db.delete(doc)
    await db.commit()
    assert (await db.scalar(select(func.count()).select_from(LexicalMaterial))) == 0
    assert (await db.scalar(select(func.count()).select_from(LexicalTerm))) == 0


@pytest.mark.asyncio
async def test_group_owner_update_delete_and_ordinal_boundary_preserve_sibling_terms(db, seed):
    doc = await seed(db, ["shared signalold", "shared signalsibling"] + ["shared later"] * 31)
    chunks = list((await db.execute(
        select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.ordinal)
    )).scalars())
    groups = (await db.execute(
        select(LexicalTerm.owner_material_id).distinct()
    )).scalars().all()
    assert len(groups) == 2
    chunk_id = chunks[1].id
    material_id = await db.scalar(select(LexicalMaterial.id).where(LexicalMaterial.chunk_id == chunk_id))
    chunks[1].content = "shared signalchanged"
    await db.commit()
    assert await db.scalar(select(LexicalMaterial.id).where(
        LexicalMaterial.chunk_id == chunk_id
    )) == material_id
    assert not await keyword.search_keywords(db, "signalsibling")
    assert [hit.chunk_id for hit in await keyword.search_keywords(db, "signalchanged")] == [chunk_id]
    await db.delete(chunks[0])
    await db.commit()
    assert not await keyword.search_keywords(db, "signalold")
    assert [hit.chunk_id for hit in await keyword.search_keywords(db, "signalchanged")] == [chunk_id]
    valid_ids = set((await db.execute(select(LexicalMaterial.id))).scalars())
    for owner, payload in (await db.execute(
        select(LexicalTerm.owner_material_id, LexicalTerm.postings)
    )):
        assert owner in valid_ids
        assert all(row[0] in valid_ids for row in unpack_postings(payload, owner_id=owner))
    assert {hit.chunk_id for hit in await keyword.search_keywords(db, "shared", limit=100)} == {
        chunk.id for chunk in chunks[1:]
    }


@pytest.mark.asyncio
async def test_index_write_failure_rolls_back_source_and_previous_terms(db, monkeypatch):
    note = Note(title="原题", content="oldsignal 正文")
    db.add(note)
    await db.commit()
    note_id = note.id
    original = lexical_index._write_group

    def fail_after_old_index_deleted(*args, **kwargs):
        raise RuntimeError("injected lexical write failure")

    monkeypatch.setattr(lexical_index, "_write_group", fail_after_old_index_deleted)
    note.content = "newsignal 正文"
    with pytest.raises(RuntimeError, match="injected lexical write failure"):
        await db.commit()
    await db.rollback()
    monkeypatch.setattr(lexical_index, "_write_group", original)
    stored = await db.get(Note, note_id, populate_existing=True)
    assert stored.content == "oldsignal 正文"
    assert await keyword.search_keywords(db, "oldsignal")
    assert not await keyword.search_keywords(db, "newsignal")


@pytest.mark.asyncio
async def test_explicit_backfill_repairs_missing_index_and_is_rerunnable(db, seed):
    await seed(db, ["向量检索"])
    db.add_all([Note(title="笔记", content="向量检索"), Note(title="空", content="")])
    await db.commit()
    await db.execute(delete(LexicalMaterial))
    await db.commit()
    assert await keyword.search_keywords(db, "向量检索") == []
    first = await lexical_index.backfill(db, batch_size=1)
    await db.commit()
    count = await db.scalar(select(func.count()).select_from(LexicalTerm))
    scores = [(row.retrieval_key, row.keyword_score) for row in await keyword.search_keywords(db, "向量检索")]
    second = await lexical_index.backfill(db, batch_size=1)
    await db.commit()
    assert first == second
    assert first["chunk_count"] == 1 and first["note_count"] == 2 and first["fragment_count"] == 1
    assert await db.scalar(select(func.count()).select_from(LexicalTerm)) == count
    assert [(row.retrieval_key, row.keyword_score) for row in await keyword.search_keywords(db, "向量检索")] == scores


@pytest.mark.asyncio
async def test_long_english_terms_are_not_truncated_to_same_posting(db, seed):
    prefix = "x" * 600
    left, right = prefix + "left", prefix + "right"
    await seed(db, [left, right])
    assert [row.content for row in await keyword.search_keywords(db, left)] == [left]
    assert [row.content for row in await keyword.search_keywords(db, right)] == [right]
