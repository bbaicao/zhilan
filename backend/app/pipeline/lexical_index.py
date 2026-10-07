"""U3 词项索引写维护及可重跑回填；和正文写入使用同一 SQL 事务。"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import delete, event, inspect, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lexical import pack_postings, posting_terms, term_key, tokenize, unpack_postings
from app.models import Chunk, LexicalMaterial, LexicalTerm, Note
from app.pipeline.chunking import chunk_document

INDEX_VERSION = 3
NOTE_FRAGMENT_SIZE = 800
NOTE_FRAGMENT_OVERLAP = 100
POSTING_GROUP_SIZE = 32
NOTE_SOURCE_GROUP_SIZE = 32


def note_fragments(content: str) -> list[tuple[str, int, int]]:
    """片段正文严格取自原笔记坐标；结构切块仅用于决定边界。"""
    if not (content or "").strip():
        return []
    if len(content) <= NOTE_FRAGMENT_SIZE:
        return [(content, 0, len(content))]
    source_map = [{
        "clean_start": 0, "clean_end": len(content),
        "raw_start": 0, "raw_end": len(content),
    }]
    chunks = chunk_document(
        content, size=NOTE_FRAGMENT_SIZE, overlap=NOTE_FRAGMENT_OVERLAP,
        source_map=source_map,
    )
    # ponytail: 复用现有 Markdown 边界；不复制其补围栏/表头的展示文本，避免坐标错位。
    return [
        (content[chunk.start_offset:chunk.end_offset], chunk.start_offset, chunk.end_offset)
        for chunk in chunks
    ]


def _insert_material(connection: Connection, content: str, **values) -> tuple[int, Counter]:
    body = Counter(tokenize(content))
    result = connection.execute(LexicalMaterial.__table__.insert().values(
        token_count=sum(body.values()), index_version=INDEX_VERSION, **values,
    ))
    return int(result.inserted_primary_key[0]), body


def _write_group(connection: Connection, owner_id: int, terms: dict[str, list]) -> None:
    connection.execute(delete(LexicalTerm).where(LexicalTerm.owner_material_id == owner_id))
    records = []
    keys = set()
    for term, postings in sorted(terms.items()):
        if not postings:
            continue
        key = term_key(term)
        if key in keys:
            raise ValueError("lexical long-term hash collision")
        keys.add(key)
        records.append({
            "owner_material_id": owner_id, "term_hash": key,
            "term": term if key[:1] == b"\x01" else None,
            "postings": pack_postings(postings, owner_id=owner_id),
        })
    if records:
        connection.execute(LexicalTerm.__table__.insert(), records)


def _add_terms(terms: dict[str, list], material_id: int, body: Counter, heading: Counter) -> None:
    for term in body.keys() | heading.keys():
        terms.setdefault(term, []).append((material_id, body.get(term, 0), heading.get(term, 0)))


def _chunk_group(connection: Connection, document_id: int, build_id: str | None, ordinal: int):
    start = (ordinal // POSTING_GROUP_SIZE) * POSTING_GROUP_SIZE
    return connection.execute(
        select(LexicalMaterial.id, Chunk.id.label("chunk_id"), Chunk.content)
        .join(Chunk, Chunk.id == LexicalMaterial.chunk_id)
        .where(
            Chunk.document_id == document_id, Chunk.build_id == build_id,
            Chunk.ordinal >= start, Chunk.ordinal < start + POSTING_GROUP_SIZE,
        ).order_by(LexicalMaterial.id)
    ).all()


def index_chunk(connection: Connection, chunk_id: int, content: str) -> None:
    source = connection.execute(
        select(Chunk.document_id, Chunk.build_id, Chunk.ordinal).where(Chunk.id == chunk_id)
    ).one()
    material_id = connection.execute(
        select(LexicalMaterial.id).where(LexicalMaterial.chunk_id == chunk_id)
    ).scalar_one_or_none()
    if material_id is None:
        material_id, body = _insert_material(
            connection, content or "", chunk_id=chunk_id, fragment_ordinal=0,
        )
    else:
        body = Counter(tokenize(content or ""))
        connection.execute(update(LexicalMaterial).where(LexicalMaterial.id == material_id).values(
            token_count=sum(body.values()), index_version=INDEX_VERSION,
        ))
    members = _chunk_group(connection, source.document_id, source.build_id, source.ordinal)
    owner_id = int(members[0].id)
    member_ids = {int(row.id) for row in members}
    terms = {}
    for key, long_term, payload in connection.execute(
        select(LexicalTerm.term_hash, LexicalTerm.term, LexicalTerm.postings)
        .where(LexicalTerm.owner_material_id == owner_id)
    ):
        terms[posting_terms(key, long_term)] = [
            row for row in unpack_postings(payload, owner_id=owner_id)
            if row[0] != material_id and row[0] in member_ids
        ]
    _add_terms(terms, material_id, body, Counter())
    # ponytail: posting 按来源/版本及最多32个 ordinal 分组；更新仅解码同组，
    # 不重读其他组正文。超大单块的词项量仍由该块大小决定。
    _write_group(connection, owner_id, terms)


def _read_note_bucket(connection: Connection, note_id: int):
    start = ((note_id - 1) // NOTE_SOURCE_GROUP_SIZE) * NOTE_SOURCE_GROUP_SIZE + 1
    bucket_where = [
        LexicalMaterial.note_id >= start,
        LexicalMaterial.note_id < start + NOTE_SOURCE_GROUP_SIZE,
    ]
    members = {
        int(row.id): (int(row.note_id), int(row.fragment_ordinal))
        for row in connection.execute(
            select(LexicalMaterial.id, LexicalMaterial.note_id, LexicalMaterial.fragment_ordinal)
            .where(*bucket_where).with_for_update()
        )
    }
    groups = {}
    # 必须先解码再删除旧锚；锚笔记删除或缩短不能 FK 级联掉其他笔记的 posting。
    for owner_id, key, long_term, payload in connection.execute(
        select(
            LexicalTerm.owner_material_id, LexicalTerm.term_hash,
            LexicalTerm.term, LexicalTerm.postings,
        ).join(LexicalMaterial, LexicalMaterial.id == LexicalTerm.owner_material_id)
        .where(*bucket_where)
    ):
        term = posting_terms(key, long_term)
        for material_id, body_tf, title_tf in unpack_postings(payload, owner_id=owner_id):
            if material_id in members and members[material_id][0] != note_id:
                group = members[material_id][1] // POSTING_GROUP_SIZE
                groups.setdefault(group, {}).setdefault(term, []).append(
                    (material_id, body_tf, title_tf)
                )
    connection.execute(delete(LexicalTerm).where(
        LexicalTerm.owner_material_id.in_(select(LexicalMaterial.id).where(*bucket_where))
    ))
    return members, groups


def _write_note_bucket(connection: Connection, members: dict, groups: dict) -> None:
    owners = {}
    for material_id, (_note_id, ordinal) in members.items():
        group = ordinal // POSTING_GROUP_SIZE
        owners[group] = min(owners.get(group, material_id), material_id)
    for group, owner_id in owners.items():
        _write_group(connection, owner_id, groups.get(group, {}))


def index_note(connection: Connection, note_id: int, title: str, content: str) -> int:
    fragments = note_fragments(content or "")
    heading = Counter(tokenize(title or ""))
    members, groups = _read_note_bucket(connection, note_id)
    old_ids = {
        ordinal: material_id for material_id, (source_id, ordinal) in members.items()
        if source_id == note_id
    }
    for material_id in old_ids.values():
        members.pop(material_id)
    for ordinal, (body, start, end) in enumerate(fragments):
        material_id = old_ids.pop(ordinal, None)
        if material_id is None:
            material_id, freqs = _insert_material(
                connection, body, note_id=note_id,
                fragment_ordinal=ordinal, span_start=start, span_end=end,
            )
        else:
            freqs = Counter(tokenize(body))
            connection.execute(update(LexicalMaterial).where(LexicalMaterial.id == material_id).values(
                token_count=sum(freqs.values()), span_start=start, span_end=end,
                index_version=INDEX_VERSION,
            ))
        members[material_id] = (note_id, ordinal)
        _add_terms(groups.setdefault(ordinal // POSTING_GROUP_SIZE, {}), material_id, freqs, heading)
    if old_ids:
        connection.execute(delete(LexicalMaterial).where(LexicalMaterial.id.in_(list(old_ids.values()))))
    # ponytail: 最多32个来源共享词项键，每来源的32片段仍分组；只复用兄弟 posting，
    # 不重读或重新分词兄弟正文。单个超长笔记的组数随其正文长度增长。
    _write_note_bucket(connection, members, groups)
    return len(fragments)


@event.listens_for(Chunk, "after_insert")
def _chunk_insert(_mapper, connection, target) -> None:
    index_chunk(connection, target.id, target.content)


@event.listens_for(Chunk, "after_update")
def _chunk_update(_mapper, connection, target) -> None:
    if inspect(target).attrs.content.history.has_changes():
        index_chunk(connection, target.id, target.content)


@event.listens_for(Chunk, "after_delete")
def _chunk_delete(_mapper, connection, target) -> None:
    members = _chunk_group(connection, target.document_id, target.build_id, target.ordinal)
    if not members:
        return
    # 删除组首块时 FK 已删其 posting；用保留材料重新锚定，避免丢兄弟块或留旧 ID。
    connection.execute(delete(LexicalTerm).where(
        LexicalTerm.owner_material_id.in_([row.id for row in members])
    ))
    terms = {}
    for row in members:
        _add_terms(terms, row.id, Counter(tokenize(row.content or "")), Counter())
    _write_group(connection, int(members[0].id), terms)


@event.listens_for(Note, "after_insert")
def _note_insert(_mapper, connection, target) -> None:
    index_note(connection, target.id, target.title, target.content)


@event.listens_for(Note, "after_update")
def _note_update(_mapper, connection, target) -> None:
    state = inspect(target)
    if any(state.attrs[field].history.has_changes() for field in ("title", "content")):
        index_note(connection, target.id, target.title, target.content)


@event.listens_for(Note, "before_delete")
def _note_delete(_mapper, connection, target) -> None:
    members, groups = _read_note_bucket(connection, target.id)
    members = {key: value for key, value in members.items() if value[0] != target.id}
    # 同 flush 的下一条 before_delete 不能把已安排删除的材料重新选为锚。
    connection.execute(delete(LexicalMaterial).where(LexicalMaterial.note_id == target.id))
    _write_note_bucket(connection, members, groups)


def backfill_connection(
    connection: Connection, *, batch_size: int = 200,
    after_chunk_id: int = 0, after_note_id: int = 0,
) -> dict[str, int]:
    """keyset 分页重建派生词项；幂等、历史构建保留，调用者决定提交/回滚。

    游标表示本次已完成的来源行；显式从 0 重跑可修复任意缺失词项。
    直接 SQL 导入不经过 ORM 事件，须在相同事务内显式调用本入口。
    """
    batch_size = max(1, int(batch_size))
    result = {
        "chunk_count": 0, "note_count": 0, "fragment_count": 0,
        "last_chunk_id": int(after_chunk_id), "last_note_id": int(after_note_id),
    }
    while True:
        rows = connection.execute(
            select(Chunk.id, Chunk.content).where(Chunk.id > result["last_chunk_id"])
            .order_by(Chunk.id).limit(batch_size)
        ).all()
        if not rows:
            break
        for row in rows:
            index_chunk(connection, row.id, row.content)
            result["chunk_count"] += 1
            result["last_chunk_id"] = row.id
    while True:
        rows = connection.execute(
            select(Note.id, Note.title, Note.content).where(Note.id > result["last_note_id"])
            .order_by(Note.id).limit(batch_size)
        ).all()
        if not rows:
            break
        for row in rows:
            result["fragment_count"] += index_note(connection, row.id, row.title, row.content)
            result["note_count"] += 1
            result["last_note_id"] = row.id
    return result


async def backfill(session: AsyncSession, **kwargs) -> dict[str, int]:
    return await session.run_sync(
        lambda sync_session: backfill_connection(sync_session.connection(), **kwargs)
    )
