"""缓存存储与失效（阶段 5 · 条目 `5.2`，子条目 `5.2.1`）。

**形态**：模块级 ``OrderedDict[key_hash, CacheEntry]`` + 一把 ``asyncio.Lock``（并发保护）。
进程内、零依赖、零出网、零新锁面、桌面态天然可用、**无迁移无表**。

## 为什么是进程内（三个候选的实测代价）

| 候选 | 结论 |
|---|---|
| **Qdrant 第二集合** | ❌ Qdrant 层支持多集合，但**应用层没有安全的打开方式**：唯一钥匙 ``get_vector_store(collection=…)`` 必然新建第二个 ``QdrantClient``（``app/vector/__init__.py:52-56``），而本地模式是 ``<path>/.lock`` 的 **EXCLUSIVE + NON_BLOCKING 独占锁** ⇒ **同进程第二次打开也失败**，且报错文案是「该目录已被另一个进程占用」（**误导**）。要让它可行得改 ``vector/base.py`` 协议 + 实现 + ``__init__``，并波及约 12 个鸭子类型假件；另加向量常驻内存（1024 维 float32 ≈ **4 KB/条**）。 |
| **新表（MySQL / SQLite）** | ❌ 可行但必要性不足：新表要落**六处登记**（``models`` / 迁移 / ``_CLEAN_ORDER`` / ``test_models_schema`` / ``verify.EXPECTED_TABLES`` / ``verify`` 里硬编码的 SQL 串 + 两处「11 张表」文案）；⚠️ ``DELETE … LIMIT`` 在本机 SQLite 3.50.4 **直接语法错误**（实测）⇒ TTL 清理只能走子查询形态。 |
| **进程内 ``OrderedDict``** | ✅ **采用**（本模块）。 |

## 失效面（§2.3 三件套 + S3 条目 ``166`` 的在途写定序）

1. **TTL**（``SEMANTIC_CACHE_TTL_SECONDS``，默认 ``86400``）：读时判
   ``now - created_at > ttl`` ⇒ 当未命中并删除。**按 ``created_at`` 现算，不在写入时烤一个
   ``expires_at``** ⇒ 改配置对已有条目**即刻生效**（调小立刻过期、调大立刻续命）。
   ``TTL <= 0`` 视为**关闭缓存**（:func:`enabled` 为假 ⇒ 三个入口各自首行早退）。
2. **容量**（``SEMANTIC_CACHE_MAX_ENTRIES``，默认 ``256``）：超出即 ``popitem(last=False)``
   （LRU；读命中要 ``move_to_end``）。
3. **存活校验**：见 ``entry.fingerprints_live``（命中时那一次 SELECT 是**唯一**的正确性支点）。
4. **在途写回的世代号**（``回归修复``）：``invalidate_all`` 同步推进模块级 ``_GENERATION``，
   写回（``store_snapshot``）只接受**排期世代 == 当前世代**的条目 —— 「失效前排的写」不得把
   已被清掉的条目复活（见 ``_GENERATION`` / ``store_snapshot`` / ``invalidate_all`` 的取舍说明）。

## 没有生命周期钩子，是实测结论

本仓**没有任何调度器**——全仓只有三处后台任务，且全是 per-request fire-and-forget
（``api/document_routes.py:493`` / ``usage/recorder.py:149`` / ``session/memory.py:554``）；
``main.py`` 的 lifespan 只做迁移 / 释放。⇒ 进程内 ``OrderedDict`` 的 TTL/LRU
**不需要任何生命周期钩子，也不需要 ``close_vector_store()`` 式的退出释放**。

## 进程内 ⇒ 重启即空

这是**特性不是缺陷**（桌面版重启后语料可能已变）。登记「进程内状态」的既有范式照
``app/db/redis.py`` 的模块 docstring（「无持久化、无跨进程、重启即丢」）。

## 关态 = 零行为（§2.1 的可操作版）

三个入口**各自首行**调 :func:`enabled` 早退（``reset_cache`` 例外：它是测试工具，
关态也该能清）。关态下：零查表、零写表、零 events 查询——「关 = 零变化」的计数判据就断在这里。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import entry as entry_mod
from app.cache.entry import CacheEntry
from app.core.config import settings

logger = logging.getLogger(__name__)

#: 缓存表（``key_hash -> CacheEntry``；``OrderedDict`` 的插入序即 LRU 序）
_STORE: OrderedDict[str, CacheEntry] = OrderedDict()

#: 并发保护：读命中要 ``move_to_end``、写要淘汰，两条都是**改序**操作。
#: 与 ``session/memory.py`` 的 ``_EXTRACT_LOCK`` 同型（模块级锁）。
_LOCK = asyncio.Lock()

#: 进程内计数（**只在模块内 + 日志**；面板化出口 = ``GET /api/agent/audit`` 的
#: ``counters.cache``——条目 `5.5` 已收口；**不进 `/api/health`**——那是冻结契约面，且 `5.1` 同口径）。
_COUNTS: dict[str, int] = {
    "hit": 0,
    "miss": 0,
    "stale": 0,
    "stored": 0,
    "evicted": 0,
    "expired": 0,
    "error": 0,
}
#: 缓存可承受的轮次窗口（§0.0 第 17 行：Agent 轮 / 带图轮 / refuse / clarify 一律不进缓存）
CACHEABLE_AGENT_MODES = ("off",)
#: **写回**侧的 kind 白名单（§2.5 明文：只有 ``normal``）——``refuse`` / ``clarify`` 走预置文案、
#: ``direct`` **无材料**，首版都不缓存（减少状态面）。写进证据的「有意限制」。
STORED_KINDS = ("normal",)

#: 失效世代号（``回归修复``）：:func:`invalidate_all` 每自称一次就 ``+1``（**含表里当时为空的
#: 情形**——表空不代表没有在途写）。在途写回带着**排期那一刻**的世代号回到 :func:`store_snapshot`，
#: 不等即在锁内整条丢弃 ⇒ "失效前排的写" 不可能在 ``drain_cache_tasks()`` 收官时复活旧条目。
#: 为什么是世代号而不是「把失效排进写队列等 drain」：``invalidate_all`` 必须是**同步**函数
#: （``app/agent/tools/_write.py`` 同步调用），定序会把「清缓存」变成异步动作并牵动 9 个调用点；
#: 世代号只需一个 int + 一次比对，不动 ``_LOCK`` 语义、也不动「已存在同键则跳过」。
_GENERATION: int = 0


def enabled() -> bool:
    """缓存总开关的**唯一读取点**（三个入口各自首行早退）。

    ``SEMANTIC_CACHE_TTL_SECONDS <= 0`` 也视为关闭——**TTL 是唯一的 0–1 新鲜度旋钮**：
    设 ``0`` 等于关掉本条（保留这个语义是为了让运维口径单一：想更保守就调小，
    想彻底不要缓存就调成 0 或把 ``SEMANTIC_CACHE_ENABLE`` 置 false）。
    ``SEMANTIC_CACHE_MAX_ENTRIES <= 0`` 同理（容量为零 = 什么都装不下）。
    """
    if not bool(getattr(settings, "SEMANTIC_CACHE_ENABLE", False)):
        return False
    if ttl_seconds() <= 0:
        return False
    return max_entries() > 0


def ttl_seconds() -> int:
    """TTL（秒）。**不吞 falsy 值**（``0`` 是合法取值 = 关闭，不是「没配」）。"""
    return _int_setting("SEMANTIC_CACHE_TTL_SECONDS", 86400)


def max_entries() -> int:
    """容量上限（条）。同样不吞 ``0``。"""
    return _int_setting("SEMANTIC_CACHE_MAX_ENTRIES", 256)


def _int_setting(name: str, default: int) -> int:
    """读一个 int 配置键；坏值回落默认（配置坏数据不该 500 掉用户这一轮）。"""
    raw = getattr(settings, name, default)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def failure_counts() -> dict[str, int]:
    """读进程内计数（诊断 / 用例用；**不是**对外接口）。

    键：``hit`` / ``miss`` / ``stale``（存活校验没过）/ ``stored`` / ``evicted``（LRU 淘汰）/
    ``expired``（TTL 过期）/ ``error``（读或写失败，一律按未命中降级）。
    """
    return dict(_COUNTS)


def reset_failure_counts() -> None:
    """清零全部计数（测试与诊断用）。"""
    for key in _COUNTS:
        _COUNTS[key] = 0


def is_cacheable_plan(plan) -> bool:
    """**收口侧**的完整判据：这一轮进不进缓存（查表与写回**共用同一份**）。

    三条全过才算「进缓存面」：

    - ``agent_mode == "off"``（Agent 轮编排状态面太大，命中要连整条编排一起重放）；
    - **无图**（保守排除：图本身影响落库）；
    - ``kind ∈`` :data:`STORED_KINDS`（只有 ``normal``）——``refuse`` / ``clarify`` 走**预置
      文案**、``direct`` **无材料**，首版都不缓存（减少状态面）。写进证据的「有意限制」。

    用 ``getattr`` 读字段：既有用例用 ``SimpleNamespace`` 当 ``TurnPlan`` 的替身，
    加个新字段就把老用例打成红是没必要的代价。

    ⚠️ ``chat_routes.build_cache_key`` 里那两行早退**就是本函数的前两个条件**（``kind``
    那条在 ``build_turn_plan`` 里判不了——那一刻还没有 ``plan``）——常量都取自本模块，
    两处不会各自漂移。
    """
    if str(getattr(plan, "agent_mode", "off") or "off") not in CACHEABLE_AGENT_MODES:
        return False
    if getattr(plan, "images", None) or []:
        return False
    return str(getattr(plan, "kind", "") or "") in STORED_KINDS


@dataclass
class CacheHit:
    """一次命中的完整读数（调用方据此**重放一轮**，不再调检索与模型）。"""

    #: 缓存条目本身（原始正文 / 思考全文都在里面）
    entry: CacheEntry
    #: **重建后的** ``Material`` 快照（编号 1..N 保留缓存时的值）
    materials: list
    #: 从 ``entry.retrieval_stats`` 重建的 :class:`RerankStats`；形状不认识时为 ``None``
    stats: object | None = None


async def lookup(db: AsyncSession, key: str | None) -> CacheHit | None:
    """查表 + 存活校验。**任何异常路径都当未命中**（I5：缓存绝不炸穿本轮）。

    命中返回 :class:`CacheHit`；未命中返回 ``None``（调用方走完整链路：检索与模型**都重跑**）。
    刻意**不做**「省检索但每次重调模型」那档中间态：它比全未命中只快一点（省下的正是最
    便宜的那一段：``VECTOR_TOP_K`` / ``FUSE_LIMIT`` 都只是几十条的量级），却要额外承担
    「缓存材料 + 现算正文」两套逻辑。**要么全用缓存，要么全重跑。**

    顺序：早退 → 取条目 → 版本 / TTL → 存活校验 → 重建 → ``move_to_end``（LRU）。
    存活校验在 ``move_to_end`` **之前**：没过的条目当场删掉（它已经不新鲜了，
    留着只会每轮白查一次 SELECT）。

    U6：存活查询会让出事件循环，返回后必须复验世代和同一条目身份；
    查询期间的同步失效不允许旧快照继续重放。
    """
    if not enabled() or not key:
        return None
    record = None
    try:
        async with _LOCK:
            record = _STORE.get(key)
            generation = current_generation()
            if record is None:
                _COUNTS["miss"] += 1
                return None
            if record.version != entry_mod.CACHE_SCHEMA_VERSION:
                _STORE.pop(key, None)
                _COUNTS["stale"] += 1
                logger.info("缓存条目结构版本不匹配，已丢弃（key=%s…）", key[:8])
                return None
            ttl = ttl_seconds()
            if time.monotonic() - float(record.created_at or 0.0) > ttl:
                _STORE.pop(key, None)
                _COUNTS["expired"] += 1
                logger.info("缓存条目已过 TTL（%ss），已删除（key=%s…）", ttl, key[:8])
                return None
            # 存活校验必须在**锁内**：它是唯一一个「读页面之外还会真的查库」的步骤，
            # 但更要紧的是——两条并发请求同时校验、其中一条删条目，另一条不能拿着
            # 已经作废的快照继续（写侧也走同一把锁，故互斥即可，不需要事务）。
            live = await entry_mod.fingerprints_live(db, record.fingerprints)
            if generation != current_generation() or _STORE.get(key) is not record:
                _COUNTS["stale"] += 1
                return None
            if not live:
                _STORE.pop(key, None)
                _COUNTS["stale"] += 1
                logger.info("缓存条目的引用已失效（块被删或内容已变），按未命中处理（key=%s…）", key[:8])
                return None
            _STORE.move_to_end(key)

            # 重建放在 `try` 里面（**不在锁外**：纯计算很快，而它能失败——见 except）。
            # §0.0 第 8 条把「反序列化失败」明确列进「一律当未命中」的三类之一，故这里
            # 不能让它漏出 try：漏出去就是一个 500（破 I5），而缓存本来是优化项。
            materials = entry_mod.deserialize_materials(record.materials)
            if len(materials) != len(record.materials or []):
                # 有条目被 `deserialize_materials` 跳过（形状坏掉）⇒ 这一轮的引用链会缺行。
                # 宁可当未命中重跑，也不能拿半份材料去现算 refs / 归因（那会做出悬空引用）。
                raise ValueError(
                    f"缓存材料重建不完整（{len(materials)}/{len(record.materials or [])}）"
                )
            stats = entry_mod.rebuild_stats(
                record.retrieval_stats, empty_reason=record.final_empty_reason
            )
    except Exception as exc:  # noqa: BLE001 —— 读失败 / 反序列化失败一律当未命中
        _COUNTS["error"] += 1
        logger.warning("缓存查表失败（本轮按未命中处理）：%s: %s", exc.__class__.__name__, exc)
        # 坏条目当场丢弃：留着只会每轮再炸一次（键是内容哈希，同键多半还会被命中）
        if key in _STORE:
            _STORE.pop(key, None)
        return None

    _COUNTS["hit"] += 1
    return CacheHit(entry=record, materials=materials, stats=stats)


async def store_snapshot(
    key: str | None, entry: CacheEntry, *, generation: int | None = None
) -> None:
    """写回一条缓存（未命中路径的收口侧）。**写失败一律吞**（只 warning + 计数）。

    四条口径：

    - **已存在同键则跳过**（第一次写入者胜出，避免两轮互相覆盖）；
    - **容量超出即淘汰最旧的**（``popitem(last=False)``，即 LRU 头）；
    - 写之前按 :data:`CACHE_SCHEMA_VERSION` 归一（调用方不必自己填）；
    - **世代校验**（``回归修复``）：端点传入本轮准备前的 :func:`current_generation`，
      直接排写未指定时由 ``chat_cache.schedule_cache_write`` 在排期时捕获；与此刻的
      :data:`_GENERATION` 不等 ⇒ 快照之后发生过 :func:`invalidate_all` ⇒ **整条丢弃**
      （INFO 日志；⛔ 不写表、不计 ``stored``）。
      ``generation=None`` = 调用方不走世代语义（既有直接调用点，行为一字不变）。

      ⚠️ **校验必须在锁内**：``async with _LOCK`` 的等待会让出事件循环，失效可能恰好插在
      「校验通过」与「落表」之间；而 enter 锁后到落表之间**没有 await**，``invalidate_all`` 也
      全程无 await ⇒ 锁内比对 + 这一条，把「在途写复活旧条目」彻底关死。
    """
    if not enabled() or not key:
        return
    try:
        entry.version = entry_mod.CACHE_SCHEMA_VERSION
        if not entry.created_at:
            entry.created_at = time.monotonic()
        capacity = max_entries()
        async with _LOCK:
            if generation is not None and generation != _GENERATION:
                logger.info(
                    "缓存写回已丢弃（快照世代 %d ≠ 当前世代 %d：快照后发生过失效）：key=%s…",
                    generation,
                    _GENERATION,
                    str(key)[:8],
                )
                return
            if key in _STORE:
                return
            _STORE[key] = entry
            while len(_STORE) > capacity:
                evicted_key, _ = _STORE.popitem(last=False)
                _COUNTS["evicted"] += 1
                logger.info("缓存容量已满（%d 条），淘汰最旧条目（key=%s…）", capacity, evicted_key[:8])
            _COUNTS["stored"] += 1
    except Exception as exc:  # noqa: BLE001 —— 写失败绝不影响本轮（I5）
        _COUNTS["error"] += 1
        logger.warning("缓存写回失败（本轮照常返回）：%s: %s", exc.__class__.__name__, exc)


def reset_cache() -> None:
    """清空缓存表（**测试工具**；关态也该能清，故不早退）。

    用例必须显式调它（进程内状态会跨用例泄漏——本仓踩过「进程全局计数 + 逐条断言」的同类坑）。
    ``tests/api/conftest.py`` 的 ``pin_semantic_cache_off`` autouse 钉子里也调一次。
    """
    _STORE.clear()


def current_generation() -> int:
    """当前失效世代号（``回归修复`` 的**唯一读法**）。

    调用方（``chat_cache.schedule_cache_write``）在**排期那一刻**捕获它，并原样交给
    :func:`store_snapshot`；:func:`invalidate_all` 由写路径在失效时推进它。
    读法本身不加锁：单个 int 的读在 Python 里是原子的，且「读到旧值」只会让一次在途写
    在锁内被再比一次（真正的判据在 :func:`store_snapshot` 的锁内）。
    """
    return _GENERATION


def invalidate_all(reason: str) -> int:
    """全量清空语义缓存（**产品入口**）；返回本次**清掉的条数**。

    ## 调用契约（逐字 · 本仓成文口径）

    **调用方 = 产品写路径**（阶段 S3 条目 ``157`` 落地后共 **9 个调用点 / 3 个文件**）：
    笔记域 ``session/note_service.py`` 的 新建 / 更新 / 软删，文档域 ``api/document_routes.py``
    的 上传 / 导入本地文件 / 重建索引 / 软删，回收站域 ``api/trash_routes.py`` 的 恢复 与
    ``_purge_one``（覆盖单条彻底删除与「清空回收站」）。
    **时机 = 写提交之后**（失败路径不得清）；形态 = **裸名 + 显式 import**（``ast.Name``
    可捕获，⛔ 不得写成 ``_cache_store.invalidate_all(...)`` 属性式）。

    ## 与 ``reset_cache`` 的关系

    ``reset_cache`` 的 docstring 自述是**测试工具**（``app/`` 生产调用点 = 0，调用全在
    ``tests/``），**不得**把它当产品 API 用；本函数是**独立命名、独立 docstring 的产品函数**，
    只是**内部复用**它的清除动作。``reset_cache`` 的既有语义与签名**一字未改**。

    ⚠️ **本函数推进的 ``_GENERATION`` 与 ``reset_cache`` 无关**：``reset_cache`` 只清表、
    **不**动世代（它是测试工具 ⇒ 世代在跨例上只增不减）；U6 ``lookup`` 复验世代，
    重置表仍由条目身份比对保护。**夹具口径**：``tests/cache/test_bug166_invalidate_ordering.py`` 的
    ``_clean_cache`` 只 ``reset_cache()`` + ``reset_failure_counts()``，且**没有任何一腿断言世代
    数值**（腿①–⑤ 全部落在 ``cache_size()`` / ``lookup()`` / 返回值 / 签名这四类接口面上）；
    判定是**相等比对**（``generation != _GENERATION``，不是阈值）⇒ 跨例累加不改变任何判据。

    ## 三条口径

    - **不早退**（照 ``reset_cache``）：**关态也该能清** —— 否则「清缓存」这一动作在关态下
      无从取证，而关态恰恰是出厂默认态；
    - **不吞异常**（与 ``lookup`` / ``store_snapshot`` 相反）：那两个是**优化路径**，失败必须
      降级成「本轮照常」；而本函数是**正确性动作**（调用方发起的显式失效），静默吞掉会让调用方
      以为已失效 ⇒ 必须让失败可见；
    - **同步函数**：表是进程内 ``OrderedDict``，本函数只做一次 ``clear()``，不需要持 ``_LOCK``
      （与 ``reset_cache`` 同口径）；同时**同步**推进 :data:`_GENERATION`（见下「世代号」）。

    ## 世代号（``回归修复``）

    本函数是**失效的分水岭**：先推进 ``_GENERATION``（**含 ``cleared == 0`` 的情形**——表里没条目
    不代表没有在途写），再清表。写回侧在**锁内**比对「排期世代」（:func:`store_snapshot`）⇒
    「失效前排的写」一律丢弃，``await drain_cache_tasks()`` 收官时旧条目**不会复活**。

    ⚠️ **取舍登记：世代号，不是「把失效排进写队列等 drain」**——后者要把本函数改成异步
    （``await``），而它被 ``agent/tools/_write.py` 的 ``_cache_store.invalidate_all(...)``
    **同步调用**，签名必须保持同步 ⇒ 会牵动 agent 面（越权 + 9 个调用点全要改）；
    世代号不动 ``_LOCK`` 语义、不动「已存在同键则跳过」，只多一个 int 与一次比对。
    ⚠️ **有意限制**：被丢弃的在途写**只记 INFO 日志、不新增计数器键**——审计面
    ``counters.cache`` 的键集被 ``tests/api/test_agent_audit_5_5.py:516/:599`` 钉成与
    ``failure_counts()`` 的键集**相等**，新增键要同笔改审计投影（不在本条目范围内）。

    ⚠️ **本函数只清空，不重算任何东西**；它**不感知**磁盘内容是否变化 —— 「谁在什么时机调它」
    由**调用方**决定：本仓调用点一律排在**写提交之后**（见上「调用契约」）。
    绕过服务层直接改数据库行仍不推进世代；U6 生产条目的完整来源校验
    会拦住已引用来源正文、构建身份及标题 / 分类 / 标签 / 知识库变化。
    """
    global _GENERATION
    _GENERATION += 1  # 失效分水岭：含 cleared == 0 的情形（表空 ≠ 没有在途写）
    cleared = cache_size()
    reset_cache()
    if cleared:
        logger.info("语义缓存已全量清空：%d 条（reason=%s）", cleared, reason)
    return cleared


def cache_size() -> int:
    """当前条数（诊断 / 用例断言用）。"""
    return len(_STORE)


__all__ = [
    "CACHEABLE_AGENT_MODES",
    "STORED_KINDS",
    "CacheHit",
    "cache_size",
    "current_generation",
    "enabled",
    "failure_counts",
    "invalidate_all",
    "is_cacheable_plan",
    "lookup",
    "max_entries",
    "reset_cache",
    "reset_failure_counts",
    "store_snapshot",
    "ttl_seconds",
]
