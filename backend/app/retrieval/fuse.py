"""RRF 融合（阶段 3 · 条目 3.1；阶段 5 · 条目 5.3 扩成**三路**）。

排名级融合：``score = Σ 1/(RRF_K + rank)``——与两路的**分数量纲无关**（向量余弦分与
BM25 分本来不可比），单路缺失也不会被记 0 分惩罚（老基线 ``rag/retriever.py::_fuse_rrf``
的同一策略，RRF_K 也是同一个 60）。

**同源去重**：同一个 ``chunk_id`` 在两路都出现时**合并成一条**，分数取两路贡献之和，
互补字段（正文 / 两个子分）从另一路补齐。这是条目 3.2「引用编号一致（I3）」的地基：
一份材料只可能拿到一个 ``[n]``。

**第三路（图通道，阶段 5 · 5.3）**：``graph_hits`` 是可选的第三路，命中同一 ``chunk_id``
时**与另两路同款**——合并成一条、RRF 贡献相加。四点口径钉死：

1. **不传 / 传空 ⇒ 行为与两路时逐字一致**（既有 60+ 条检索用例全走两路，签名一改就会连带打红；
   故它是 ``limit`` **之后**的 keyword-only 形参，且空值时连分支都不进）；
2. **一路一个 ``chunk_id`` 最多贡献一项**：图通道在
   :func:`app.retrieval.graph.search_graph` 里已经按 ``chunk_id`` 去重并只留最强那条，
   所以「被三个种子带到」不会算三次——这正是「只计一次 RRF」的落点；
3. **跨路取和而不是取最大**：RRF 的既有语义就是「多路都认为它相关 ⇒ 排更前」，
   改成取最大会**改变两路命中的既有排名**（那是 3.1 的已验收行为，本条目不改它）。
   ``graph_score`` 字段本身按「谁非空用谁」补齐，只作证据，不参与计分；
4. **``graph_weight`` 是第三路的通道系数**（默认 ``1.0`` = 与另两路同权）。检索层传的是
   ``GRAPHRAG_SCORE_FACTOR``：它让图通道的 RRF 贡献整体缩小，从而**结构性保证**
   「主命中永远优先」（口径红线）——不是靠调参碰巧对，见 :func:`app.retrieval.graph._channel_weight`
   的算式与那条不变式用例。缺省 ``1.0`` 时行为与「不加权」逐字相同（不传图通道的老调用点无感）。

输出排序**稳定可重复**：先按融合分降序，再按 ``chunk_id`` 升序做 tie-break——同一批输入
两次调用结果逐条相同（RRF 分相同的并列情形由后者定序，不依赖字典遍历顺序）。
"""

from __future__ import annotations

from app.retrieval.types import FUSE_LIMIT, RRF_K, Material


def fuse(
    vector_hits: list[Material],
    keyword_hits: list[Material],
    limit: int = FUSE_LIMIT,
    *,
    graph_hits: list[Material] | None = None,
    graph_weight: float = 1.0,
) -> list[Material]:
    """两路 / 三路融合；返回按融合分降序、至多 ``limit`` 条的材料（已去重）。

    ``graph_hits`` 缺省 ``None``（或空列表）时**走的还是原来那条两路代码路径**——
    逐个 ``if`` 都没有多出来，只是 ``channels`` 里少一组。
    """
    scores: dict[tuple, float] = {}
    picked: dict[tuple, Material] = {}

    channels: list[tuple[float, list[Material]]] = [
        (1.0, vector_hits or []),
        (1.0, keyword_hits or []),
    ]
    if graph_hits:
        channels.append((float(graph_weight), graph_hits))

    for weight, hits in channels:
        seen: set[tuple] = set()
        for rank, hit in enumerate(hits, start=1):
            key = hit.retrieval_key
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + weight / (RRF_K + rank)
            merged = picked.get(key)
            if merged is None:
                picked[key] = hit
                continue
            # 多路都命中：互补字段从另一路补齐（不覆盖已有值）
            if merged.vector_score is None:
                merged.vector_score = hit.vector_score
            if merged.keyword_score is None:
                merged.keyword_score = hit.keyword_score
            if merged.graph_score is None:
                merged.graph_score = hit.graph_score
            if not merged.content:
                merged.content = hit.content
            if not merged.filename:
                merged.filename = hit.filename
            if merged.page is None:
                merged.page = hit.page

    ordered = sorted(picked.values(), key=lambda item: (
        -scores[item.retrieval_key], int(item.chunk_id), item.fragment_ordinal or 0,
        item.source_type,
    ))
    for item in ordered:
        item.score = scores[item.retrieval_key]
    return ordered[: max(0, int(limit))]


__all__ = ["fuse"]
