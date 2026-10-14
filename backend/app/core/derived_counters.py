"""派生数据失败的**进程内计数出口**（`回归修复` 的可见面）。

## 为什么要有这一层

本仓有一批「**派生数据**」：笔记双链边、文档侧链接边、图谱边、缓存快照、事件行、审计行、
会话摘要（`chats.context_summary` / `compressed_up_to`）。
它们的设计口径是**失败不反噬主流程**（I5）—— 保存成功、写入失败、只记一条 `log.warning`。
这条口径本身是对的，但**代价没有被登记**：`回归修复`（原登记 `S4`）登记的就是这一点。

最严重的后果不是「丢了一条边」，而是**任何派生故障都无法被观测**：
· `回归修复`（笔记双链）就是活样本 —— 它在真机上被判 FAIL，而**判据面与日志面都没有任何动静**；
· 叠加 `回归修复`（冻结态应用层日志**不进控制台**）⇒ 「有 log」**等于不可见**。

`回归修复` 的 **S10**（2026-09-26 接线）：长会话压缩的**摘要落库失败**
（`session/compaction.py::compact_history` 的 store 分支）原先只有压缩断路器 `_BREAKER` 里一个
**按 `chat_id` 分桶的连续失败数**（不进 `counters`），唯一出口 `CompactionResult.warnings`
又在 `session/service.py::build_model_history` 里被一句 `logger.warning` 吃掉
⇒ 补键 :data:`COUNTER_KEYS` 的 `compaction_summary`（**按派生链命名**：一眼看出是哪条链坏了；
不叫 `summary_store` 那种指向存储层的名字）。**只增不改义**：不扩 `bump` / `ordered_counts`
的形状，不动 `_BREAKER` 的任何语义（熔断状态仍是纯进程内优化）。

**2026-09-28 批次三（`083` 收口）**：再**只增**两键 `trace_persist` / `tool_result_channel`
（**八键 → 十键**，见 :data:`COUNTER_KEYS`）—— 前者是「本轮尾部溯源/审计落库失败」，
后者是「工具结果通道降级」。既有八键的**名/义/顺序逐字不动**；两处独立键集锚
（`tests/api/test_bug083_s10_breaker.py::EXPECTED_DERIVED_KEYS` /
`tests/api/test_bug083_counter_keys_ten.py::TEN_KEYS`）与读侧回退表
（`app/agent/audit.py::COUNTER_KEYS["derived"]`）**同笔显式跟随**。

因此本模块提供**唯一**的一处进程内计数 + 快照出口，语义与既有
`graph.failure_counts` / `cache.failure_counts` / `memory.failure_counts` **逐字同型**
（同一套 `_COUNTERS` + `_bump` + 复制返回的范式），并接进 `GET /api/agent/audit` 的
`counters` 顶层键 —— **不新增端点、不新增帧键、不动 `/api/health`**。

## 口径（三条，不得含糊）

1. **进程内、自启动以来累计** —— 不是持久化统计（与既有三个计数器同口径）；
2. **只记「被吞掉的失败」**，不记成功次数，也**不改任何调用方的控制流**：
   本模块的 `bump()` 是**幂等无副作用**的一行（`dict.get` + 赋值），**绝不抛**；
3. **键是稳定的**（见 :data:`COUNTER_KEYS`）：新增键**只增不改义**，缺席一律按 0 读。

## 分层

落 `app/core/` 是因为**下游两个层都要用它**：
`app/session/note_service.py`（session 层）与 `app/pipeline/wikilink.py`、`app/pipeline/ingest.py`
（pipeline 层）。按分包边界（`api → agent/retrieval/generation/routing → pipeline/vector/session → db/core`），
**core 是最底层** ⇒ 两层 import 它都是**向下**，不产生任何反向边。本模块**纯标准库、零本仓 import**。
"""

from __future__ import annotations

from typing import Any

#: 稳定键序（缺席即 0；实现里多出来的键照旧透传 ⇒ **只增不改义**）。
#: 键名刻意写得**能一眼看出是哪条派生链**，而不是笼统的 `failures`：
#: 三个计数器合在一起才回答得出「今天是双链坏了还是向量坏了」。
COUNTER_KEYS: tuple[str, ...] = (
    "note_links",      # 笔记双链边维护失败（note_service.maintain_links / wikilink.maintain_note_links / wikilink 清理边）
    "doc_links",       # 文档侧链接边维护失败（pipeline/ingest 的派生边写入）
    "compaction_summary",  # 压缩链的摘要落库失败（session/compaction.py 的 store_failed 分支 · S10）
    "usage_events",    # 用量事件落库失败（usage/recorder.py 四个吞点 · S6–S9 · 2026-09-27 批次）
    "graph_view_edges",  # 图谱视图边丢弃（pipeline/knowledge_graph.py 源/目标缺失 ⇒ 边画不出来 · S1）
    "pdf_ocr_fallback",  # PDF 的 OCR 兜底未完成/回落（parsing.py 缺页补齐五退路 + 整篇 MinerU 回落 · S11–S13 · 2026-09-27 第二批）
    "web_fetch_degrade",  # 网页抓取降级（web/client.py 清洗回落/标题回落/代理 fail-open 不 pin · S18–S19 · 2026-09-27 第二批）
    "stale_vector_points",  # 旧向量点删除失败（pipeline/ingest._delete_stale_points ⇒ 孤儿点挤占向量检索 · 回归修复 · 2026-09-27 缺陷清单-2 阶段二）
    # 🔴 2026-09-28 批次三（`083` 收口 · Lead 定稿键法）：**只增**两键（八键 → 十键），
    # 既有八键的**名/义/顺序逐字不动**，两新键只准**追加在尾部**。
    # 键集钉在两处独立锚（都要显式跟随）：`tests/api/test_bug083_s10_breaker.py::EXPECTED_DERIVED_KEYS`
    # 与 `tests/api/test_bug083_counter_keys_ten.py::TEN_KEYS`。
    # ⚠️ `retrieval/link.py` 的「链接扩展失败，已静默跳过」(#31) **不另开键** ⇒ 并入既有
    # `doc_links`（扩链定义 = 文档侧链接边维护）；`pipeline/clean_status.py` 的
    # `except OSError`(S14) 判定为**不收**（fail-closed，不是吞点 ⇒ 不写码）。
    "trace_persist",   # 本轮尾部溯源/审计落库失败（`chat_routes` 归因落库 #5 + 审计回填 #6）
    "tool_result_channel",  # 工具结果通道降级（`runner` 通道构造失败 #24 + 写通道三键不全 #29 + `tool_artifacts._fail()` #30）
    # 🔴 2026-10-08 专项「导入资料自动连线」：**只增**第十一键（十键 → 十一键），
    # 既有十键的**名/义/顺序逐字不动**，新键只准**追加在尾部**。
    "similar_links",  # 相似边维护链降级（pipeline/graphlink.py：代表向量算不出的分块/文档、
                       # 单库超 `GRAPHLINK_MAX_DOCS_PER_KB` 跳过、水印不符未提交、异常回滚）
)

#: 模块内计数（**进程全局**；与 graph/cache/memory 同型）。
_COUNTERS: dict[str, int] = {}


def bump(name: str, amount: int = 1) -> None:
    """把派生失败计数 +1（**绝不抛**：观测项不该让被观测的调用炸掉，I5 同型）。

    未知键**照记**（不丢信息）；但调用方应优先用 :data:`COUNTER_KEYS` 里的键。
    """
    try:
        _COUNTERS[str(name)] = _COUNTERS.get(str(name), 0) + int(amount)
    except Exception:  # noqa: BLE001 —— 计数器是最不该炸的东西
        pass


def failure_counts() -> dict[str, int]:
    """模块内计数快照（**复制**，调用方改不动内部状态）—— 与既有三个 `failure_counts` 同型。"""
    return dict(_COUNTERS)


def reset_failure_counts() -> None:
    """清零。⚠️ **必须在用例夹具里调**（本仓踩过「进程全局计数跨用例泄漏」）。"""
    _COUNTERS.clear()


def ordered_counts(raw: Any = None) -> dict[str, int]:
    """按 :data:`COUNTER_KEYS` 补 0，再把 ``raw`` 里多出来的键原样并入（值坏掉就跳过）。

    与 `audit._fill_counters` 同型：**缺席即 0、多余键透传**，保证 `counters` 的形状稳定。
    """
    source = raw if isinstance(raw, dict) else failure_counts()
    out: dict[str, int] = {key: 0 for key in COUNTER_KEYS}
    for key, value in source.items():
        try:
            out[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


__all__ = [
    "COUNTER_KEYS",
    "bump",
    "failure_counts",
    "ordered_counts",
    "reset_failure_counts",
]
