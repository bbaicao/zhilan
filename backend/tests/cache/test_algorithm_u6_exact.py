"""U6 精确缓存：实际模型身份、完整来源、在途读写；复用真实 SQLite 测试库。"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import chat_cache, chat_routes
from app.cache import entry as cache_entry, store as cache_store
from app.core.config import settings
from app.models import Chunk, Note
from app.retrieval.types import Material
from tests.retrieval.conftest import seed_document


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    cache_store.reset_cache()
    cache_store.reset_failure_counts()
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 86400)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 16)
    yield
    cache_store.reset_cache()
    cache_store.reset_failure_counts()


def _key(**changes):
    inputs = dict(
        question="这份项目谁负责？", filters={"kb_ids": [1]}, agent_mode="off",
        skill_ids=[], model_label="openai/model", memory_text="", history=[],
        has_images=False, retrieval_config={},
        retrieval_query="项目负责人", rewrite_history=[], source_type="internal_db",
    )
    inputs.update(changes)
    return cache_entry.build_key(cache_entry.CacheKeyInputs(**inputs))


@pytest.mark.parametrize("role", ["chat", "embed", "rerank"])
def test_actual_endpoint_identity_changes_key_without_hashing_secret(monkeypatch, role):
    services = [{
        "id": name, "name": name, "provider": "openai",
        "base_url": f"https://{name}.invalid/v1", "api_key": "synthetic-only",
        "models": [f"{name}-model"], "model_roles": {f"{name}-model": [name]},
        "for_roles": [name],
    } for name in ("chat", "embed", "rerank")]
    monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({
        name: {"service_id": name, "model": f"{name}-model"}
        for name in ("chat", "embed", "rerank")
    }))
    monkeypatch.setattr(settings, "MODEL_SERVICES", json.dumps(services))
    monkeypatch.setattr(settings, "CHAT_REASONING", "")
    first = cache_entry.retrieval_fingerprint()
    before = _key(retrieval_config=first)
    target = next(item for item in services if item["id"] == role)
    target["api_key"] = "different-synthetic-secret"
    monkeypatch.setattr(settings, "MODEL_SERVICES", json.dumps(services))
    assert _key(retrieval_config=cache_entry.retrieval_fingerprint()) == before
    target["base_url"] = f"https://other-{role}.invalid/v1"
    monkeypatch.setattr(settings, "MODEL_SERVICES", json.dumps(services))
    assert _key(retrieval_config=cache_entry.retrieval_fingerprint()) != before
    assert "synthetic" not in json.dumps(first)


@pytest.mark.parametrize("change", [
    {"filters": {"kb_ids": [2]}},
    {"source_type": "external"},
    {"history": [{"role": "user", "content": "另一个项目"}]},
    {"rewrite_history": [{"role": "user", "content": "另一个项目"}]},
    {"retrieval_query": "另一个项目负责人"},
    {"memory_text": "用户偏好改变"},
])
def test_exact_scope_history_rewrite_source_and_memory_are_separate(change):
    assert _key(**change) != _key()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["neighbor", "build", "scope", "source", "delete"])
async def test_window_sources_all_participate_in_liveness(db, mutation):
    document = await seed_document(
        db, ["开头原文", "中段回答证据", "窗口外邻块原文"], start_ordinal=0,
    )
    chunks = (await db.execute(
        select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.ordinal)
    )).scalars().all()
    material = Material(
        chunk_id=chunks[1].id, document_id=document.id, content="中段回答证据",
        filename=document.filename, block_ordinal=1, windowed=True,
        covered_ordinals=(0, 1, 2), span_start=400, span_end=900,
    )
    snapshots = await cache_entry.capture_fingerprints(db, [material])
    assert snapshots and len(snapshots) == 3
    assert await cache_entry.fingerprints_live(db, snapshots)
    if mutation == "neighbor":
        chunks[2].content += "发生修改"
    elif mutation == "build":
        document.active_build_id = "different-published-build"
    elif mutation == "scope":
        document.kb_id = 2
    elif mutation == "source":
        document.filename = "新来源名称.md"
    else:
        document.deleted_at = datetime(2026, 10, 4)
    await db.commit()
    assert not await cache_entry.fingerprints_live(db, snapshots)
    if mutation == "delete":
        document.deleted_at = None
        await db.commit()
        cache_store.invalidate_all("restore")
        # 完全恢复原源行可以重新建条目；被失效的旧缓存不会复活。
        assert await cache_entry.fingerprints_live(db, snapshots)
        assert cache_store.cache_size() == 0


@pytest.mark.asyncio
async def test_two_note_windows_share_full_source_and_reject_edit_outside_window(db):
    note = Note(title="长笔记", content="前段" + "甲" * 1800 + "中段证据" + "乙" * 1800)
    db.add(note)
    await db.commit()
    materials = [Material(
        chunk_id=-note.id, document_id=0, note_id=note.id, filename=note.title,
        content=content, span_start=start, span_end=start + len(content),
        fragment_ordinal=ordinal, windowed=True,
    ) for ordinal, (content, start) in enumerate([("前段", 0), ("中段证据", 1802)])]
    snapshots = await cache_entry.capture_fingerprints(db, materials)
    assert snapshots and list(snapshots) == [f"source:note:{note.id}"]
    assert await cache_entry.fingerprints_live(db, snapshots)
    note.content += "尾部新增"
    await db.commit()
    assert not await cache_entry.fingerprints_live(db, snapshots)


@pytest.mark.asyncio
async def test_missing_covered_source_refuses_snapshot_and_empty_sources_are_valid(db):
    document = await seed_document(db, ["唯一块"], start_ordinal=0)
    chunk = (await db.execute(select(Chunk).where(Chunk.document_id == document.id))).scalar_one()
    material = Material(
        chunk_id=chunk.id, document_id=document.id, content=chunk.content,
        filename=document.filename, covered_ordinals=(0, 9),
    )
    assert await cache_entry.capture_fingerprints(db, [material]) is None
    assert await cache_entry.capture_fingerprints(db, []) == {}
    assert await cache_entry.fingerprints_live(db, {})


@pytest.mark.asyncio
async def test_invalidation_during_lookup_rejects_old_snapshot(monkeypatch):
    await cache_store.store_snapshot("old", cache_entry.CacheEntry(raw_answer="旧答案"))

    async def invalidate_while_reading(_db, _fingerprints):
        cache_store.invalidate_all("concurrent-source-change")
        return True

    monkeypatch.setattr(cache_entry, "fingerprints_live", invalidate_while_reading)
    assert await cache_store.lookup(None, "old") is None
    assert cache_store.failure_counts()["hit"] == 0


@pytest.mark.parametrize("original,computed,snapshots,expected", [
    ("prepared", "prepared", {}, 1),
    ("prepared", "changed", {}, 0),
    (None, "prepared", {}, 0),
    ("prepared", "prepared", None, 0),
])
def test_write_uses_prepared_identity_and_complete_snapshot(
    monkeypatch, original, computed, snapshots, expected,
):
    queued = []
    monkeypatch.setattr(chat_cache, "build_cache_key", lambda **kwargs: computed)
    monkeypatch.setattr(chat_cache, "schedule_cache_write", lambda *args, **kwargs: queued.append(args))
    monkeypatch.setattr(chat_routes, "answer_succeeded", lambda *args: True)
    monkeypatch.setattr(chat_routes, "attempt_provider_model", lambda: ("openai", "model"))
    plan = SimpleNamespace(
        cached=False, cache_generation=cache_store.current_generation(),
        cache_key=original, cache_fingerprints=snapshots, kind="normal",
        question="问题", filters=None, agent_mode="off", skill_ids=[], images=[],
        history=[], query_history=[], memory_text="", source_type="internal_db",
        retrieval_query="问题", materials=[], materials_text="", retrieval_warning=None,
        retrieval_stats=None,
    )
    chat_cache.maybe_store_cache(plan, "回答", None)
    assert len(queued) == expected
    if queued:
        assert queued[0][0] == original
        assert queued[0][1].fingerprints == snapshots


@pytest.mark.asyncio
async def test_disabled_cache_performs_zero_queries_and_writes(monkeypatch):
    class NoQueries:
        async def execute(self, _statement):
            raise AssertionError("关闭缓存不应查库")

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", False)
    assert await cache_store.lookup(NoQueries(), "key") is None
    await cache_store.store_snapshot("key", cache_entry.CacheEntry())
    assert cache_store.cache_size() == 0
    assert chat_cache.build_cache_key(
        question="问题", filters=None, agent_mode="off", skill_ids=[],
        images=[], history=[], memory_text="",
    ) is None
