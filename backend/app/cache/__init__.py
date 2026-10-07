"""语义缓存（阶段 5 · 条目 `5.2`）：**路由后、检索前**的进程内精确键缓存。

## 一句话职责

``SEMANTIC_CACHE_ENABLE`` 打开时，「同一个问题 + 同一套范围 + 同一套上下文」的第二轮
**直接返回缓存里的整轮答案**——**检索与模型一起省**（既省延迟，又省钱）。
**默认关 = 逐字零影响**（§2.1 三条判据）；**不新增端点、不改 02、``client/`` 仍冻结（已登记例外恰一条 = 6.2 的 ``client/app.py``；⚠️ 2026-09-28 勘误：本行原写「不解冻 ``frontend/src``」，``frontend/src`` 已于 **2026-09-24** 由业主拍板解冻）、
不改冻结提示词、不新增依赖**（``gptcache`` 整包不可引入：它的启动链硬依赖
``numpy`` + ``cachetools`` + ``requests``，且 ``_check_dependency`` 缺库会**自己 `pip install`**）。

## 对外就 3 个接口

| 接口 | 职责 |
|---|---|
| :func:`lookup` | 查表 + **存活校验**；命中返回 :class:`app.cache.store.CacheHit`，否则 ``None`` |
| :func:`store_snapshot` | 未命中路径的收口侧**写回**一条 |
| :func:`reset_cache` | 清空（测试工具） |
| :func:`invalidate_all` | 清空（**产品入口**；调用契约见函数 docstring —— 语义缓存写路径的失效面，9 个调用点） |

其余一律 ``_`` 前缀（``store`` / ``entry`` 两个子模块里的辅助函数是**包内公开**的，
供单测直接断言纯函数语义；上游 ``api`` 只需上面这三个）。

## 首版是**精确键缓存**，不是向量相似检索

当前实现使用精确键，不启用向量相似检索：
相似度扩展点**只在 ``app/cache/entry.py`` 的模块 docstring 里预留**（照 ``GPTCache`` 的
``SimilarityEvaluation`` 协议 + ``range()`` 归一化阈值的形状），**不写死接口假装它在**。
完整理由与实测代价对照见该 docstring。

## 缓存键（10 个成分，缺一不可）

① 规范化问题 / ② 范围（``sort_keys=True`` 稳定序列化） / ③ 生效 ``agent_mode`` /
④ 排序后 ``skill_ids`` / ⑤ 模型身份 / ⑥ **``memory_text``（5.1 注入段）** /
⑦ ``history`` 内容指纹 / ⑧ 是否带图 / ⑨ **检索配置指纹**（embed+rerank 模型与重排门槛） /
⑩ **同轮路由 ``source_type``**（库内检索与库外直连**不得互相复用**，回归修复）。

⚠️ 第 ⑥ 条是本条**唯一**的跨条目改动：``chat_routes`` 的缓存点必须排在
``memory.recall`` + ``render_block`` **之后**——否则「第一轮无记忆时算出的答案」会在记忆
写入后被复用 ⇒ **5.1 的注入被 5.2 静默吞掉**（两个增强互斥）。

## 失效

**TTL + 容量 LRU + ``chunk_id`` 存活 _及内容指纹_ 校验**三件套，见 :mod:`app.cache.store`。

**写路径全失效**：笔记 / 材料 / 回收站三条写域的落库收口都在**写提交之后**调
:func:`invalidate_all` ⇒ 语料新增、笔记元数据改动、恢复与彻底删除都让旧键立即作废
（正确性优先）。这段取代首版「新增材料后旧键仍会命中」那条**有意限制**。

⚠️ **残余面**：绕过服务层直改数据库行不触发失效（存活校验只比 ``Note.content`` 三候选指纹
与块内容指纹，不看标题 / 分类 / 标签）——已登记，不在本条失效面内。
"""

from __future__ import annotations

from app.cache.store import (
    CacheHit,
    cache_size,
    enabled,
    failure_counts,
    invalidate_all,
    is_cacheable_plan,
    lookup,
    max_entries,
    reset_cache,
    reset_failure_counts,
    store_snapshot,
    ttl_seconds,
)

__all__ = [
    "CacheHit",
    "cache_size",
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
