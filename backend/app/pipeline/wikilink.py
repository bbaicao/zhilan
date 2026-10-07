"""双向链接（任务清单 13）：正文 ``[[标题]]`` 的解析、落库、维护与检索扩展。

口径约定：
- 扫描输入一律是「frontmatter 剥离后」的正文——笔记用 ``note.content``，文档用
  ``_prepare`` 产物 ``prepared.text``（与分块同源），不回头扫 ``raw_content``；
- 解析目标在同 ``kb_id`` 内按归一化标题唯一匹配，笔记与文档同名也保留歧义；
- 两个 target 都为空的边是「未解析」（``resolved=False``），之后唯一目标出现时反向补上；
- 链接边是派生数据：维护失败只记日志，绝不反过来让笔记保存 / 文档删除失败。

先不做（任务清单 13「先不做」）：块级引用 ``[[note#^block]]``（只取 ``#`` 前段当标题）、
嵌入 ``![[note]]``（跳过不建链）。

v2 移植说明（阶段 2 · 2.2.5；**阶段 4 · 4.5 收尾修订**）：
- ``extract_links`` 可独立单测；U1.8 起统一标题身份并跳过 Markdown 代码；
- DB 维护函数（建边 / 反向解析 / 取名 / 双向查询）整文件照抄，依赖 ``app.models`` 的 import
  放在 try 块里——models 未落地时本模块仍可被 import，纯提取规则不受影响。
- ⚠️ **已删除**：老的「检索扩展（P2）」半段（``expand_chunks`` / ``_link_fragment``）——
  它**零调用点**，且依赖 v2 从未落地的 ``app.pipeline.schemas`` / ``store``
  （老基线的 ``RetrievedChunk.chunk_id`` 是**字符串**合成键，与 v2 ``Material.chunk_id`` 的整型
  ``chunks.id`` 不同族）。4.5.2 已用 ``app/retrieval/link.py`` 以 ``Material`` 原生重实现一跳扩展，
  旧实现于 4.5 收尾清理时删除（``回归修复`` 收口的一部分；先例：4.4 的 `7f9ad45`）。
"""

from __future__ import annotations

import logging
import re

from app.core import derived_counters
from app.pipeline.cleaning import protected_line_indices

try:
    from sqlalchemy import and_, delete, or_, select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import Chunk, Document, Note, WikiLink, active_chunks
except ImportError:  # 2.1 models 尚未落地时本模块仍可 import（纯提取规则不依赖它）
    pass

log = logging.getLogger(__name__)

# ``[[标题]]``：``[[`` 前不是 ``!``（嵌入跳过），标题里不允许再出现方括号；
# 标题不跨行（Obsidian 语义，与前端渲染口径一致，避免造出永远无法解析的边）
LINK_RE = re.compile(r"(?<!\!)\[\[([^\[\]\n]+)\]\]")
TITLE_LIMIT = 255
_TITLE_TRIM_RE = re.compile(r"^[\s\ufeff]+|[\s\ufeff]+$")
# 行内代码允许跨普通换行，开闭反引号必须等长；奇数个转义反斜杠不打开代码。
_INLINE_CODE_RE = re.compile(
    r"(?<![\\`])(?:\\\\)*(`+)(?!`)(?:(?!\n[ \t]*\n)[\s\S])*?(?<!`)\1(?!`)"
)

# 链接片段并入上下文时的内容截断与降权系数（主命中分 × 0.5）
LINK_SNIPPET_CHARS = 800
LINK_SCORE_FACTOR = 0.5
# 主命中种子数：检索结果的前 N 个沿链扩展
LINK_SEEDS = 2


def other_endpoint(edge: "WikiLink", source: tuple[str, int]) -> tuple[str, int] | None:
    """已解析边的另一端；两端各自判断类型，不把入链目标当邻居。"""
    if not edge.resolved or edge.source_type not in ("note", "document"):
        return None
    targets = [
        (kind, int(sid))
        for kind, sid in (("note", edge.target_note_id), ("document", edge.target_document_id))
        if sid is not None
    ]
    if len(targets) != 1:
        return None
    start = (edge.source_type, int(edge.source_id))
    target = targets[0]
    other = target if start == source else start if target == source else None
    return other if other != source else None


async def incident_edges(
    session: "AsyncSession", source_type: str, source_id: int, kb_id: int | None
) -> list["WikiLink"]:
    """同库已解析入边＋出边；resolved 与库约束覆盖整个 OR。"""
    if source_type not in ("note", "document"):
        return []
    column = WikiLink.target_note_id if source_type == "note" else WikiLink.target_document_id
    stmt = select(WikiLink).where(
        WikiLink.resolved.is_(True),
        or_(
            and_(WikiLink.source_type == source_type, WikiLink.source_id == int(source_id)),
            column == int(source_id),
        ),
    )
    if kb_id is not None:
        stmt = stmt.where(WikiLink.kb_id == int(kb_id))
    return list((await session.execute(stmt.order_by(WikiLink.id))).scalars().all())


def normalize_title(title: str | None) -> str:
    """建边、解析和 ghost 同源：丢锚、去首尾空白、限 255 个 Unicode 代码点。"""
    key = _TITLE_TRIM_RE.sub("", (title or "").split("#", 1)[0])
    return _TITLE_TRIM_RE.sub("", key[:TITLE_LIMIT])  # 截断落在空白处时，再归一也不变身份。


def extract_links(text: str) -> list[str]:
    """只扫描 Markdown 正文，先归一标题再去重；代码、嵌入不建业务边。"""
    # 引用 / 列表容器里的围栏仍是代码，先去容器前缀；普通引用 / 表格仍参与建链。
    lines = [
        re.sub(
            r"^ {0,3}(?:[-+*]|\d{1,9}[.)])[ \t]+", "",
            re.sub(r"^ {0,3}(?:> ?)+", "", line),
        )
        for line in (text or "").splitlines()
    ]
    protected = protected_line_indices(lines, only_code=True)
    body = "\n".join("" if index in protected else line for index, line in enumerate(lines))
    body = _INLINE_CODE_RE.sub(lambda match: " " * len(match[0]), body)
    titles: list[str] = []
    seen: set[str] = set()
    for match in LINK_RE.finditer(body):
        title = normalize_title(match[1])
        if not title or title in seen:
            continue
        seen.add(title)
        titles.append(title)
    return titles


def _stem(filename: str) -> str:
    """文档参与标题匹配的名字：去掉最后一个扩展名。"""
    return (filename or "").rsplit(".", 1)[0]


async def _resolve_target(session: "AsyncSession", kb_id: int, title: str) -> tuple[int | None, int | None]:
    """单个标题与批量建边用同一唯一候选规则；歧义 / 回收站不绑定。"""
    key = normalize_title(title)
    if not key:
        return None, None
    notes, docs = await _kb_targets(session, int(kb_id or 1))
    _, note_id, doc_id = _map_targets([key], notes, docs)[0]
    return note_id, doc_id


async def _kb_targets(
    session: "AsyncSession", kb_id: int
) -> tuple[dict[str, int | None], dict[str, int | None]]:
    """同库活跃标题映射；None 表示该类型内多个候选，不取最小 id。

    回收站（软删）里的笔记 / 文档不参与建边（条目 25）。
    """
    notes: dict[str, int | None] = {}
    for note_id, title in (
        await session.execute(
            select(Note.id, Note.title).where(Note.kb_id == kb_id, Note.deleted_at.is_(None))
        )
    ).all():
        key = normalize_title(title)
        if key:
            notes[key] = None if key in notes else int(note_id)
    docs: dict[str, int | None] = {}
    for doc_id, filename in (
        await session.execute(
            select(Document.id, Document.filename).where(Document.kb_id == kb_id, Document.deleted_at.is_(None))
        )
    ).all():
        key = normalize_title(_stem(filename))
        if key:
            docs[key] = None if key in docs else int(doc_id)
    return notes, docs


def _map_targets(
    titles: list[str], notes: dict[str, int | None], docs: dict[str, int | None]
) -> list[tuple[str, int | None, int | None]]:
    """跨两个类型也必须恰有一个候选；同名笔记 / 文档不按优先级碰巧绑定。"""
    return [
        (title, None, None) if title in notes and title in docs else
        (title, notes.get(title), docs.get(title))
        for title in titles
    ]


async def rebuild_source_links(
    session: "AsyncSession", source_type: str, source_id: int, kb_id: int, text: str
) -> int:
    """重建一个来源的出链边：先删旧边，再按当前正文解析落库。返回边数。

    与调用方共用事务，不自行 commit——文档入库路径由 ingest_document 提交，
    笔记路径由维护入口提交。
    """
    titles = extract_links(text)
    await session.execute(
        delete(WikiLink).where(WikiLink.source_type == source_type, WikiLink.source_id == int(source_id))
    )
    if titles:
        notes, docs = await _kb_targets(session, int(kb_id or 1))
        for title, note_id, doc_id in _map_targets(titles, notes, docs):
            session.add(
                WikiLink(
                    source_type=source_type,
                    source_id=int(source_id),
                    kb_id=int(kb_id or 1),
                    target_title=title,
                    target_note_id=note_id,
                    target_document_id=doc_id,
                    resolved=note_id is not None or doc_id is not None,
                )
            )
    return len(titles)


async def refresh_resolved_for_title(session: "AsyncSession", kb_id: int, title: str) -> int:
    """按当前同库事实重判标题的全部边；创建、改名、删除与恢复共用。"""
    key = normalize_title(title)
    if not key:
        return 0
    rows = list(
        (
            await session.execute(
                select(WikiLink).where(WikiLink.kb_id == int(kb_id or 1), WikiLink.target_title == key)
            )
        ).scalars().all()
    )
    note_id, doc_id = await _resolve_target(session, int(kb_id or 1), key)
    resolved = note_id is not None or doc_id is not None
    updated = 0
    for row in rows:
        if (row.target_note_id, row.target_document_id, row.resolved) == (note_id, doc_id, resolved):
            continue
        row.target_note_id = note_id
        row.target_document_id = doc_id
        row.resolved = resolved
        updated += 1
    return updated


async def maintain_source_links(
    session: "AsyncSession",
    source_type: str,
    source_id: int,
    *,
    text: str | None = None,
    old_title: str | None = None,
    old_kb_id: int | None = None,
) -> bool:
    """业务提交后的统一维护入口；成功提交才返回 True，失败可重试且不回滚业务保存。

    文档入库 / 重清洗传规范正文；软删保留出链标题，恢复重新解析这些标题，
    避免从重叠的分块拼出不存在的正文或代码围栏。硬删才清掉出链。
    """
    if source_type not in ("note", "document"):
        raise ValueError(f"unknown link source type: {source_type}")
    source_id = int(source_id)
    source = None
    try:
        source = await session.get(Note if source_type == "note" else Document, source_id)
        column = WikiLink.target_note_id if source_type == "note" else WikiLink.target_document_id
        rows = list(
            (
                await session.execute(
                    select(WikiLink).where(
                        or_(
                            and_(WikiLink.source_type == source_type, WikiLink.source_id == source_id),
                            column == source_id,
                        )
                    )
                )
            ).scalars().all()
        )
        affected = {(int(row.kb_id or 1), normalize_title(row.target_title)) for row in rows}
        if source is None:
            if old_kb_id is not None and old_title is not None:
                affected.add((int(old_kb_id or 1), normalize_title(old_title)))
            await session.execute(
                delete(WikiLink).where(WikiLink.source_type == source_type, WikiLink.source_id == source_id)
            )
        else:
            kb_id = int(source.kb_id or 1)
            title = source.title if source_type == "note" else _stem(source.filename)
            affected.add((kb_id, normalize_title(title)))
            if old_title is not None:
                affected.add((
                    int(old_kb_id or 1) if old_kb_id is not None else kb_id,
                    normalize_title(old_title),
                ))
            if source.deleted_at is None and (source_type == "note" or text is not None):
                body = (source.content or "") if source_type == "note" else (text or "")
                await rebuild_source_links(session, source_type, source_id, kb_id, body)
        for kb_id, title in sorted(affected):
            await refresh_resolved_for_title(session, kb_id, title)
        await session.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        try:
            await session.rollback()
            # rollback 会过期同 session 的业务行；恢复已提交内容供调用方返回 / 序列化。
            if source is not None:
                await session.refresh(source)
        except Exception as rollback_exc:  # noqa: BLE001
            log.warning("恢复 %s:%s 的已提交状态失败：%s", source_type, source_id, rollback_exc)
        log.warning("%s:%s 的链接维护失败（业务已提交，可重试）：%s", source_type, source_id, exc)
        derived_counters.bump("note_links" if source_type == "note" else "doc_links")
        return False


async def remove_links_for_deleted(
    session: "AsyncSession", source_type: str, source_id: int, *,
    old_kb_id: int | None = None, old_title: str | None = None,
) -> bool:
    """彻底删除后清边；删前库 / 标题快照让歧义边能按原范围重新解析。"""
    return await maintain_source_links(
        session, source_type, source_id, old_kb_id=old_kb_id, old_title=old_title,
    )


async def maintain_note_links(
    session: "AsyncSession", note: "Note", old_title: str | None = None
) -> bool:
    """兼容笔记保存调用点：创建、更新、改名与软删共用来源维护。"""
    return await maintain_source_links(session, "note", int(note.id), old_title=old_title)


# --------------------------------------------------------------------------
# 回链 / 相关（任务清单 14）
# --------------------------------------------------------------------------

BACKLINK_LIMIT = 20
BACKLINK_SNIPPET_CHARS = 80


def _brief(text: str) -> str:
    snippet = (text or "").strip().replace("\n", " ")
    return snippet[:BACKLINK_SNIPPET_CHARS] + ("…" if len(snippet) > BACKLINK_SNIPPET_CHARS else "")


async def _load_sources(
    session: "AsyncSession", pairs: list[tuple[str, int]], *, kb_id: int
) -> dict[tuple[str, int], tuple[str, str, str]]:
    """批量取来源的标题与摘录：(title, snippet, category)。回收站里的来源不取（条目 25）。"""
    note_ids = sorted({sid for stype, sid in pairs if stype == "note"})
    doc_ids = sorted({sid for stype, sid in pairs if stype == "document"})
    out: dict[tuple[str, int], tuple[str, str, str]] = {}
    if note_ids:
        rows = (
            await session.execute(
                select(Note.id, Note.title, Note.content, Note.category)
                .where(Note.id.in_(note_ids), Note.deleted_at.is_(None), Note.kb_id == kb_id)
            )
        ).all()
        for note_id, title, content, category in rows:
            out[("note", int(note_id))] = (title or f"笔记 {note_id}", _brief(content), category or "")
    if doc_ids:
        docs = (
            await session.execute(
                select(Document.id, Document.filename, Document.category, Chunk.content)
                .join(
                    Chunk,
                    and_(Chunk.document_id == Document.id, Chunk.ordinal == 0, active_chunks()),
                    isouter=True,
                )
                .where(Document.id.in_(doc_ids), Document.deleted_at.is_(None), Document.kb_id == kb_id)
            )
        ).all()
        for doc_id, filename, category, content in docs:
            out[("document", int(doc_id))] = (filename or f"文档 {doc_id}", _brief(content or ""), category or "")
    return out


async def _source_pairs_from_edges(
    session: "AsyncSession",
    edges: list["WikiLink"],
    *,
    outgoing: bool,
    self_pair: tuple[str, int] | None = None,
) -> list[tuple[str, int, str]]:
    """把边映射成 (source_type, source_id, direction)，去掉自环与未解析。"""
    pairs: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int]] = set()
    for edge in edges:
        if not edge.resolved:
            continue
        if self_pair is not None:
            pair = other_endpoint(edge, self_pair)
            if pair is None:
                continue
            direction = "outgoing" if outgoing else "incoming"
        elif outgoing:
            if edge.target_note_id is not None:
                pair = ("note", int(edge.target_note_id))
            elif edge.target_document_id is not None:
                pair = ("document", int(edge.target_document_id))
            else:
                continue
            direction = "outgoing"
        else:
            pair = (edge.source_type, int(edge.source_id))
            direction = "incoming"
        if self_pair is not None and pair == self_pair:
            continue
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append((pair[0], pair[1], direction))
    return pairs


async def _relationship_source(session: "AsyncSession", kind: str, sid: int):
    if kind not in ("note", "document"):
        return None
    source = await session.get(Note if kind == "note" else Document, int(sid))
    return source if source is not None and source.deleted_at is None else None


async def list_backlinks(
    session: "AsyncSession", target_type: str, target_id: int, limit: int = BACKLINK_LIMIT
) -> list[dict]:
    """谁链到了我：``target_type``/``target_id`` 的入边，按来源标题 + 摘录返回。

    只看已解析边（``resolved=True``）；自环、已消失的来源与回收站里的来源不进列表。
    """
    source = await _relationship_source(session, target_type, target_id)
    if source is None:
        return []
    kb_id = int(source.kb_id or 1)
    edges = [
        edge for edge in await incident_edges(session, target_type, target_id, kb_id)
        if (edge.source_type, int(edge.source_id)) != (target_type, int(target_id))
    ]
    pairs = await _source_pairs_from_edges(
        session, edges, outgoing=False, self_pair=(target_type, int(target_id))
    )
    sources = await _load_sources(session, [(st, sid) for st, sid, _ in pairs], kb_id=kb_id)
    items: list[dict] = []
    for stype, sid, direction in pairs:
        if (stype, sid) not in sources:
            continue
        title, snippet, category = sources[(stype, sid)]
        items.append(
            {
                "type": stype,
                "id": sid,
                "title": title,
                "snippet": snippet,
                "category": category,
                "direction": direction,
            }
        )
    items.sort(key=lambda item: item["title"])
    return items[: max(int(limit), 0)]


async def list_related(
    session: "AsyncSession", source_type: str, source_id: int, limit: int = BACKLINK_LIMIT
) -> list[dict]:
    """与我通过链接边相连的一跳：出链目标 + 入链来源（引用卡「相关笔记」用）。

    只走已落库的链接边，**不做**向量检索；自环去掉；同一目标只保留一条；
    回收站（软删）一侧的条目不进列表（条目 25）。
    """
    self_pair = (source_type, int(source_id))
    source = await _relationship_source(session, source_type, source_id)
    if source is None:
        return []
    kb_id = int(source.kb_id or 1)
    edges = await incident_edges(session, source_type, source_id, kb_id)
    outgoing_edges = [edge for edge in edges if (edge.source_type, int(edge.source_id)) == self_pair]
    incoming_edges = [edge for edge in edges if (edge.source_type, int(edge.source_id)) != self_pair]
    pairs = await _source_pairs_from_edges(session, outgoing_edges, outgoing=True, self_pair=self_pair)
    pairs += await _source_pairs_from_edges(session, incoming_edges, outgoing=False, self_pair=self_pair)
    # 去重：出链优先于入链（先放进 seen 的保留）
    seen: set[tuple[str, int]] = set()
    unique: list[tuple[str, int, str]] = []
    for stype, sid, direction in pairs:
        if (stype, sid) in seen:
            continue
        seen.add((stype, sid))
        unique.append((stype, sid, direction))
    sources = await _load_sources(session, [(st, sid) for st, sid, _ in unique], kb_id=kb_id)
    items: list[dict] = []
    for stype, sid, direction in unique:
        if (stype, sid) not in sources:
            continue
        title, snippet, category = sources[(stype, sid)]
        items.append(
            {
                "type": stype,
                "id": sid,
                "title": title,
                "snippet": snippet,
                "category": category,
                "direction": direction,
            }
        )
    items.sort(key=lambda item: (0 if item["direction"] == "outgoing" else 1, item["title"]))
    return items[: max(int(limit), 0)]


async def resolve_title(session: "AsyncSession", kb_id: int, title: str) -> tuple[str | None, int | None]:
    """标题 → 同库唯一 (type, id)；找不到或歧义返回 (None, None)。"""
    note_id, doc_id = await _resolve_target(session, kb_id, title)
    if note_id is not None:
        return "note", int(note_id)
    if doc_id is not None:
        return "document", int(doc_id)
    return None, None
