"""Explicit U5 experiment only; production retrieve() never imports this module."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Mapping

from app.pipeline.entity_graph import EntityGraph, EvidenceBlock, content_hash
from app.retrieval.link import relevant_materials
from app.retrieval import rerank
from app.retrieval.types import FUSE_LIMIT, MATERIAL_ITEM_MAX_CHARS, Material, number
from app.retrieval.windows import focus_window


def _material(block: EvidenceBlock, score: float) -> Material:
    return Material(
        chunk_id=block.chunk_id,
        document_id=block.source_id if block.source_type == "document" else 0,
        note_id=block.source_id if block.source_type == "note" else None,
        content=block.content, filename=block.filename, category=block.category,
        kb_id=block.kb_id, block_ordinal=block.block_ordinal, page=block.page,
        span_start=block.span_start, span_end=block.span_end, score=score,
    )


def expand_candidates(
    question: str, main: list[Material], *, graph: EntityGraph | None = None,
    enabled: bool = False,
    current_sources: Mapping[tuple[str, int], tuple[str, str, str]] | None = None,
    hops: int = 1, max_edges: int = 24, max_candidates: int = 4,
    max_characters: int = 12_000,
) -> tuple[list[Material], dict]:
    """Use verified recalled blocks as seeds; edges only nominate original blocks.

    current_sources is the explicitly allowed query scope and active build/model/
    version manifest. Ambiguous names are never used to choose an entity.
    """
    stats = {"enabled": enabled, "edges_visited": 0, "candidates": 0,
             "gate_removed": 0, "characters": 0, "budget_stopped": False}
    if not enabled or graph is None:
        return [], stats
    if type(hops) is not int or hops not in (1, 2):
        raise ValueError("trial hops must be one or two")
    for value in (max_edges, max_candidates, max_characters):
        if type(value) is not int or value < 0:
            raise ValueError("trial budgets must be nonnegative integers")
    if not current_sources:
        raise ValueError("trial requires an explicit active source scope")
    for ref, identity in current_sources.items():
        if (type(ref) is not tuple or len(ref) != 2 or ref[0] not in ("document", "note")
                or type(ref[1]) is not int or ref[1] <= 0
                or type(identity) is not tuple or len(identity) != 3
                or not all(isinstance(value, str) and value.strip() for value in identity)):
            raise ValueError("trial active scope requires exact source/build/model/version identities")
    if not main or not all((max_edges, max_candidates, max_characters)):
        return [], stats
    snapshot = graph.snapshot
    valid = {
        chunk_id: block for chunk_id, block in snapshot.blocks.items()
        if current_sources.get(block.source_ref) == block.identity
    }
    seed_blocks = {
        item.chunk_id for item in main
        if all(type(value) is int for value in (
            item.chunk_id, item.document_id, item.kb_id, item.block_ordinal, item.span_start, item.span_end,
        ))
        and (item.note_id is None or type(item.note_id) is int)
        and (item.page is None or type(item.page) is int)
        and isinstance(item.content, str)
        and (block := valid.get(item.chunk_id)) is not None
        and not item.is_web and item.kb_id == graph.kb_id
        and item.source_ref == block.source_ref
        and item.block_ordinal == block.block_ordinal and item.page == block.page
        and item.span_start == block.span_start and item.span_end == block.span_end
        and content_hash(item.content) == block.content_sha256
    }
    seeds = [
        key for key, mentions in snapshot.entities.items()
        if any(mention.support.chunk_id in seed_blocks for mention in mentions)
    ]
    queue = deque((key, 0) for key in seeds)
    visited, edges, candidate_blocks = set(seeds), set(), []
    existing = {item.retrieval_key for item in main}
    seen_blocks = set()
    while queue:
        entity, depth = queue.popleft()
        if depth >= hops:
            continue
        for key in snapshot.adjacency.get(entity, ()):
            if key in edges:
                continue
            if len(edges) >= max_edges:
                stats["budget_stopped"] = True
                break
            edges.add(key)
            supported = False
            for relation in snapshot.relations[key]:
                block = valid.get(relation.support.chunk_id)
                if block is None:
                    continue
                supported = True
                if block.chunk_id in seen_blocks or _material(block, 0).retrieval_key in existing:
                    continue
                seen_blocks.add(block.chunk_id)
                if stats["characters"] + len(block.content) > max_characters:
                    stats["budget_stopped"] = True
                    continue
                candidate_blocks.append(block)
                stats["characters"] += len(block.content)
                if len(candidate_blocks) >= max_candidates:
                    stats["budget_stopped"] = True
                    break
            if supported:
                for target in key[:2]:
                    if target not in visited:
                        visited.add(target)
                        queue.append((target, depth + 1))
            if len(candidate_blocks) >= max_candidates:
                break
        if len(edges) >= max_edges or len(candidate_blocks) >= max_candidates:
            break
    stats["edges_visited"] = len(edges)
    # The U3 gate tests original text. Graph reachability itself is never evidence
    # that a relation or a multi-hop business inference answers the question.
    score = max((float(item.score or 0) for item in main), default=0) * 0.5
    candidates = [_material(block, score) for block in candidate_blocks]
    kept = relevant_materials(question, candidates)
    stats["candidates"] = len(candidates)
    stats["gate_removed"] = len(candidates) - len(kept)
    return kept, stats


async def rank_trial(
    question: str, main: list[Material], *,
    graph: EntityGraph | None = None, enabled: bool = False,
    current_sources: Mapping[tuple[str, int], tuple[str, str, str]] | None = None,
    hops: int = 1, max_edges: int = 24, max_candidates: int = 4,
    max_characters: int = 12_000, cap: int = FUSE_LIMIT,
    rerank_call=None,
) -> tuple[list[Material], dict]:
    """Both trial paths use identical windows and exactly one shared rerank call."""
    if type(cap) is not int or not 0 <= cap <= FUSE_LIMIT:
        raise ValueError("trial cap must be within the frozen citation limit")
    if len(main) > cap:
        raise ValueError("trial cap cannot discard a primary retrieval hit")
    if len({item.retrieval_key for item in main}) != len(main):
        raise ValueError("trial main retrieval keys must be unique")
    extras, stats = expand_candidates(
        question, main, graph=graph, enabled=enabled, current_sources=current_sources,
        hops=hops, max_edges=max_edges, max_candidates=max_candidates,
        max_characters=max_characters,
    )
    main_keys = {item.retrieval_key for item in main}
    candidates = []
    for item in [*main, *extras]:
        body, start, end = focus_window(item.content, question, MATERIAL_ITEM_MAX_CHARS)
        base = item.span_start
        candidates.append(replace(
            item, content=body, windowed=True,
            span_start=base + start if base is not None else None,
            span_end=base + end if base is not None else None,
        ))
    rank_stats = rerank.RerankStats()
    ranked = await (rerank_call or rerank.apply_rerank)(question, candidates, stats=rank_stats)
    primary = [item for item in ranked if item.retrieval_key in main_keys]
    extra = [item for item in ranked if item.retrieval_key not in main_keys]
    extra = relevant_materials(question, extra)
    kept = primary + extra[:max(0, cap - len(primary))]
    stats.update({"kept": len(kept), "extra_kept": len(kept) - len(primary),
                  "rerank_status": rank_stats.status, "business_inferences": 0})
    return number(kept), stats
