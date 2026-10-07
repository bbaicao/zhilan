"""判据载体 · `回归修复`：缓存失效与**在途写盘**的定序。

**缺陷（现码）**：``backend/app/api/chat_cache.py:285-301`` 的 ``schedule_cache_write`` 是
**fire-and-forget**（``loop.create_task(cache_store.store_snapshot(cache_key, entry))``），
而 ``backend/app/cache/store.py:277-308`` 的 ``invalidate_all`` 是**同步清表**
（``:298-299`` 自述「同步函数…不需要持 ``_LOCK``」，体 = ``cleared = cache_size(); reset_cache()``）。
⇒ **先排写、后失效** 时，失效只是把**当时**的表清空；**在途的那条任务随后仍会写进表里** ⇒
「写完缓存」语义被破坏：被失效掉的旧答案又活了。

**判据（本件）**：

| 腿 | 用例 | 断言 | 现码 |
|---|---|---|---|
| ① 主判据 | ``test_invalidation_before_drain_does_not_resurrect`` | 排写 → ``invalidate_all``（返回 0）→ ``await drain_cache_tasks()`` ⇒ ``cache_size() == 0`` 且 ``lookup(...) is None`` | **RED**（size = 1） |
| ② 主判据 | ``test_write_after_invalidation_lands`` | 失效**之后**排的写必须真落盘（``cache_size() == 1`` / 新条目可命中 / 旧条目仍无） | **RED**（size = 2） |
| ③ 反向保护 | ``test_same_generation_first_writer_wins`` | 同一代内同键两次排写 ⇒ 第一次写入者胜出（对照 ``tests/cache/test_cache.py:520-528``） | 两态绿 |
| ④ 反向保护 | ``test_disabled_state_still_clears_and_reports_count`` | 关态也清 + ``invalidate_all`` 返回**已清条数**（照 ``tests/api/test_cache_clear_on_change_t3_4.py:353-364`` 的口径） | 两态绿 |
| ⑤ 接口约束 | ``test_invalidate_all_stays_synchronous`` | ``inspect.iscoroutinefunction(...) is False``（``backend/app/agent/tools/_write.py:478-480`` 是同步调用点）+ ``reason`` 参数仍在 | 两态绿 |

**材料用真语料**（``tests/retrieval/conftest.py:32 seed_document``）⇒ ``fingerprints_live`` 真的过 ⇒
「条目有没有复活」才**有判别力**（若拿空材料，``lookup`` 会因存活校验恒 ``None``，腿① 的第二条断言就成**空靶**）。

**红线的反例（本件一律不依赖）**：不断言任何私有表 ``_STORE`` 的内部形状、不匹配产品代码里的字符串、
不断言「某函数被调用过」这类弱判据 —— 全部落在**接口级行为**（``cache_size()`` / ``lookup()`` /
``invalidate_all()`` 的返回值 / 签名）上。
"""

from __future__ import annotations

import inspect
import time

import pytest

from app.api import chat_cache
from app.cache import entry as entry_mod
from app.cache import store as cache_store
from app.cache.entry import CacheEntry
from app.retrieval.rerank import RerankStats
from app.retrieval.types import Material
from tests.retrieval.conftest import seed_document

QUESTION = "向量库用什么存储？"
MATERIAL_CHUNK = "向量库使用 Qdrant 内嵌模式，向量化由上游接口完成。"
MATERIAL_FILENAME = "缓存定序手册.md"

KEY_STALE = "bug166-probe-stale"
KEY_FRESH = "bug166-probe-fresh"
KEY_SAME = "bug166-probe-same"


@pytest.fixture(autouse=True)
def _clean_cache():
    """进出各清一次（``tests/cache/`` 无 conftest ⇒ 本件自带，照 ``test_cache.py:32-39``）。"""
    cache_store.reset_cache()
    cache_store.reset_failure_counts()
    yield
    cache_store.reset_cache()
    cache_store.reset_failure_counts()


@pytest.fixture
def cache_on(monkeypatch):
    """开态夹具（照 ``tests/api/test_cache_clear_on_change_t3_4.py:158-170`` 的三键开法）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 86400)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 256)
    cache_store.reset_cache()
    cache_store.reset_failure_counts()
    yield settings
    cache_store.reset_cache()
    cache_store.reset_failure_counts()


def _entry(materials: list[Material], *, raw_answer: str) -> CacheEntry:
    """造一条**存活校验真能过**的条目（指纹只含真 chunk_id ⇒ 挂的就是真语料）。"""
    stats = RerankStats(status="disabled", candidates=len(materials))
    return CacheEntry(
        created_at=time.monotonic(),
        question=QUESTION,
        materials=entry_mod.serialize_materials(materials),
        materials_text="",
        retrieval_stats=stats.payload(),
        final_empty_reason=stats.empty_reason,
        raw_answer=raw_answer,
        fingerprints={
            str(item.chunk_id): entry_mod.content_fingerprint(item.content) for item in materials
        },
        model_label="stub/fake-chat",
        memory_text="",
    )


async def _real_materials(db) -> list[Material]:  # noqa: ANN001
    """真语料 ⇒ 真 ``chunk_id``（``fingerprints_live`` 的块支路要 join 到 ``Document.deleted_at IS NULL``）。"""
    from sqlalchemy import select

    from app.models import Chunk
    from app.retrieval.types import number

    document = await seed_document(db, [MATERIAL_CHUNK], filename=MATERIAL_FILENAME)
    rows = (
        (await db.execute(select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.id)))
        .scalars()
        .all()
    )
    materials = [
        Material(
            chunk_id=row.id,
            document_id=document.id,
            content=row.content,
            filename=MATERIAL_FILENAME,
            kb_id=1,
            block_ordinal=row.ordinal,
            keyword_score=0.9,
        )
        for row in rows
    ]
    number(materials)
    return materials


@pytest.mark.asyncio
async def test_invalidation_before_drain_does_not_resurrect(db, cache_on):  # noqa: ANN001
    """**主判据**：先排写、后失效、再等写盘落定 ⇒ 被失效的条目**不得**复活。

    现码：``invalidate_all`` 清的是「**此刻**的表」，在途的 ``create_task`` 照旧在
    ``drain_cache_tasks()`` 里落定 ⇒ ``cache_size()`` 回到 1 ⇒ 本腿红。
    """
    materials = await _real_materials(db)

    chat_cache.schedule_cache_write(KEY_STALE, _entry(materials, raw_answer="写盘前的回答"))
    cleared = cache_store.invalidate_all("bug166:leg1")
    assert cleared == 0, "排写与失效之间没有 await ⇒ 那时表里本应是空的（调度任务还没跑）"

    await chat_cache.drain_cache_tasks()

    assert cache_store.cache_size() == 0, (
        "「schedule_cache_write 之后失效 ⇒ 再等写盘落定」把旧条目又写回表里了："
        "失效与在途写盘之间缺少定序（现码 invalidate_all 只清当时表，管不住已排期的任务）"
    )
    assert await cache_store.lookup(db, KEY_STALE) is None, "复活的那条还必须真的能被命中（否则本腿断言过弱）"


@pytest.mark.asyncio
async def test_write_after_invalidation_lands(db, cache_on):  # noqa: ANN001
    """**主判据**：失效**之后**排的写必须真落盘；失效**之前**排的那条不得跟着复活。"""
    materials = await _real_materials(db)

    chat_cache.schedule_cache_write(KEY_STALE, _entry(materials, raw_answer="失效前的回答"))
    assert cache_store.invalidate_all("bug166:leg2") == 0

    chat_cache.schedule_cache_write(KEY_FRESH, _entry(materials, raw_answer="失效后的回答"))
    await chat_cache.drain_cache_tasks()

    assert cache_store.cache_size() == 1, (
        "定序缺失：两条排写（失效前 1 条 + 失效后 1 条）都落盘了 ⇒ 旧条目复活。"
        "正确语义 = 失效是分水岭：失效前排的写法必须作废，失效后排的必须生效"
    )
    fresh = await cache_store.lookup(db, KEY_FRESH)
    assert fresh is not None, "失效之后的新写入必须真能命中（否则「size == 1」可能只是「那一条恰好没写进去」）"
    assert fresh.entry.raw_answer == "失效后的回答"
    assert await cache_store.lookup(db, KEY_STALE) is None


@pytest.mark.asyncio
async def test_same_generation_first_writer_wins(db, cache_on):  # noqa: ANN001
    """反向保护：**同一代内**同键两次排写 ⇒ 第一次写入者胜出（对照 ``tests/cache/test_cache.py:520-528``）。

    定序修复（世代号 / 定序）不得把「同代 first-writer-wins」这条既有语义顺手改掉。
    """
    materials = await _real_materials(db)

    chat_cache.schedule_cache_write(KEY_SAME, _entry(materials, raw_answer="第一份回答"))
    chat_cache.schedule_cache_write(KEY_SAME, _entry(materials, raw_answer="第二份回答"))
    await chat_cache.drain_cache_tasks()

    hit = await cache_store.lookup(db, KEY_SAME)
    assert hit is not None and hit.entry.raw_answer == "第一份回答", (
        "同代内同键必须是第一次写入者胜出（两轮并发写不得互相覆盖）"
    )
    assert cache_store.cache_size() == 1


@pytest.mark.asyncio
async def test_disabled_state_still_clears_and_reports_count(db, cache_on, monkeypatch):  # noqa: ANN001
    """反向保护：关态也清 + 返回值 = **已清条数**。

    与 ``tests/api/test_cache_clear_on_change_t3_4.py:353-364`` 同一口径（那条腿是直接
    戳 ``_STORE`` 塞两条；本腿改成先经公开接口 ``store_snapshot`` 真写两条再关开关 ⇒
    断言完全落在接口面上：``invalidate_all()`` 的返回值与 ``cache_size()``）。
    """
    from app.core.config import settings

    materials = await _real_materials(db)
    await cache_store.store_snapshot(KEY_STALE, _entry(materials, raw_answer="A"))
    await cache_store.store_snapshot(KEY_FRESH, _entry(materials, raw_answer="B"))
    assert cache_store.cache_size() == 2

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", False)

    cleared = cache_store.invalidate_all("bug166:leg4")
    assert cleared == 2, "返回值必须是**已清条数**（不是 bool、也不是 0）"
    assert isinstance(cleared, int)
    assert cache_store.cache_size() == 0, "关态也必须真清（失效不能因为「缓存关着」而早退）"


def test_invalidate_all_stays_synchronous():
    """接口约束（两态同绿）：``invalidate_all`` 必须**保持同步**签名。

    理由：``backend/app/agent/tools/_write.py:478-480`` 是**同步**调用点（写入成功后
    ``_cache_store.invalidate_all(reason=...)``）⇒ 改成 ``async def`` 会牵动产品代码面
    （附录 B 把该文件定为 S3→S4 串行）。``schedule_cache_write`` 侧同理（它是排期入口，不阻塞调用方）。
    """
    assert inspect.iscoroutinefunction(cache_store.invalidate_all) is False, (
        "invalidate_all 必须保持同步：backend/app/agent/tools/_write.py:478-480 同步调用它"
    )
    assert inspect.iscoroutinefunction(chat_cache.schedule_cache_write) is False, (
        "schedule_cache_write 是**排期**入口（fire-and-forget），不得改成「必须 await」才落盘"
    )
    assert "reason" in inspect.signature(cache_store.invalidate_all).parameters, (
        "生产调用点一律用 reason= 关键字传参（9 处：note_service 3 / document_routes 4 / trash_routes 2）"
    )
