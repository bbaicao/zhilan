"""检索层入口：双路召回 → RRF → 相关扩链 → 正文窗口 → 一次重排 → 编号与渲染。

本包是 `api → retrieval → vector/session/db` 里的那一层：只依赖 ``app.vector`` 协议与
ORM 模型，**不 import qdrant_client**，也不反向依赖 api/agent/generation。

对外只暴露一个函数 :func:`retrieve`（``chat_routes.retrieve_materials`` 的唯一实现来源）：

```
retrieve(session, question, filters) -> (materials, materials_text)
```

- ``materials``：**本轮材料快照**（已在融合后编号 1..N）——条目 3.2 的 Ref 与
  ``message_attributions`` 都从这一份取，保证「模型看到的 [n] = 引用卡片的编号」（I3）；
- ``materials_text``：渲染好的资料区正文，同步与流式**共用同一份**（接线四条之 2/3）。

两路各自的降级都在包内消化：向量不可用（未配置 embed / 服务失败 / 向量库打不开）只记警告并
**退回纯关键词路**；关键词路为 0 条时向量路照常。两条路都空 ⇒ 返回 ``([], "")``，由调用方按
§3.8 落到 ``direct`` 或 ``refuse``——检索层**不替调用方决定分路**。

阶段 4 · 条目 4.5.2 补两处（都不改已绿的融合 / 重排语义）：

- **笔记来源进关键词路**（``回归修复`` 的 B-lite）：语料从「文档分块」扩成
  「文档分块 + 非空笔记」，见 :mod:`app.retrieval.keyword`；
- **双向链接一跳扩展**（``回归修复``）：U3 在融合之后先取有词项依据的链接片段，
  主命中和扩链使用同一窗口与重排门槛，最后仍由主命中优先占名额。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.retrieval import fuse as fuse_mod
from app.retrieval import graph, keyword, link, rerank, vector, windows
from app.retrieval.types import (
    FUSE_LIMIT,
    KEYWORD_TOP_K,
    RRF_K,
    VECTOR_TOP_K,
    Material,
    new_retrieval_id,
    normalize_body,
    number,
    render_materials,
)

log = logging.getLogger(__name__)

__all__ = [
    "FUSE_LIMIT",
    "KEYWORD_TOP_K",
    "Material",
    "RRF_K",
    "VECTOR_TOP_K",
    "expand_materials",
    "fuse",
    "graph",
    "link_budget",
    "new_retrieval_id",
    "normalize_body",
    "number",
    "render_materials",
    "retrieve",
    "search_keywords",
    "search_vectors",
]


async def search_keywords(session: AsyncSession, question: str, filters: dict | None = None):
    """包级快捷入口（关键词路）：见 :func:`keyword.search_keywords`。"""
    return await keyword.search_keywords(session, question, filters)


async def search_vectors(session: AsyncSession, question: str, filters: dict | None = None):
    """包级快捷入口（向量路）：见 :func:`vector.search_vectors`。"""
    return await vector.search_vectors(session, question, filters)


def fuse(
    vector_hits: list[Material],
    keyword_hits: list[Material],
    limit: int = FUSE_LIMIT,
    *,
    graph_hits: list[Material] | None = None,
    graph_weight: float = 1.0,
):
    """包级快捷入口（RRF 融合）：见 :func:`fuse.fuse`。

    ⚠️ ``graph_hits`` / ``graph_weight`` **必须 keyword-only**：本行原来是
    ``fuse_mod.fuse(vector_hits, keyword_hits, limit)``，把新形参插到 ``limit`` 之前会
    **静默错位**（测试全走模块函数，发现不了）。
    """
    return fuse_mod.fuse(
        vector_hits, keyword_hits, limit, graph_hits=graph_hits, graph_weight=graph_weight
    )


async def expand_materials(
    session: AsyncSession, materials: list[Material], **kwargs
) -> list[Material]:
    """包级快捷入口（双向链接一跳扩展）：见 :func:`link.expand_materials`。"""
    return await link.expand_materials(session, materials, **kwargs)


link_budget = link.link_budget


async def retrieve(
    session: AsyncSession, question: str, filters: dict | None = None,
    *, stats: rerank.RerankStats | None = None,
) -> tuple[list[Material], str, str | None]:
    """双路召回 + 融合 + 可选重排，返回 ``(材料快照, 渲染正文, 降级提示)``。

    ``降级提示`` 非空表示**向量路已降级**（未配置 embed / 服务失败 / 向量库打不开），
    此时本次只有关键词路的产出——条目 3.3 的 trace 器官用它记 ``retrieval/result.degraded``。

    两路**顺序执行**（不是 ``asyncio.gather``）：它们共用同一个 ``AsyncSession``，
    并发使用同一个会话是不安全的（异步驱动不允许同一连接并发执行）。

    图通道进同一次 RRF；链接候选有界预取，和主命中共同扩窗及重排，
    全部通过门槛后再以主命中优先分配最多十二条快照。
    """
    text = (question or "").strip()
    stats = stats if stats is not None else rerank.RerankStats()
    if not text:
        await rerank.apply_rerank(text, [], stats=stats)
        return [], "", None

    keyword_hits = await keyword.search_keywords(session, text, filters)
    vector_hits, warning = await vector.search_vectors(session, text, filters)
    if warning:
        log.warning("%s", warning)

    # --- 图通道（阶段 5 · 条目 5.3）：第三路，**进 RRF 融合** -------------------
    # ⚠️ **关态连 `search_graph` 都不调**（A11 明文要求「零 search_graph 调用」）：
    # 只靠模块内早退做不到「调用计数 = 0」，所以这一句 `graph.enabled()` 是必需的。
    # 判据仍**只有一处**（`graph.enabled()` 是唯一的开关读取点），这里只是提前把它读一次。
    # 名额口径 = `min(GRAPHRAG_TOP_K, FUSE_LIMIT − 主命中条数)`，「主命中一个不删」；
    # 已在主命中里的 `chunk_id` 不再由图通道带一遍（名额留给主命中**拿不到**的块，A4）。
    graph_hits: list[Material] = []
    if graph.enabled():
        main_ids = {int(item.chunk_id) for item in [*keyword_hits, *vector_hits]}
        graph_hits = await graph.search_graph(
            session,
            text,
            filters,
            main_count=len(main_ids),
            exclude_ids=main_ids,
        )

    fused = fuse_mod.fuse(
        vector_hits,
        keyword_hits,
        graph_hits=graph_hits or None,
        graph_weight=graph._channel_weight(),
    )
    from app.retrieval.types import material_identities
    stats.recall = {
        "vector": material_identities(vector_hits),
        "keyword": material_identities(keyword_hits),
        "graph": material_identities(graph_hits),
        "fused": material_identities(fused),
    }
    main_sources = {item.source_ref for item in fused}
    max_links = max(0, int(getattr(settings, "RAG_LINK_MAX", 0) or 0))
    candidate_cap = FUSE_LIMIT + min(4, max_links)
    # ponytail: 最多额外四个总候选位，使重排删出的空位可补取；若要扩大召回再调预算，
    # 不在这里循环请求重排。有效范围原样传给扩链及窗口，避免范围被尾部步骤绕过。
    candidates = await link.expand_materials(
        session, fused, filters=filters, question=text, cap=candidate_cap,
        max_links=max_links, stats=stats,
    )
    candidates = await windows.expand_windows(session, candidates, text, filters)
    ranked = await rerank.apply_rerank(text, candidates, stats=stats)
    main = [item for item in ranked if item.source_ref in main_sources]
    extra = [item for item in ranked if item.source_ref not in main_sources]
    if stats.status != "applied":
        gated = link.relevant_materials(text, extra)
        if stats.links is not None:
            stats.links["gate_removed"] += len(extra) - len(gated)
            stats.links["degraded"] = stats.links["degraded"] or stats.status in {
                "failed", "unbound", "shape_mismatch",
            }
        extra = gated
    room = link.link_budget(len(main), cap=FUSE_LIMIT, max_links=max_links)
    kept = main[:FUSE_LIMIT] + extra[:room]
    if stats.links is not None:
        stats.links["kept"] = len(extra[:room])
        stats.links["gate"] = "keyword+rerank" if stats.status == "applied" else "keyword"
    number(kept)
    stats.selected = material_identities(kept)
    return kept, render_materials(kept), warning
