"""引用与归因（阶段 3 · 条目 3.2）——**纯函数，无 IO**。

三件事：

1. **Ref 组装**：本轮材料快照 → 02 §3 的**十一段**引用结构；
2. **[n] 判定**：按正文里模型写的编号定出最终引用集合与 ``citation_mode``；
3. **归因来源**：给出编号对应的材料行，供 ``message_attributions`` 落库，保留候选模式。

## 不变量（改本模块前先读这四条）

1. **编号同源**：``Ref.ordinal`` 取材料快照编号；未编号材料采用与渲染一致的位置兜底。
   解析稀疏集合也使用冻结编号，过滤或重排不能把 ``[4]`` 变成 ``[2]``（I3）；
2. **身份同源**：归因同时核对编号与已有 chunk 身份；调用方不能把不同 ``retrieval_id``
   的引用强行改绑为本轮。旧引用缺少可选身份时仍保留按编号匹配的兼容行为；
3. **兜底不漏报**：一个有效编号都没有时（模型忘写 ``[n]``、或只写了越界编号）**原样带出全部来源**
   并标 ``citation_mode="fallback"``，仅表示本轮候选资料，不证明正文实际使用或受其支持；
4. **前后端一致**：正则 ``\\[(\\d+)\\]``、冻结编号匹配和 mode 判定与
   ``frontend/src/citation.ts`` 同步；``cited`` 也只证明标注对应，不能代替事实与支持度检查。

## 与别的模块的分工

- 分路（``kind`` / ``answer_source``）归 :mod:`app.generation.source_map`——本模块只处理 **refs**；
- 「refs 是否恒空」复用 :func:`source_map.refs_empty_for`（黑名单唯一控制点），不在这里再抄一份；
- 写库归 ``app.session.repository``，HTTP 组装归 ``app.api.chat_routes``。
"""

from __future__ import annotations

import re
from typing import Iterator, Literal
from unicodedata import decimal

from app.generation import source_map
from app.retrieval.types import Material

# 引用解析正则（与前端 `citation.ts::CITE_RE` 同口径）：半角方括号 + 至少一位数字。
# `[10]` 解析为 10（不会被拆成 `[1]` + 残留 `0`）；`[01]` 归一为 1；全角 `［1］` 不是编号。
CITE_RE = re.compile(r"\[(\d+)\]")

# 该消息内引用条数上限：与检索侧 `retrieval.types.FUSE_LIMIT` **对齐**
# （快照本来就 ≤ 该值，此处再兜一层）。不 import 检索层常量是为了不引入同层横向依赖，
# 一致性由 `test_max_refs_matches_fuse_limit` 这条用例钉住。
MAX_REFS = 12

# 片段长度（老基线 `RetrievedChunk.to_ref` 口径：160 字 + 省略号）
SNIPPET_LIMIT = 160

CitationMode = Literal["cited", "fallback", "none"]


def snapshot_ordinal(value: object, position: int) -> int:
    """采用已有正整数编号；旧引用或未编号材料按原快照位置兜底。"""
    return value if type(value) is int and value >= 1 else position


def parse_citation_ordinal(value: str) -> int | None:
    """解析编号；超出 Python 整数转换上限的模型文本按无效编号处理。"""
    try:
        for index, digit in enumerate(value):
            if decimal(digit):
                return int(value[index:])
        return 0
    except ValueError:
        return None


def snippet_of(text: str, limit: int = SNIPPET_LIMIT) -> str:
    """引用卡片的片段：压平换行、去首尾空白，超长截断加省略号。"""
    snippet = (text or "").strip().replace("\n", " ")
    return f"{snippet[:limit]}…" if len(snippet) > limit else snippet


def _normalized_scores(materials: list[Material]) -> dict[int, float]:
    """快照内按最高分归一到 0~1（RRF 分只有 ``1/60`` 量级，直接展示很怪）。"""
    best = max([float(item.score or 0.0) for item in materials or []] or [0.0])
    if best <= 0:
        return {int(item.chunk_id): 0.0 for item in materials or []}
    return {int(item.chunk_id): float(item.score or 0.0) / best for item in materials or []}


def refs_from_materials(
    materials: list[Material],
    retrieval_id: str | None = None,
    kb_names: dict[int, str] | None = None,
) -> list[dict]:
    """材料快照 → 02 §3 的 Ref 列表（顺序 = 快照顺序，编号 = 快照编号）。

    十一个字段全给：``type`` / ``title`` / ``snippet`` / ``score`` / ``kb_name`` / ``source_id`` /
    ``url`` / ``chunk_id`` / ``retrieval_id`` / ``ordinal`` / ``citation_mode``。

    - ``type`` / ``url`` / ``chunk_id`` 由材料自身分流（阶段 4 · 4.1 起）：库内材料是
      ``document`` / ``None`` / ``kb:...``（**逐字与新增前一致**），网页材料是 ``web`` /
      原地址 / ``web:<sha1>``——见 :class:`app.retrieval.types.Material` 的 ``url`` 字段；
    - ``source_id`` = **库内来源 id**（供前端回链与「相关笔记」）：文档材料是文档 id、笔记材料是
      **笔记 id**（阶段 4 · 4.5.2 起，走 :attr:`app.retrieval.types.Material.source_ref`）；
      **chunk 身份一律走 ``chunk_id``**（02 §3 明文：禁止用 ``source_id`` 承载 chunk 身份）。
      网页材料不是库内来源 ⇒ ``source_id = 0``（02 允许该字段可空，前端对 0 不做回链）。
    - ``kb_name`` 由调用方给映射（默认 ``None``——02 允许全字段可空）。
    """
    scores = _normalized_scores(materials)
    names = kb_names or {}
    out: list[dict] = []
    for position, item in enumerate((materials or [])[:MAX_REFS], start=1):
        # 来源 id 按材料身份分流（笔记 / 文档 / 网页），见 docstring：
        # 冻结前端 `CitationCard.tsx:57` 把它当 `/api/wiki/related?id=` 传回来，
        # 而 wikilink 的边表里笔记与文档是**两个 id 空间**（笔记用 note_id）。
        source_ref = item.source_ref
        out.append(
            {
                "type": item.source_type,
                "title": item.filename,
                "snippet": snippet_of(item.content),
                "score": round(scores.get(int(item.chunk_id), 0.0), 4),
                "kb_name": names.get(int(item.kb_id)),
                "source_id": int(source_ref[1]) if source_ref else 0,
                "url": item.url,
                "chunk_id": item.chunk_key,
                "retrieval_id": retrieval_id,
                "ordinal": snapshot_ordinal(item.ordinal, position),
                "citation_mode": None,
            }
        )
    return out


def _tagged(ref: dict, mode: CitationMode) -> dict:
    """复制一份并改写 ``citation_mode``（不改动入参）。"""
    return {**ref, "citation_mode": mode}


def resolve_citations(answer: str, refs: list[dict]) -> tuple[list[dict], CitationMode]:
    """按三条规则解析引用，返回 ``(refs, mode)``。

    - ``mode="none"``：``refs`` 为空 → 返回空列表（澄清 / 拒答 / 未命中轮）；
    - ``mode="cited"``：只保留正文里 ``[n]`` **真正点到**的来源（按原列表顺序）；
    - ``mode="fallback"``：一个有效编号都没有 → **原样带出全部**来源。

    判定使用已有冻结编号；旧引用的编号缺失或无效时才按列表位置兜底。
    ``fallback`` 和 ``cited`` 都保留各来源原有身份，均不证明事实支持程度。
    """
    if not refs:
        return [], "none"
    cited = {parse_citation_ordinal(number) for number in CITE_RE.findall(answer or "")}
    selected = [
        _tagged(ref, "cited")
        for position, ref in enumerate(refs, start=1)
        if snapshot_ordinal(ref.get("ordinal"), position) in cited
    ]
    if not selected:
        return [_tagged(ref, "fallback") for ref in refs], "fallback"
    return selected, "cited"


def cited_refs(answer: str, refs: list[dict]) -> list[dict]:
    """答案里真正点到的来源（兜底见 :func:`resolve_citations`）。"""
    items, _mode = resolve_citations(answer, refs)
    return items


def refs_for_kind(kind: str | None, answer: str, all_refs: list[dict]) -> list[dict]:
    """按正文与分路决定落库引用：空正文或恒空的 kind 不带引用，其余走引用判定。

    「哪些 kind 恒空」的唯一真相在 :func:`source_map.refs_empty_for`（``direct`` / ``refuse`` /
    ``clarify``）；这里不另立一份表，避免两处口径漂移。
    """
    if not (answer or "").strip() or source_map.refs_empty_for(kind):
        return []
    return cited_refs(answer, all_refs)


def _matched_materials(
    materials: list[Material], refs: list[dict]
) -> Iterator[tuple[dict, Material]]:
    """编号和已有 chunk 身份都须属于该快照；旧引用缺少 chunk 身份时仅核编号。"""
    by_ordinal = {
        snapshot_ordinal(item.ordinal, position): item
        for position, item in enumerate(materials or [], start=1)
    }
    for ref in refs or []:
        ordinal = ref.get("ordinal")
        item = by_ordinal.get(ordinal) if type(ordinal) is int else None
        if item is None or (ref.get("chunk_id") and ref["chunk_id"] != item.chunk_key):
            continue
        yield ref, item


def cited_materials(materials: list[Material], refs: list[dict]) -> list[Material]:
    """编号对应的材料行，顺序与 refs 一致；fallback 行仍仅表示候选资料。"""
    return [item for _ref, item in _matched_materials(materials, refs)]


def build_refs(
    kind: str | None,
    answer: str,
    materials: list[Material],
    retrieval_id: str | None = None,
) -> tuple[list[dict], list[Material]]:
    """一步到位：**快照** → ``(最终 refs, 被引用材料)``。

    这是给**阶段 4 Agent 轮**预留的同源入口：Agent 自己的检索快照（``retrieve`` 那一路的
    ``materials`` + ``retrieval_id``）直接喂进来，就能得到与普通轮**逐字段同源**的引用与归因，
    不必复制任何判定逻辑（I3：Agent 轮与普通轮同源去重排序编号）。
    普通轮的 ``chat_routes.finalize_citations`` 用的是**已经冻结在 TurnPlan 上的** ``all_refs``
    （同一份 ``refs_from_materials`` 产物），两条路径因此等价。
    """
    all_refs = refs_from_materials(materials, retrieval_id)
    refs = refs_for_kind(kind, answer, all_refs)
    return refs, cited_materials(materials, refs)


def attribution_rows(
    message_id: int,
    materials: list[Material],
    refs: list[dict],
    retrieval_id: str | None = None,
) -> list[dict]:
    """``message_attributions`` 的行（dict 形态，由 ``session.repository`` 落库）。

    ``ordinal`` = 该消息内的引用编号（从 1 起，与资料区的 ``[n]`` 同源）；
    ``chunk_id`` = ``chunks.id``（FK，就是 Qdrant 的 point id）；
    ``meta`` 存可回查的溯源信息（快照 id / 解析模式 / 冻结 chunk 身份 / kb / 页码）。

    **网页材料不写归因行**（阶段 4 · 条目 4.1）：``message_attributions.chunk_id`` 是指向
    ``chunks`` 表的外键，网页材料没有对应分块行——硬写会撞 FK（在 MySQL 严格模式下报错、
    在 SQLite 外键开启时同样报错），而调用方的归因写入是「失败整体回滚」的，一条网页行会
    连带丢掉**全部**文档归因。老基线同样不给网页写归因（它只写 chunk 行）。判据用
    ``Material.is_web`` 与 ``document_id <= 0`` 双条件：前者是语义、后者是兜底。
    """
    rows: list[dict] = []
    for ref, material in _matched_materials(materials, refs):
        if retrieval_id and ref.get("retrieval_id") and retrieval_id != ref["retrieval_id"]:
            continue
        if material.is_web or int(material.document_id) <= 0:
            continue  # 没有真实分块行 ⇒ 没有 FK 可指，跳过（见 docstring）
        rows.append(
            {
                "message_id": int(message_id),
                "chunk_id": int(material.chunk_id),
                "ordinal": ref["ordinal"],
                "title": ref.get("title") or material.filename,
                "snippet": ref.get("snippet") or "",
                "score": float(ref.get("score") or 0.0),
                "meta": {
                    "retrieval_id": retrieval_id or ref.get("retrieval_id"),
                    "citation_mode": ref.get("citation_mode"),
                    "semantic_support": None,
                    "support_review": "unmeasured",
                    "chunk_key": material.chunk_key,
                    "kb_id": int(material.kb_id),
                    "page": material.page,
                },
            }
        )
    return rows


__all__ = [
    "CITE_RE",
    "MAX_REFS",
    "SNIPPET_LIMIT",
    "CitationMode",
    "attribution_rows",
    "build_refs",
    "cited_materials",
    "cited_refs",
    "parse_citation_ordinal",
    "refs_for_kind",
    "refs_from_materials",
    "resolve_citations",
    "snapshot_ordinal",
    "snippet_of",
]
