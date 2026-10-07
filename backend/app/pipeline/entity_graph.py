"""U5 isolated, offline entity trial. No extractor, database, config or global graph.

Callers supply explicitly labelled original evidence. These checks prove identity,
traceability and source maintenance, not the semantic accuracy of the labels.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from threading import RLock
from types import MappingProxyType
from typing import Mapping


def content_hash(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True)
class EvidenceBlock:
    kb_id: int
    source_type: str
    source_id: int
    chunk_id: int
    block_ordinal: int
    page: int | None
    span_start: int
    span_end: int
    content: str
    content_sha256: str
    build_id: str
    model: str
    version: str
    filename: str = ""
    category: str = ""
    evidence_kind: str = "original"

    @property
    def source_ref(self) -> tuple[str, int]:
        return self.source_type, self.source_id

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.build_id, self.model, self.version


@dataclass(frozen=True)
class Support:
    chunk_id: int
    start: int
    end: int
    quote: str


@dataclass(frozen=True)
class Entity:
    key: str
    label: str
    support: Support
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Relation:
    subject: str
    object: str
    predicate: str
    support: Support
    time: str | None = None
    negated: bool = False

    @property
    def key(self) -> tuple:
        # Never sort the endpoints: direction, time and negation are identity.
        return self.subject, self.object, self.predicate, self.time, self.negated


@dataclass(frozen=True)
class SourceBatch:
    source_type: str
    source_id: int
    blocks: tuple[EvidenceBlock, ...]
    entities: tuple[Entity, ...]
    relations: tuple[Relation, ...]

    @property
    def source_ref(self) -> tuple[str, int]:
        return self.source_type, self.source_id


def support_from_quote(source: SourceBatch, chunk_id: int, quote: str) -> Support:
    """Locate an unchanged, unambiguous quote in one original source block."""
    if not isinstance(source, SourceBatch) or type(chunk_id) is not int:
        raise ValueError("support must reference an original block in the same source")
    _text(quote, "support.quote")
    matches = [
        block for block in source.blocks
        if isinstance(block, EvidenceBlock) and type(block.chunk_id) is int
        and block.chunk_id == chunk_id
    ]
    if len(matches) != 1:
        raise ValueError("support must reference exactly one original block in the same source")
    block = matches[0]
    if block.source_ref != source.source_ref or block.evidence_kind != "original":
        raise ValueError("support must reference an original block in the same source")
    start = block.content.find(quote)
    if start < 0 or block.content.find(quote, start + 1) >= 0:
        raise ValueError("support quote must occur exactly once in its original block")
    return Support(chunk_id, start, start + len(quote), quote)


@dataclass(frozen=True)
class GraphSnapshot:
    sources: Mapping[tuple[str, int], SourceBatch]
    blocks: Mapping[int, EvidenceBlock]
    entities: Mapping[str, tuple[Entity, ...]]
    relations: Mapping[tuple, tuple[Relation, ...]]
    adjacency: Mapping[str, tuple[tuple, ...]]


def _support(support: Support, blocks: dict[int, EvidenceBlock]) -> EvidenceBlock:
    if not isinstance(support, Support) or type(support.chunk_id) is not int or support.chunk_id not in blocks:
        raise ValueError("support must reference an original block in the same source")
    block = blocks[support.chunk_id]
    _integer(support.start, "support.start")
    _integer(support.end, "support.end", 1)
    if not support.start < support.end <= len(block.content):
        raise ValueError("support span is outside the original block")
    if not isinstance(support.quote, str) or support.quote != block.content[support.start:support.end]:
        raise ValueError("support quote does not match the original span")
    return block


class EntityGraph:
    """One explicit isolated knowledge base, capped by the U0 trial protocol."""

    def __init__(self, kb_id: int):
        _integer(kb_id, "kb_id", 1)
        self.kb_id = kb_id
        self._lock = RLock()
        self._snapshot = self._compile({})

    @property
    def snapshot(self) -> GraphSnapshot:
        return self._snapshot

    def _validate(self, batch: SourceBatch) -> None:
        if not isinstance(batch, SourceBatch) or batch.source_type not in ("document", "note"):
            raise ValueError("trial requires an explicit document/note source")
        _integer(batch.source_id, "source_id", 1)
        if any(type(items) is not tuple for items in (batch.blocks, batch.entities, batch.relations)):
            raise ValueError("source batch collections must be immutable tuples")
        if not batch.blocks:
            raise ValueError("source batch requires original evidence")
        blocks = {}
        ordinals = set()
        identities = set()
        for block in batch.blocks:
            if not isinstance(block, EvidenceBlock) or block.kb_id != self.kb_id:
                raise ValueError("cross-knowledge-base evidence is forbidden")
            if block.source_ref != batch.source_ref or block.evidence_kind != "original":
                raise ValueError("block source mismatch or non-original evidence")
            _integer(block.kb_id, "block.kb_id", 1)
            _integer(block.source_id, "block.source_id", 1)
            if type(block.chunk_id) is not int or (
                block.source_type == "document" and block.chunk_id <= 0
            ) or (block.source_type == "note" and block.chunk_id != -block.source_id):
                raise ValueError("invalid real document/note block identity")
            _integer(block.block_ordinal, "block_ordinal")
            if block.page is not None:
                _integer(block.page, "page", 1)
            _integer(block.span_start, "span_start")
            _integer(block.span_end, "span_end", 1)
            _text(block.content, "content")
            if block.span_end - block.span_start != len(block.content):
                raise ValueError("original source span must match content length")
            if block.content_sha256 != content_hash(block.content):
                raise ValueError("original content hash mismatch")
            for name in ("build_id", "model", "version"):
                _text(getattr(block, name), name)
            if block.chunk_id in blocks or block.block_ordinal in ordinals:
                raise ValueError("duplicate original block identity")
            blocks[block.chunk_id] = block
            ordinals.add(block.block_ordinal)
            identities.add(block.identity)
        if len(identities) != 1:
            raise ValueError("one source batch must use one build/model/version")
        entities = {}
        for entity in batch.entities:
            if not isinstance(entity, Entity):
                raise ValueError("invalid entity record")
            _text(entity.key, "entity.key")
            _text(entity.label, "entity.label")
            block = _support(entity.support, blocks)
            if entity.key in entities or entity.label not in entity.support.quote:
                raise ValueError("duplicate entity key or label absent from its original span")
            if type(entity.aliases) is not tuple:
                raise ValueError("aliases must be immutable explicit attributes")
            for alias in entity.aliases:
                _text(alias, "alias")
                if alias not in block.content:
                    raise ValueError("alias absent from its original block")
            entities[entity.key] = entity
        for relation in batch.relations:
            if not isinstance(relation, Relation):
                raise ValueError("invalid relation record")
            _text(relation.subject, "relation.subject")
            _text(relation.object, "relation.object")
            _text(relation.predicate, "predicate")
            if relation.time is not None:
                _text(relation.time, "time")
            if type(relation.negated) is not bool:
                raise ValueError("negation must be an explicit boolean")
            _support(relation.support, blocks)
            if relation.subject not in entities or relation.object not in entities:
                raise ValueError("relation endpoints need explicit identities in the same source")
            for key in (relation.subject, relation.object):
                entity = entities[key]
                if not any(label in relation.support.quote for label in (entity.label, *entity.aliases)):
                    raise ValueError("relation endpoint absent from its original span")

    def _compile(self, sources: dict) -> GraphSnapshot:
        for batch in sources.values():
            self._validate(batch)
        if len(sources) > 100 or sum(
            len(block.content) for batch in sources.values() for block in batch.blocks
        ) > 100_000:
            raise ValueError("U0 isolated trial limit exceeded (100 sources/100000 characters)")
        if len({(block.model, block.version) for batch in sources.values() for block in batch.blocks}) > 1:
            raise ValueError("one isolated graph must use one model/version configuration")
        blocks, entities, relations, adjacency = {}, defaultdict(list), defaultdict(list), defaultdict(list)
        for batch in sources.values():
            for block in batch.blocks:
                if block.chunk_id in blocks:
                    raise ValueError("block identity reused by a different source")
                blocks[block.chunk_id] = block
            for entity in batch.entities:
                if entities[entity.key] and entities[entity.key][0].label != entity.label:
                    raise ValueError("conflicting canonical labels for an explicit entity key")
                if entity not in entities[entity.key]:
                    entities[entity.key].append(entity)
            for relation in batch.relations:
                if relation not in relations[relation.key]:
                    relations[relation.key].append(relation)
        for key in relations:
            for endpoint in set(key[:2]):
                adjacency[endpoint].append(key)
        return GraphSnapshot(
            MappingProxyType(dict(sources)), MappingProxyType(blocks),
            MappingProxyType({key: tuple(value) for key, value in entities.items()}),
            MappingProxyType({key: tuple(value) for key, value in relations.items()}),
            MappingProxyType({key: tuple(value) for key, value in adjacency.items()}),
        )

    def replace_source(self, batch: SourceBatch) -> None:
        """Validate the complete next snapshot before atomically replacing a source."""
        with self._lock:
            if not isinstance(batch, SourceBatch):
                raise ValueError("invalid source batch")
            self._validate(batch)
            # ponytail: rebuild <=100 sources/100000 chars; a measured larger trial
            # should replace this with incremental support indexes, not remove checks.
            candidate = {**self._snapshot.sources, batch.source_ref: batch}
            self._snapshot = self._compile(candidate)

    def delete_source(self, source_type: str, source_id: int) -> None:
        if source_type not in ("document", "note"):
            raise ValueError("invalid source_type")
        _integer(source_id, "source_id", 1)
        with self._lock:
            candidate = dict(self._snapshot.sources)
            candidate.pop((source_type, source_id), None)
            self._snapshot = self._compile(candidate)

    def export(self) -> dict:
        return {
            "kb_id": self.kb_id, "schema_version": 1,
            "sources": [asdict(batch) for _, batch in sorted(self._snapshot.sources.items())],
        }

    def json_bytes(self) -> bytes:
        return json.dumps(self.export(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
