"""条目 `5.2` 纯函数层与存储层用例（`app/cache/**`）。

覆盖开工包 §5 的这些断言：

| 用例 | 断言 |
|---|---|
| ``test_key_*`` | **A8**（9 个成分各一条反例：只改一个 ⇒ 都不命中；含 ``filters`` 的**键序**） |
| ``test_lookup_rejects_*`` | **A10**（无悬空 / 无陈旧引用：缺块、**id 未变内容变**） |
| ``test_ttl_*`` / ``test_capacity_*`` | **A9**（TTL / 容量 / LRU；改配置**即刻**对已有条目生效） |
| ``test_disabled_*`` | **A2** / **A11**（关态零查零写；三个键都**不是死键**） |
| ``test_roundtrip_*`` | §2.3（材料全字段 round-trip；命中能**把一轮重放完整**） |

⚠️ 这里**不 mock** ``chunks`` 查询：存活校验是本条**唯一的正确性支点**，
拿假会话测它等于什么都没测——用例往真测试库（``tests/conftest.py::db``）里插真行，
再真的删 / 改，看 ``lookup`` 到底怎么判。
"""

from __future__ import annotations

import time

import pytest

from app.cache import entry as entry_mod
from app.cache import store as cache_store
from app.cache.entry import CacheEntry, CacheKeyInputs
from app.retrieval.rerank import RerankStats
from app.retrieval.types import Material
from tests.retrieval.conftest import seed_document


@pytest.fixture(autouse=True)
def _clean_cache():
    """进 / 出各清一次：**进程内状态会跨用例泄漏**（本仓踩过同类坑，见开工包 §7.3 第 9 条）。"""
    cache_store.reset_cache()
    cache_store.reset_failure_counts()
    yield
    cache_store.reset_cache()
    cache_store.reset_failure_counts()


@pytest.fixture
def cache_on(monkeypatch):
    """开态：三个键都设成有效值（本机 ``.env`` 无这三个键 ⇒ 默认关，必须显式打开）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 86400)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 256)
    return settings


def _inputs(**overrides) -> CacheKeyInputs:
    base = dict(
        question="向量库用什么存储？",
        filters={"kb_ids": [1]},
        agent_mode="off",
        skill_ids=["builtin:knowledge"],
        model_label="dashscope/qwen-plus",
        memory_text="",
        history=[{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好。"}],
        has_images=False,
        retrieval_config={"embed": {"service_id": "s1", "model": "m1"}},
    )
    base.update(overrides)
    return CacheKeyInputs(**base)


def _material(chunk_id: int, content: str, *, ordinal: int, **extra) -> Material:
    """造一条**字段填满**的材料（round-trip 用例要能逐字段对上）。"""
    base = dict(
        chunk_id=chunk_id,
        document_id=7,
        content=content,
        filename="检索手册.md",
        category="项目",
        kb_id=1,
        block_ordinal=3,
        page=12,
        score=0.5,
        vector_score=0.25,
        keyword_score=0.75,
        rerank_score=0.9,
        ordinal=ordinal,
    )
    base.update(extra)
    return Material(**base)


def _entry(materials: list[Material], *, raw_answer: str = "据资料 [1]，用 Qdrant。") -> CacheEntry:
    stats = RerankStats(status="disabled", candidates=len(materials))
    return CacheEntry(
        created_at=time.monotonic(),
        question="向量库用什么存储？",
        materials=entry_mod.serialize_materials(materials),
        materials_text="[1]（文档：检索手册.md）\n用 Qdrant。",
        retrieval_stats=stats.payload(),
        final_empty_reason=stats.empty_reason,
        raw_answer=raw_answer,
        reasoning="先看资料…",
        fingerprints={
            str(item.chunk_id): entry_mod.content_fingerprint(item.content) for item in materials
        },
        model_label="dashscope/qwen-plus",
        memory_text="",
    )


# --------------------------------------------------------------------------
# A8：9 个成分各一条反例（只改一个 ⇒ 键就必须变）
# --------------------------------------------------------------------------


def test_key_is_deterministic_for_identical_inputs():
    assert entry_mod.build_key(_inputs()) == entry_mod.build_key(_inputs())


def test_key_normalizes_whitespace_in_question():
    """① 规范化问题：同一问题多空格 / 换行 ⇒ **不能**造出两个键（否则命中率白掉）。"""
    left = entry_mod.build_key(_inputs(question="向量库 用什么\n存储？"))
    right = entry_mod.build_key(_inputs(question="  向量库 用什么 存储？  "))
    assert left == right


def test_key_is_insensitive_to_dict_field_order_but_not_content():
    """② 范围：``to_stored()`` **不排序** ⇒ 键构造必须 ``sort_keys=True``。

    同一个范围的两种**键序**必须同键（假 miss 的来源）；而**内容**不同就必须不同键。
    """
    left = entry_mod.build_key(_inputs(filters={"kb_ids": [1], "categories": ["项目"]}))
    right = entry_mod.build_key(_inputs(filters={"categories": ["项目"], "kb_ids": [1]}))
    assert left == right, "同一个范围换了键序就变键 ⇒ 假 miss"

    other = entry_mod.build_key(_inputs(filters={"kb_ids": [2], "categories": ["项目"]}))
    assert left != other


@pytest.mark.parametrize(
    "changed",
    [
        {"agent_mode": "react"},                      # ③ 生效 agent_mode
        {"skill_ids": ["builtin:knowledge", "builtin:web"]},  # ④ skill_ids
        {"skill_ids": []},
        {"model_label": "dashscope/qwen-max"},        # ⑤ 模型身份
        {"memory_text": "【跨会话记忆】用户叫小王。"},   # ⑥ 5.1 注入段（**最危险的一处**）
        {"history": [{"role": "user", "content": "换了一轮"}]},  # ⑦ 历史指纹
        {"retrieval_config": {"embed": {"service_id": "s1", "model": "m2"}}},  # ⑨ 检索配置
        {"retrieval_config": {"rerank_enabled": True}},
    ],
)
def test_key_changes_with_each_component(changed):
    """③–⑦ 与 ⑨：**只改一个成分**就必须换键（少任何一个都会串答案）。"""
    assert entry_mod.build_key(_inputs()) != entry_mod.build_key(_inputs(**changed))


def test_skill_ids_order_does_not_matter_but_membership_does():
    """④ ``skill_ids`` 排序后进键：顺序无关、成员有关。"""
    left = entry_mod.build_key(_inputs(skill_ids=["a", "b"]))
    assert left == entry_mod.build_key(_inputs(skill_ids=["b", "a"]))
    assert left != entry_mod.build_key(_inputs(skill_ids=["a"]))


def test_retrieval_fingerprint_reads_roles_and_knobs(monkeypatch):
    """⑨ 指纹 = 检索（embed / rerank 绑定 + 重排开关与门槛）+ **推理参数**子对象。

    ⚠️ **2026-09-20「推理等级登记表回流」加了一个 ``reasoning`` 子键**（见
    `entry._reasoning_fingerprint`）：换思考档位 ⇒ 答案本身就不同 ⇒ 必须换键（A17）。
    这个子键算的是「**实际会发给对话模型**的那份思考参数」，所以本用例也要给一个 chat
    绑定——否则 `retrieval_fingerprint()` 会按「配置解析失败 ⇒ 本轮不缓存」**抛
    `ValueError`**（那是有意的降级，专门有下一条用例钉它）。
    """
    from app.core.config import settings

    # 本机 .env 里真有 CHAT_REASONING 这一行，用例内一律显式钉住（否则结果随机器变）
    monkeypatch.setattr(settings, "CHAT_REASONING", "")
    # ⚠️ chat 绑定**必须有一条真服务**：这个子键算的是「实际会发给对话模型的参数」，
    # 而 `chat_config` 在「绑定指向已删服务」时会抛 `ValueError`（有意的降级口径）。
    monkeypatch.setattr(
        settings,
        "MODEL_SERVICES",
        '[{"id":"svc_c","name":"mimo","provider":"mimo","base_url":"https://x.invalid/v1",'
        '"api_key":"sk-c","models":["mimo-v2.5"],"model_roles":{"mimo-v2.5":["chat"]},'
        '"for_roles":["chat"]},'
        '{"id":"svc_a","name":"embed","provider":"openai","base_url":"https://embed.invalid/v1",'
        '"api_key":"sk-test","models":["emb-1","emb-2"],'
        '"model_roles":{"emb-1":["embed"],"emb-2":["embed"]},"for_roles":["embed"]}]',
    )
    monkeypatch.setattr(
        settings,
        "MODEL_ROLES",
        '{"chat": {"service_id": "svc_c", "model": "mimo-v2.5"},'
        ' "embed": {"service_id": "svc_a", "model": "emb-1"},'
        ' "rerank": {"service_id": "svc_b", "model": "rk-1"}}',
    )
    monkeypatch.setattr(settings, "RAG_ENABLE_RERANK", True)
    first = entry_mod.retrieval_fingerprint()
    assert first["embed"] == {
        "provider": "openai", "service_id": "svc_a", "base_url": "https://embed.invalid/v1",
        "model": "emb-1", "dim": settings.EMBED_DIM,
    }
    assert first["rerank"] == {"service_id": "svc_b", "model": "rk-1"}
    assert first["rerank_enabled"] is True
    assert first["reasoning"] == {}, "没设档位 ⇒ 空 dict（**不是** None、不是缺键）"

    monkeypatch.setattr(
        settings,
        "MODEL_ROLES",
        '{"chat": {"service_id": "svc_c", "model": "mimo-v2.5"},'
        ' "embed": {"service_id": "svc_a", "model": "emb-2"}}',
    )
    second = entry_mod.retrieval_fingerprint()
    assert second != first
    assert second["rerank"] is None
    monkeypatch.setattr(
        settings, "MODEL_SERVICES",
        settings.MODEL_SERVICES.replace("https://embed.invalid/v1", "https://embed-two.invalid/v1"),
    )
    third = entry_mod.retrieval_fingerprint()
    assert third != second
    monkeypatch.setattr(settings, "EMBED_DIM", settings.EMBED_DIM + 8)
    assert entry_mod.retrieval_fingerprint() != third


def test_retrieval_fingerprint_raises_when_chat_config_unreadable(monkeypatch):
    """⑨ 的 ``reasoning`` 子对象**故意不吞异常**：读不到对话模型 ⇒ 抛（调用方 ⇒ 本轮不缓存）。

    这条与 ``embed`` / ``rerank`` / ``graph`` 三支的取舍**相反**，理由写在
    `entry._reasoning_fingerprint` 的 docstring 里：返回 ``{}`` 会把「读不到配置」
    伪装成「本来就不发思考参数」⇒ **错命中**。`chat_routes._cache_key_for_turn` 的
    ``except Exception`` 会把它收敛成「本轮不缓存」——**不是**把 `{}` 写进键。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "MODEL_SERVICES", "")
    monkeypatch.setattr(settings, "MODEL_ROLES", "")
    for key in ("MODEL_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL"):
        monkeypatch.setattr(settings, key, "")
    with pytest.raises(ValueError):
        entry_mod.retrieval_fingerprint()


def test_reasoning_fingerprint_equals_the_body_actually_sent(monkeypatch):
    """**核心不变量**：键里的 ``reasoning`` 就是**真装配出来的那份 `extra_body`**。

    指纹（`app/cache/entry.py`）与装配（`app/generation/answer.py`）必然各写一遍，
    所以这里把两者**钉成同一个值**——「指纹用**实际会发出去的参数**算」这条口径
    只有这一处能验。
    """
    from app.core.config import settings
    from app.generation import answer as answer_mod

    monkeypatch.setattr(
        settings,
        "MODEL_SERVICES",
        '[{"id":"svc_c","name":"mimo","provider":"mimo","base_url":"https://x.invalid/v1",'
        '"api_key":"sk-x","models":["mimo-v2.5"],"model_roles":{"mimo-v2.5":["chat"]},'
        '"for_roles":["chat"]}]',
    )
    monkeypatch.setattr(
        settings, "MODEL_ROLES", '{"chat": {"service_id": "svc_c", "model": "mimo-v2.5"}}'
    )
    monkeypatch.setattr(
        settings,
        "CHAT_REASONING",
        '{"param":"thinking.type","value":"disabled","model":"mimo-v2.5"}',
    )
    bound = answer_mod.build_chat_model()
    assert bound.provider._extra_body == {"thinking": {"type": "disabled"}}
    assert entry_mod.retrieval_fingerprint()["reasoning"] == bound.provider._extra_body

    # 换档 ⇒ 指纹跟着变（缓存键随之不同）
    before = entry_mod.retrieval_fingerprint()
    monkeypatch.setattr(settings, "CHAT_REASONING", "")
    after = entry_mod.retrieval_fingerprint()
    assert after != before
    assert after["reasoning"] == {}
    assert answer_mod.build_chat_model().provider._extra_body == {}, "关档 ⇒ 与改动前逐字一致"


def test_history_token_ignores_extra_keys_but_tracks_content():
    """⑦ 历史指纹只取 ``role`` / ``content``：加内部键不失效，改内容就换键。"""
    plain = [{"role": "user", "content": "A"}]
    with_extra = [{"role": "user", "content": "A", "message_id": 99}]
    assert entry_mod.history_token(plain) == entry_mod.history_token(with_extra)
    assert entry_mod.history_token(plain) != entry_mod.history_token(
        [{"role": "user", "content": "B"}]
    )


# --------------------------------------------------------------------------
# 关态（A2 / A11）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_store_is_zero_touch(db, monkeypatch):
    """**A11**：关态下 ``lookup`` / ``store_snapshot`` 各自首行早退 —— 零查表、零写表。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", False)
    assert await cache_store.lookup(db, "any-key") is None
    await cache_store.store_snapshot("any-key", _entry([]))
    assert cache_store.cache_size() == 0
    assert cache_store.failure_counts()["hit"] == 0


@pytest.mark.asyncio
async def test_ttl_zero_and_capacity_zero_also_disable(db, monkeypatch):
    """**A2**：TTL / 容量也是**真开关**（``<= 0`` ⇒ 关），不是装饰性配置键。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_ENABLE", True)

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 0)
    assert cache_store.enabled() is False
    await cache_store.store_snapshot("k", _entry([]))
    assert cache_store.cache_size() == 0

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 60)
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 0)
    assert cache_store.enabled() is False
    await cache_store.store_snapshot("k", _entry([]))
    assert cache_store.cache_size() == 0


@pytest.mark.asyncio
async def test_reset_cache_clears_even_when_disabled(cache_on):
    """``reset_cache`` 是测试工具：**关态也该能清**（否则用例之间会互相污染）。"""
    await cache_store.store_snapshot("k", _entry([]))
    assert cache_store.cache_size() == 1
    cache_store.reset_cache()
    assert cache_store.cache_size() == 0


# --------------------------------------------------------------------------
# 命中 / 重放（§2.3）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_roundtrip_rebuilds_materials_and_stats(db, cache_on):
    """材料**全字段** round-trip 且重建回 ``Material``；统计与派生原因一并读回。"""
    document = await seed_document(db, ["用 Qdrant 内嵌模式。"], filename="检索手册.md")
    from app.models import Chunk
    from sqlalchemy import select

    chunk = (await db.execute(select(Chunk).where(Chunk.document_id == document.id))).scalars().first()
    material = _material(chunk.id, chunk.content, ordinal=1, document_id=document.id, block_ordinal=1)
    stats = RerankStats(status="disabled", candidates=1)
    entry = CacheEntry(
        created_at=time.monotonic(),
        question="向量库用什么存储？",
        materials=entry_mod.serialize_materials([material]),
        materials_text="[1]（文档：检索手册.md）",
        retrieval_stats=stats.payload(),
        final_empty_reason=stats.empty_reason,
        raw_answer="据资料 [1]，用 Qdrant。",
        reasoning="先看资料…",
        fingerprints={str(chunk.id): entry_mod.content_fingerprint(chunk.content)},
    )
    await cache_store.store_snapshot("k", entry)

    hit = await cache_store.lookup(db, "k")
    assert hit is not None
    assert len(hit.materials) == 1
    rebuilt = hit.materials[0]
    assert isinstance(rebuilt, Material)
    assert rebuilt == material, "材料字段没有全字段 round-trip"
    assert rebuilt.chunk_key == material.chunk_key, "重建后 property 仍要工作"
    assert hit.stats is not None and hit.stats.status == "disabled"
    assert hit.entry.raw_answer == "据资料 [1]，用 Qdrant。"
    assert hit.entry.reasoning == "先看资料…"


@pytest.mark.asyncio
async def test_lookup_miss_for_unknown_key(db, cache_on):
    assert await cache_store.lookup(db, "never-stored") is None
    assert cache_store.failure_counts()["miss"] == 1


@pytest.mark.asyncio
async def test_lookup_drops_entries_with_foreign_schema_version(db, cache_on):
    """结构版本号不匹配 ⇒ 当未命中并**丢弃**（结构一改就整体失效）。"""
    entry = _entry([])
    await cache_store.store_snapshot("k", entry)
    entry.version = entry_mod.CACHE_SCHEMA_VERSION + 1
    assert await cache_store.lookup(db, "k") is None
    assert cache_store.cache_size() == 0
    assert cache_store.failure_counts()["stale"] == 1


# --------------------------------------------------------------------------
# A10：存活校验（**本条最关键的闸门**）
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_passes_when_chunks_are_unchanged(db, cache_on):
    document = await seed_document(db, ["原始内容"])
    from app.models import Chunk
    from sqlalchemy import select

    chunk = (await db.execute(select(Chunk).where(Chunk.document_id == document.id))).scalars().first()
    await cache_store.store_snapshot("k", _entry([_material(chunk.id, chunk.content, ordinal=1)]))
    assert await cache_store.lookup(db, "k") is not None


@pytest.mark.asyncio
async def test_lookup_rejects_when_chunk_is_gone(db, cache_on):
    """**A10 ①**：块被硬删 ⇒ 不命中（否则会给出悬空引用）。"""
    document = await seed_document(db, ["会被删掉的内容"])
    from app.models import Chunk
    from sqlalchemy import delete, select

    chunk = (await db.execute(select(Chunk).where(Chunk.document_id == document.id))).scalars().first()
    await cache_store.store_snapshot("k", _entry([_material(chunk.id, chunk.content, ordinal=1)]))
    await db.execute(delete(Chunk).where(Chunk.id == chunk.id))
    await db.commit()

    assert await cache_store.lookup(db, "k") is None
    assert cache_store.cache_size() == 0, "没过的条目要当场删掉，别每轮白查一次 SELECT"
    assert cache_store.failure_counts()["stale"] == 1


@pytest.mark.asyncio
async def test_lookup_rejects_when_content_changed_but_id_survived(db, cache_on):
    """**A10 ②**（本包按侦察结论新增）：**``chunk_id`` 未变、内容已变** ⇒ 也不命中。

    形状就是 ``reindex`` 的「删块重建」：新块的 ``(document_id, ordinal)`` 与旧的相同 ⇒
    ``chunk_id`` 可能照样存在，但内容已经变了。只查「id 还在不在」会给出**基于旧内容**的
    陈旧答案（现在还会连答案一起是旧的）。
    """
    document = await seed_document(db, ["旧内容"])
    from app.models import Chunk
    from sqlalchemy import select, update

    chunk = (await db.execute(select(Chunk).where(Chunk.document_id == document.id))).scalars().first()
    await cache_store.store_snapshot("k", _entry([_material(chunk.id, chunk.content, ordinal=1)]))
    assert await cache_store.lookup(db, "k") is not None

    # 同一个 chunk_id、内容换掉（reindex 后的等价情形）
    await db.execute(update(Chunk).where(Chunk.id == chunk.id).values(content="新内容"))
    await db.commit()

    assert await cache_store.lookup(db, "k") is None
    assert cache_store.failure_counts()["stale"] == 1


@pytest.mark.asyncio
async def test_liveness_is_vacuously_true_without_fingerprints(db, cache_on):
    """零材料的轮（``direct``）没有可悬空的东西 ⇒ 不查库、恒判存活。"""
    await cache_store.store_snapshot("k", _entry([]))
    assert await cache_store.lookup(db, "k") is not None


@pytest.mark.asyncio
async def test_liveness_ignores_negative_placeholder_ids(db, cache_on):
    """网页材料的负 id 占位 / 笔记的负 placeholder 没有 ``chunks`` 行 ⇒ 跳过、不误判未命中。"""
    entry = _entry([])
    entry.fingerprints = {"-123456": entry_mod.content_fingerprint("网页正文")}
    await cache_store.store_snapshot("k", entry)
    assert await cache_store.lookup(db, "k") is not None


# --------------------------------------------------------------------------
# A9：TTL 与容量
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ttl_expiry_drops_entry(db, cache_on, monkeypatch):
    """过 TTL ⇒ 不命中，且条目被删。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 60)
    entry = _entry([])
    entry.created_at = time.monotonic() - 61
    await cache_store.store_snapshot("k", entry)

    assert await cache_store.lookup(db, "k") is None
    assert cache_store.cache_size() == 0
    assert cache_store.failure_counts()["expired"] == 1


@pytest.mark.asyncio
async def test_ttl_is_recomputed_not_baked_in(db, cache_on, monkeypatch):
    """**A9 的关键一条**：TTL 按 ``created_at`` **现算** ⇒ **改配置对已有条目即刻生效**。

    写入时烤一个 ``expires_at`` 的做法做不到这一点（改小的 TTL 要等旧条目自己到期）。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 86400)
    entry = _entry([])
    entry.created_at = time.monotonic() - 10  # 10 秒前写的
    await cache_store.store_snapshot("k", entry)
    assert await cache_store.lookup(db, "k") is not None, "长 TTL 下应当命中"

    # 同一个条目、**不重写**，只把配置调小 ⇒ 立刻过期
    monkeypatch.setattr(settings, "SEMANTIC_CACHE_TTL_SECONDS", 5)
    assert await cache_store.lookup(db, "k") is None


@pytest.mark.asyncio
async def test_capacity_evicts_the_oldest(db, cache_on, monkeypatch):
    """超容量 ⇒ 淘汰**最旧**的（``popitem(last=False)``）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 2)
    for key in ("k1", "k2", "k3"):
        await cache_store.store_snapshot(key, _entry([]))
    assert cache_store.cache_size() == 2
    assert cache_store.failure_counts()["evicted"] == 1
    assert await cache_store.lookup(db, "k1") is None, "最旧的应当被淘汰"
    assert await cache_store.lookup(db, "k2") is not None
    assert await cache_store.lookup(db, "k3") is not None


@pytest.mark.asyncio
async def test_hit_refreshes_lru_position(db, cache_on, monkeypatch):
    """命中会 ``move_to_end`` ⇒ 「最近用过的」不会先被淘汰（正牌 LRU 而不是 FIFO）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "SEMANTIC_CACHE_MAX_ENTRIES", 2)
    await cache_store.store_snapshot("k1", _entry([]))
    await cache_store.store_snapshot("k2", _entry([]))
    assert await cache_store.lookup(db, "k1") is not None  # k1 变成最近使用的
    await cache_store.store_snapshot("k3", _entry([]))     # 该淘汰 k2

    assert await cache_store.lookup(db, "k2") is None
    assert await cache_store.lookup(db, "k1") is not None


@pytest.mark.asyncio
async def test_store_snapshot_keeps_the_first_writer(db, cache_on):
    """已存在同键 ⇒ **跳过**（第一次写入者胜出，避免两条并发轮互相覆盖）。"""
    first = _entry([], raw_answer="第一份回答")
    second = _entry([], raw_answer="第二份回答")
    await cache_store.store_snapshot("k", first)
    await cache_store.store_snapshot("k", second)
    hit = await cache_store.lookup(db, "k")
    assert hit is not None and hit.entry.raw_answer == "第一份回答"


# --------------------------------------------------------------------------
# I5：任何异常路径都当未命中 / 写失败一律吞
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_failure_is_a_miss_not_a_crash(db, cache_on, monkeypatch):
    """**A5 ①**：读表抛异常 ⇒ 当未命中（**绝不炸穿本轮**）。"""
    await cache_store.store_snapshot("k", _entry([]))

    def _boom(_fingerprints):
        raise RuntimeError("模拟查库失败")

    monkeypatch.setattr(entry_mod, "fingerprints_live", _boom)
    assert await cache_store.lookup(db, "k") is None
    assert cache_store.failure_counts()["error"] == 1


@pytest.mark.asyncio
async def test_deserialization_failure_is_a_miss_not_a_crash(db, cache_on, monkeypatch):
    """**A5 ②**：材料反序列化失败（形状坏掉）⇒ 当未命中，不炸穿。"""
    await cache_store.store_snapshot("k", _entry([]))
    monkeypatch.setattr(entry_mod, "deserialize_materials", lambda rows: 1 / 0)
    assert await cache_store.lookup(db, "k") is None
    assert cache_store.failure_counts()["error"] == 1


@pytest.mark.asyncio
async def test_write_failure_never_raises(db, cache_on, monkeypatch):
    """**A5**：写失败一律吞（缓存是优化项，绝不因为写不进去影响本轮）。"""

    class _ExplodingLock:
        """假锁：一进 ``async with`` 就炸（模拟「写表时抛了」）。"""

        async def __aenter__(self):
            raise RuntimeError("模拟写失败")

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(cache_store, "_LOCK", _ExplodingLock())
    await cache_store.store_snapshot("k", _entry([]))  # 不抛即通过
    assert cache_store.failure_counts()["error"] == 1
    assert cache_store.cache_size() == 0
