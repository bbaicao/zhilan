"""关系图谱（任务清单 16）：全库节点 + 链接边的组装。

沿用老基线的全图形状；U1 统一库与实体身份，U5 仅加载图所需字段并共享请求内邻接。
v2 的 ``pipeline/`` 对应老仓库的 ``rag/``，检索通道在上层复用邻接而不反向依赖。

节点：全部 ``Note`` + ``Document``；未解析的 ``[[标题]]`` 以 ghost 节点出现（Obsidian 语义）。
边：

- ``wiki``：条目 13 的 ``wiki_links``（已解析连到真实节点，未解析连到 ghost）；
- ``source``：条目 29 的 ``Note.source_document_id`` 溯源。

大数据量先全量给前端，服务端不做布局与图算法；筛选裁剪由前端在全量上做。
不做未链接提及的模糊匹配边（与条目 14「先不做」一致）。

**回收站（软删）内容不进图**（条目 25）：软删的笔记 / 文档既不产生节点，指向它们的
边也直接丢弃、**不转成幽灵**——转幽灵会带着标题出现在图里，等于把回收站的内容又露出来了。
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import and_, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import derived_counters
from app.models import Document, DocumentSimilarityEdge, IndexBuild, Note, WikiLink
from app.pipeline import graphlink
from app.pipeline.wikilink import _stem, normalize_title, other_endpoint

GraphKey = tuple[int, str, int]  # kb_id, source_type, source_id
Adjacency = dict[GraphKey, list[GraphKey]]


def wiki_adjacency(edges: Iterable) -> Adjacency:
    """已解析边建双向邻接，保留边表顺序；类型与库都是身份的一部分。"""
    adjacency: Adjacency = {}
    seen: dict[GraphKey, set[GraphKey]] = {}
    for edge in edges:
        source = (edge.source_type, int(edge.source_id))
        target = other_endpoint(edge, source)
        if target is None:
            continue
        kb_id = int(edge.kb_id or 1)
        left, right = (kb_id, *source), (kb_id, *target)
        for origin, neighbor in ((left, right), (right, left)):
            if neighbor not in seen.setdefault(origin, set()):
                seen[origin].add(neighbor)
                adjacency.setdefault(origin, []).append(neighbor)
    return adjacency


def wiki_neighbors(adjacency: Adjacency, seed: GraphKey, *, hops: int = 1) -> list[GraphKey]:
    """同一邻接快照查一／两跳；不重扫边，环与多条路径只返回一次。"""
    if hops not in (1, 2):
        raise ValueError("wiki hops must be 1 or 2")
    seen = {seed}
    frontier = [seed]
    result: list[GraphKey] = []
    for _ in range(hops):
        following: list[GraphKey] = []
        for origin in frontier:
            for neighbor in adjacency.get(origin, []):
                if neighbor[0] == seed[0] and neighbor not in seen:
                    seen.add(neighbor)
                    following.append(neighbor)
                    result.append(neighbor)
        frontier = following
    return result


async def load_wiki_adjacency(
    db: AsyncSession, seeds: Iterable[GraphKey], *, hops: int = 1,
) -> Adjacency:
    """请求内批量取边；一跳调用方验证种子和目标，两跳在扩中间点前验证活跃及库。"""
    if hops not in (1, 2):
        raise ValueError("wiki hops must be 1 or 2")
    frontier = list(dict.fromkeys(
        (int(kb_id or 1), kind, int(source_id))
        for kb_id, kind, source_id in seeds if kind in ("note", "document")
    ))
    loaded: set[GraphKey] = set()
    rows: dict[int, object] = {}
    adjacency: Adjacency = {}
    for _ in range(hops):
        # ponytail: IN 参数每批最多 128 个种子，无边数截断；更大规模仍按批加载。
        for start in range(0, len(frontier), 128):
            batch = frontier[start:start + 128]
            scopes: dict[int, dict[str, list[int]]] = {}
            for kb_id, kind, source_id in batch:
                scopes.setdefault(kb_id, {}).setdefault(kind, []).append(source_id)
            clauses = []
            for kb_id, kinds in scopes.items():
                incident = []
                for kind, ids in kinds.items():
                    target = WikiLink.target_note_id if kind == "note" else WikiLink.target_document_id
                    incident.extend((
                        and_(WikiLink.source_type == kind, WikiLink.source_id.in_(ids)),
                        target.in_(ids),
                    ))
                clauses.append(and_(WikiLink.kb_id == kb_id, or_(*incident)))
            statement = select(
                WikiLink.id, WikiLink.source_type, WikiLink.source_id, WikiLink.kb_id,
                WikiLink.target_note_id, WikiLink.target_document_id, WikiLink.resolved,
            ).where(WikiLink.resolved.is_(True), or_(*clauses)).order_by(WikiLink.id)
            for row in (await db.execute(statement)).all():
                rows[int(row.id)] = row
        loaded.update(frontier)
        adjacency = wiki_adjacency(rows[key] for key in sorted(rows))
        if hops == 2 and adjacency:
            active: set[GraphKey] = set()
            keys = list(adjacency)
            for start in range(0, len(keys), 128):
                batch = keys[start:start + 128]
                note_ids = [sid for _, kind, sid in batch if kind == "note"]
                doc_ids = [sid for _, kind, sid in batch if kind == "document"]
                statement = union_all(
                    select(literal("note"), Note.id, Note.kb_id).where(
                        Note.id.in_(note_ids), Note.deleted_at.is_(None),
                    ),
                    select(literal("document"), Document.id, Document.kb_id).where(
                        Document.id.in_(doc_ids), Document.deleted_at.is_(None),
                    ),
                )
                active.update(
                    (int(kb_id or 1), kind, int(sid))
                    for kind, sid, kb_id in (await db.execute(statement)).all()
                )
            adjacency = {
                origin: [neighbor for neighbor in neighbors if neighbor in active]
                for origin, neighbors in adjacency.items() if origin in active
            }
        frontier = [
            key for key in dict.fromkeys(
                neighbor for seed in frontier for neighbor in adjacency.get(seed, [])
            ) if key not in loaded
        ]
        if not frontier:
            break
    return adjacency


def node_id(
    kind: str, source_id: int | None = None, title: str | None = None, *, kb_id: int = 1
) -> str:
    """实体身份为类型＋id；未解析身份为库＋归一标题。"""
    if kind == "ghost":
        return f"ghost:{int(kb_id or 1)}:{normalize_title(title)}"
    return f"{kind}:{int(source_id or 0)}"


def tags_list(raw) -> list[str]:
    """JSON tags 列归一成字符串列表（历史可能写成 dict，宽容读取）。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, dict):
        for key in ("items", "tags", "list"):
            value = raw.get(key)
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
        return [str(key).strip() for key in raw.keys() if str(key).strip()]
    return []


def _build_dim(fingerprint) -> int:
    """当前构建的向量维度（`fingerprint.embedding.dim`）；没有该身份（legacy 构建）⇒ 0 = 未知。

    `dim` 不是一个独立列 —— 它随 `IndexBuild.fingerprint`（`embedding_identity`）走，这里按
    同一份身份读出来与边表记录的 `dim` 比对（P2-B）。
    """
    if not isinstance(fingerprint, dict):
        return 0
    embedding = fingerprint.get("embedding")
    if not isinstance(embedding, dict):
        return 0
    try:
        return int(embedding.get("dim") or 0)
    except (TypeError, ValueError):
        return 0


async def build_graph(db: AsyncSession) -> dict:
    """组装全库图谱：节点列表 + 边列表 + 规模统计。回收站（软删）内容不进图（条目 25）。

    返回的是**纯 dict**（``nodes`` / ``edges`` / ``stats``），由 HTTP 面套 pydantic 模型——
    与老基线同形，图算法 / 布局与契约字段的解耦点就在这里。
    """
    notes = (await db.execute(select(
        Note.id, Note.title, Note.category, Note.tags, Note.kb_id, Note.source_document_id,
    ).where(Note.deleted_at.is_(None)).order_by(Note.id))).all()
    docs = (await db.execute(select(
        Document.id, Document.filename, Document.category, Document.tags, Document.kb_id,
    ).where(Document.deleted_at.is_(None)).order_by(Document.id))).all()
    note_ids = {int(item.id) for item in notes}
    doc_ids = {int(item.id) for item in docs}
    # 软删目标的 id 集合：指向它们的边直接丢弃，而不是落到「未创建」幽灵——
    # 幽灵会带着标题出现在图里，等于把回收站的内容又露出来了
    trashed_notes = (
        await db.execute(select(Note.id, Note.kb_id, Note.title).where(Note.deleted_at.is_not(None)))
    ).all()
    trashed_docs = (
        await db.execute(
            select(Document.id, Document.kb_id, Document.filename).where(Document.deleted_at.is_not(None))
        )
    ).all()
    trashed_note_ids = {int(sid) for sid, _, _ in trashed_notes}
    trashed_doc_ids = {int(sid) for sid, _, _ in trashed_docs}
    trashed_titles = {
        (int(kb_id or 1), normalize_title(title)) for _, kb_id, title in trashed_notes
    } | {
        (int(kb_id or 1), normalize_title(_stem(filename))) for _, kb_id, filename in trashed_docs
    }

    nodes: list[dict] = []
    for item in notes:
        nodes.append(
            {
                "id": node_id("note", item.id),
                "type": "note",
                "source_id": int(item.id),
                "title": (item.title or "").strip() or f"笔记 {item.id}",
                "category": item.category or "",
                "tags": tags_list(item.tags),
                "kb_id": int(item.kb_id or 1),
                "degree": 0,
            }
        )
    for item in docs:
        nodes.append(
            {
                "id": node_id("document", item.id),
                "type": "document",
                "source_id": int(item.id),
                "title": (item.filename or "").strip() or f"文档 {item.id}",
                "category": item.category or "",
                "tags": tags_list(item.tags),
                "kb_id": int(item.kb_id or 1),
                "degree": 0,
            }
        )

    edges: list[dict] = []
    nodes_by_id = {item["id"]: item for item in nodes}
    seen: set[tuple[str, str, str]] = set()
    ghost_titles: set[tuple[int, str]] = set()

    def add_edge(source: str, target: str, kind: str) -> None:
        if source == target:
            return
        key = (source, target, kind)
        if key in seen:
            return
        seen.add(key)
        edges.append({"source": source, "target": target, "kind": kind})

    def ensure_ghost(title: str, kb_id: int) -> str:
        key = (int(kb_id or 1), normalize_title(title))
        gid = node_id("ghost", title=key[1], kb_id=key[0])
        if key not in ghost_titles:
            ghost_titles.add(key)
            nodes.append(
                {
                    "id": gid,
                    "type": "ghost",
                    "source_id": None,
                    "title": key[1],
                    "category": "",
                    "tags": [],
                    "kb_id": key[0],
                    "degree": 0,
                }
            )
        return gid

    wiki_links = (await db.execute(select(
        WikiLink.source_type, WikiLink.source_id, WikiLink.kb_id, WikiLink.target_title,
        WikiLink.target_note_id, WikiLink.target_document_id, WikiLink.resolved,
    ).order_by(WikiLink.id))).all()
    for edge in wiki_links:
        source_type = (edge.source_type or "").strip()
        if source_type == "note" and int(edge.source_id) not in note_ids:
            # `回归修复`（S1）：边被静默丢弃必须可计数——源笔记已不可见
            # （典型：实体被硬删/边清理失败 ⇒ 残留边），这条边画不出来且原本无任何出口。
            derived_counters.bump("graph_view_edges")
            continue
        if source_type == "document" and int(edge.source_id) not in doc_ids:
            derived_counters.bump("graph_view_edges")  # 同上（S1 · 源文档缺失）
            continue
        source = node_id(source_type, edge.source_id)
        if source not in nodes_by_id or nodes_by_id[source]["kb_id"] != int(edge.kb_id or 1):
            derived_counters.bump("graph_view_edges")
            continue

        target: str | None = None
        other = other_endpoint(edge, (source_type, int(edge.source_id)))
        if edge.resolved and other is None:
            continue
        if other is not None and node_id(*other) in nodes_by_id:
            target = node_id(*other)
            if nodes_by_id[target]["kb_id"] != nodes_by_id[source]["kb_id"]:
                derived_counters.bump("graph_view_edges")
                continue
        elif int(edge.target_note_id or 0) in trashed_note_ids or int(edge.target_document_id or 0) in trashed_doc_ids:
            continue  # 目标在回收站：边不画，也不转幽灵
        else:
            title = normalize_title(edge.target_title)
            if not title:
                derived_counters.bump("graph_view_edges")  # 同上（S1 · 目标无标题 ⇒ ghost 也建不出）
                continue
            if (int(edge.kb_id or 1), title) in trashed_titles:
                continue  # 生命周期维护已反解析入链时，仍不让软删标题转成 ghost。
            target = ensure_ghost(title, int(edge.kb_id or 1))
        add_edge(source, target, "wiki")

    for item in notes:
        if item.source_document_id is not None and int(item.source_document_id) in doc_ids:
            source = node_id("note", item.id)
            target = node_id("document", item.source_document_id)
            if nodes_by_id[source]["kb_id"] == nodes_by_id[target]["kb_id"]:
                add_edge(source, target, "source")

    # 导入资料自动连线（专项 · 2026-10-08）：相似边 = 两篇文档的**内容接近**边。
    # 关态下连本表都不 SELECT（`GET /api/graph` 输出与改动前逐字一致）。
    # 源/目标缺失（硬删残留）或两端不同库照 wiki 边丢弃并计数 ⇒ 与既有边同一套可见性口径。
    if graphlink.enabled():
        # P2-B：边表的 `space_id` / `dim` 是**写入当时那一份构建**的身份（表里不做版本管理）。
        # 文档重建、换 embedding 模型或改维度之后，旧边与当前构建不兼容 —— 照画等于把两个
        # 空间的坐标当成一个空间用（前端只看 source/target/kind，没法自己判断）。payload
        # 形状不变：不兼容的边直接不进，只计数，不加字段（边仍只有 source/target/kind）。
        current = {
            int(row[0]): (str(row[1] or ""), _build_dim(row[2]))
            for row in (await db.execute(
                select(Document.id, IndexBuild.space_id, IndexBuild.fingerprint)
                .join(IndexBuild, and_(
                    IndexBuild.id == Document.active_build_id,
                    IndexBuild.document_id == Document.id,
                ))
                .where(Document.deleted_at.is_(None))
            )).all()
        }
        similar = (await db.execute(
            select(
                DocumentSimilarityEdge.source_document_id,
                DocumentSimilarityEdge.target_document_id,
                DocumentSimilarityEdge.space_id,
                DocumentSimilarityEdge.dim,
            ).order_by(
                DocumentSimilarityEdge.source_document_id,
                DocumentSimilarityEdge.target_document_id,
            )
        )).all()
        for row in similar:
            source = node_id("document", row[0])
            target = node_id("document", row[1])
            if source not in nodes_by_id or target not in nodes_by_id:
                derived_counters.bump("graph_view_edges")
                continue
            if nodes_by_id[source]["kb_id"] != nodes_by_id[target]["kb_id"]:
                derived_counters.bump("graph_view_edges")
                continue
            left, right = current.get(int(row[0])), current.get(int(row[1]))
            if left is None or left != right:
                derived_counters.bump("graph_view_edges")  # 两端必须同处一个当前空间
                continue
            if left[0] != str(row[2] or ""):
                derived_counters.bump("graph_view_edges")  # 边的坐标空间 ≠ 当前空间
                continue
            if left[1] and left[1] != int(row[3] or 0):
                derived_counters.bump("graph_view_edges")  # 维度不等 ⇒ 不是一个空间
                continue
            add_edge(source, target, "similar")

    degree: dict[str, int] = {item["id"]: 0 for item in nodes}
    for edge in edges:
        if edge["source"] in degree:
            degree[edge["source"]] += 1
        if edge["target"] in degree:
            degree[edge["target"]] += 1
    for item in nodes:
        item["degree"] = int(degree.get(item["id"], 0))

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "notes": len(notes),
            "documents": len(docs),
            "ghosts": len(ghost_titles),
            "edges": len(edges),
            "isolated": sum(1 for item in nodes if item["degree"] == 0),
        },
    }


__all__ = [
    "build_graph", "node_id", "tags_list", "load_wiki_adjacency", "wiki_adjacency",
    "wiki_neighbors",
]
