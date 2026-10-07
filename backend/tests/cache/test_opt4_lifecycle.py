"""OPT4.5：真来源、真存取与变更生命周期；离线 SQLite，无模型调用。"""

import importlib
import json
from datetime import datetime

import pytest
from sqlalchemy import select

from app.api import chat_cache
from app.cache import entry as cache_entry, store as cache_store
from app.core.config import settings
from app.models import Chunk, Memory
from app.retrieval.types import Material
from app.session import memory
from tests.cache.test_algorithm_u6_exact import _key, clean_cache
from tests.retrieval.conftest import seed_document
from tests.session.test_algorithm_u6_memory import add_fact


async def _source(db):
    body = "项目负责人为李甲。" + "窗口之外的背景内容。" * 100
    document = await seed_document(db, [body], filename="OPT4缓存资料.md", start_ordinal=0)
    chunk = (await db.execute(select(Chunk).where(Chunk.document_id == document.id))).scalar_one()
    material = Material(
        chunk_id=chunk.id, document_id=document.id, filename=document.filename,
        content="项目负责人为李甲。", kb_id=document.kb_id, block_ordinal=0,
        ordinal=1, windowed=True, covered_ordinals=(0,), span_start=0, span_end=10,
    )
    return document, chunk, [material]


async def _snapshot(db, materials, answer="项目负责人为李甲。[1]"):
    fingerprints = await cache_entry.capture_fingerprints(db, materials)
    assert fingerprints and len(fingerprints) == 1, "必须抓到真实完整来源，不能靠空指纹通过"
    return cache_entry.CacheEntry(
        raw_answer=answer, materials=cache_entry.serialize_materials(materials),
        fingerprints=fingerprints,
    )


async def _prime(db, materials, key):
    entry = await _snapshot(db, materials)
    await cache_store.store_snapshot(key, entry, generation=cache_store.current_generation())
    assert cache_store.cache_size() == 1 and cache_store.failure_counts()["stored"] == 1
    hit = await cache_store.lookup(db, key)
    assert hit is not None and hit.entry.raw_answer == entry.raw_answer
    assert len(hit.materials) == 1 and hit.materials[0].chunk_id == materials[0].chunk_id
    assert cache_store.failure_counts()["hit"] == 1
    return entry


@pytest.mark.asyncio
async def test_document_edit_outside_rendered_window_invalidates_real_hit(db):
    _, chunk, materials = await _source(db)
    key = _key()
    await _prime(db, materials, key)
    displayed = materials[0].content
    chunk.content += "尾部修订：项目负责人改为李乙。"
    await db.commit()
    assert chunk.content.startswith(displayed) and materials[0].content == displayed
    assert await cache_store.lookup(db, key) is None, "窗口正文不变也不能重放已失效的完整来源"
    assert cache_store.cache_size() == 0 and cache_store.failure_counts()["stale"] == 1
    assert cache_store.failure_counts()["hit"] == 1


@pytest.mark.asyncio
async def test_document_delete_restore_does_not_resurrect_but_new_snapshot_can_hit(db):
    document, _, materials = await _source(db)
    key = _key()
    await _prime(db, materials, key)
    document.deleted_at = datetime(2026, 10, 6)
    await db.commit()
    assert await cache_store.lookup(db, key) is None
    assert cache_store.cache_size() == 0 and cache_store.failure_counts()["stale"] == 1
    document.deleted_at = None
    await db.commit()
    cache_store.invalidate_all("opt4-source-restored")
    assert await cache_store.lookup(db, key) is None, "来源恢复不等于旧缓存复活"
    await cache_store.store_snapshot(
        key, await _snapshot(db, materials, answer="恢复后新生成的回答。[1]"),
        generation=cache_store.current_generation(),
    )
    hit = await cache_store.lookup(db, key)
    assert hit is not None and hit.entry.raw_answer == "恢复后新生成的回答。[1]"
    assert cache_store.cache_size() == 1 and cache_store.failure_counts()["stored"] == 2
    assert cache_store.failure_counts()["hit"] == 2


@pytest.mark.parametrize("change", ["scope", "model", "history", "memory_version"])
@pytest.mark.asyncio
async def test_changed_request_identity_cannot_hit_previously_stored_answer(db, monkeypatch, change):
    _, _, materials = await _source(db)
    inputs = {}
    first_memory = None
    if change == "model":
        services = [{
            "id": role, "name": role, "provider": "openai",
            "base_url": f"https://{role}.invalid/v1", "api_key": "synthetic-only",
            "models": [f"{role}-model"], "model_roles": {f"{role}-model": [role]},
            "for_roles": [role],
        } for role in ("chat", "embed", "rerank")]
        monkeypatch.setattr(settings, "MODEL_ROLES", json.dumps({
            role: {"service_id": role, "model": f"{role}-model"}
            for role in ("chat", "embed", "rerank")
        }))
        monkeypatch.setattr(settings, "MODEL_SERVICES", json.dumps(services))
        inputs["retrieval_config"] = cache_entry.retrieval_fingerprint()
    if change == "memory_version":
        monkeypatch.setattr(settings, "MEMORY_ENABLE", True)
        monkeypatch.setattr(settings, "MEMORY_TOP_K", 5)
        first_memory = await add_fact(db, "用户项目负责人为李甲", fact_key="opt4-project-owner")
        inputs["memory_text"] = memory.render_block(await memory.recall(db, question="项目负责人"))
        assert inputs["memory_text"]
    old_key = _key(**inputs)
    await _prime(db, materials, old_key)
    if change == "scope":
        inputs["filters"] = {"kb_ids": [2]}
    elif change == "model":
        services[0]["base_url"] = "https://other-chat.invalid/v1"
        monkeypatch.setattr(settings, "MODEL_SERVICES", json.dumps(services))
        inputs["retrieval_config"] = cache_entry.retrieval_fingerprint()
    elif change == "history":
        inputs["history"] = [{"role": "user", "content": "当前讨论的是另一个项目"}]
    else:
        revised = await add_fact(
            db, "用户项目负责人为李乙", fact_key="opt4-project-owner", supersedes_id=first_memory,
        )
        assert (await db.get(Memory, revised)).version == 2
        assert (await db.get(Memory, first_memory)).status == "superseded"
        inputs["memory_text"] = memory.render_block(await memory.recall(db, question="项目负责人"))
        assert "李乙" in inputs["memory_text"] and "李甲" not in inputs["memory_text"]
    assert await cache_store.lookup(db, _key(**inputs)) is None, "新请求身份不能重放之前实际存入的回答"
    assert cache_store.cache_size() == 1
    assert cache_store.failure_counts()["stored"] == cache_store.failure_counts()["hit"] == 1
    assert cache_store.failure_counts()["miss"] == 1


@pytest.mark.asyncio
async def test_inflight_old_generation_is_rejected_and_fresh_generation_can_hit(db):
    _, _, materials = await _source(db)
    old_key = _key()
    pending_key = _key(history=[{"role": "user", "content": "已开始的第二轮"}])
    await _prime(db, materials, old_key)
    pending = await _snapshot(db, materials)
    chat_cache.schedule_cache_write(pending_key, pending, generation=cache_store.current_generation())
    assert cache_store.invalidate_all("opt4-change-during-generation") == 1
    await chat_cache.drain_cache_tasks()
    assert cache_store.cache_size() == 0 and cache_store.failure_counts()["stored"] == 1
    assert await cache_store.lookup(db, old_key) is None
    assert await cache_store.lookup(db, pending_key) is None, "排期中的旧世代快照不得复活"
    await cache_store.store_snapshot(
        pending_key, await _snapshot(db, materials, answer="新世代回答。[1]"),
        generation=cache_store.current_generation(),
    )
    hit = await cache_store.lookup(db, pending_key)
    assert hit is not None and hit.entry.raw_answer == "新世代回答。[1]"
    assert cache_store.cache_size() == 1 and cache_store.failure_counts()["stored"] == 2


@pytest.mark.asyncio
async def test_module_restart_clears_a_previously_stored_and_live_answer(db):
    _, _, materials = await _source(db)
    key = _key()
    await _prime(db, materials, key)
    importlib.reload(cache_store)
    assert cache_store.cache_size() == 0 and cache_store.failure_counts()["stored"] == 0
    assert await cache_store.lookup(db, key) is None
    assert cache_store.failure_counts()["hit"] == 0 and cache_store.failure_counts()["miss"] == 1
