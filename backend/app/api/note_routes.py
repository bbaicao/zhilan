"""notes · wiki · graph · knowledge-bases 四域共 9 个端点（阶段 4 · 条目 4.5.2）。

契约与老基线对照（``02-API契约冻结清单.md`` §1.1 / §7.4 ↔ 老仓库
``<legacy-repo>\\backend\\app\\api\\__init__.py`` 的对应行）：

| 方法 | 路径 | 老基线条 | 本文件落点 |
|---|---|---|---|
| GET | ``/api/notes`` | 475 | :func:`list_notes` |
| POST | ``/api/notes`` | 483 | :func:`create_note` |
| PUT | ``/api/notes/{note_id}`` | 503 | :func:`update_note` |
| DELETE | ``/api/notes/{note_id}`` | 528 | :func:`delete_note` |
| GET | ``/api/wiki/backlinks`` | 568 | :func:`wiki_backlinks` |
| GET | ``/api/wiki/related`` | 583 | :func:`wiki_related` |
| GET | ``/api/graph`` | 651 | :func:`knowledge_graph_view` |
| GET | ``/api/knowledge-bases`` | 1129 | :func:`list_knowledge_bases` |
| POST | ``/api/knowledge-bases`` | 1149 | :func:`create_knowledge_base` |

字段表（``NoteOut`` / ``WikiLinkHit`` / ``WikiLinksOut`` / ``GraphNode`` / ``GraphEdge`` /
``GraphStats`` / ``GraphOut`` / ``KnowledgeBaseOut``）**逐字照抄** 02 §7.4 与老基线的类定义，
顺序、默认值、可空性都不改——冻结前端 ``frontend/src/api.ts`` 按它们渲染。

两个 v2 适配点（都登记在证据里，不是随手改的）：

1. **笔记不进 ``message_attributions``**：``Material.document_id`` 对笔记来源取 ``0``
   ⇒ ``generation.citation.attribution_rows`` 的既有两个跳过条件（``is_web`` /
   ``document_id <= 0``）自然覆盖笔记，不会往 ``chunks`` 的外键里塞假行。
   代价是 ``Ref.source_id`` 对笔记恒 ``0`` ⇒ 见下一条。
2. **``id=0`` 视为「没给 id」**：冻结前端 ``CitationCard.tsx:57`` 恒把
   ``source_id ?? null`` 与 ``title`` 一起传（``getRelated(type, item.source_id ?? null,
   item.title)``），而 ``title`` 参数的既定用途就是「没有 id 时按标题解析」
   （老基线 ``_wiki_target`` docstring 原文）。笔记来源的 ``source_id`` 是 ``0``
   ⇒ 把 ``id <= 0`` 当「没给」并退回标题解析，「相关笔记」在笔记来源上才真的能展开。
   给了**正数** id 但行不存在 / 在回收站，仍按 02 §4 回 404「来源不存在」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.kb_identity import kb_canonical_path, kb_path_key
from app.api.response_time import response_time
from app.db.session import get_db
from app.models import Document, KnowledgeBase, Note
from app.pipeline import knowledge_graph, wikilink
from app.session import note_service

router = APIRouter()


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class NoteIn(BaseModel):
    """``POST /api/notes`` 请求体（老基线 L63-70）。"""

    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    category: str = ""
    # 任务清单 29：AI 生成草稿保存时携带的溯源与元数据（可选）
    source_document_id: int | None = None
    tags: list[str] | None = None
    generation_meta: dict | None = None


class NoteUpdate(BaseModel):
    """``PUT /api/notes/{note_id}`` 请求体（老基线 L73-80）。``None`` = 该字段不动。"""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1)
    category: str | None = None
    # 任务清单 29：None 表示不动（与 title/content/category 的现有语义一致）；
    # 不收 source_document_id——覆盖保存时不动溯源字段
    tags: list[str] | None = None
    generation_meta: dict | None = None


class NoteOut(BaseModel):
    """``Note`` 响应体：九字段（02 §7.4 / 老基线 L83-95），**逐字一致**。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    content: str
    category: str
    created_at: datetime
    updated_at: datetime
    # 任务清单 29：AI 生成笔记的溯源与元数据
    source_document_id: int | None = None
    tags: list[str] | None = None
    generation_meta: dict | None = None

    @field_serializer("created_at", "updated_at")
    def serialize_time(self, value: datetime) -> datetime:
        return response_time(value)


class WikiLinkHit(BaseModel):
    """回链 / 相关列表的一条：指向或来自某个笔记 / 文档（02 §7.4 / 老基线 L541-550）。"""

    type: Literal["note", "document"]
    id: int
    title: str
    snippet: str = ""
    category: str = ""
    # incoming = 谁链到我；outgoing = 我链向谁（回链接口恒为 incoming）
    direction: Literal["incoming", "outgoing"] = "incoming"


class WikiLinksOut(BaseModel):
    items: list[WikiLinkHit] = Field(default_factory=list)


class GraphNode(BaseModel):
    """图谱节点：笔记 / 文档 / 未创建的幽灵标题（02 §7.4 / 老基线 L616-626）。"""

    id: str
    type: Literal["note", "document", "ghost"]
    source_id: int | None = None
    title: str
    category: str = ""
    tags: list[str] = Field(default_factory=list)
    kb_id: int = 1
    degree: int = 0


class GraphEdge(BaseModel):
    """图谱边：wiki = ``[[标题]]``；source = 笔记溯源文档（条目 29）；similar = 内容接近（专项，关态不产出）。"""

    source: str
    target: str
    kind: Literal["wiki", "source", "similar"] = "wiki"


class GraphStats(BaseModel):
    notes: int = 0
    documents: int = 0
    ghosts: int = 0
    edges: int = 0
    isolated: int = 0


class GraphOut(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    stats: GraphStats = Field(default_factory=GraphStats)


class KnowledgeBaseOut(BaseModel):
    """``KnowledgeBase`` 响应体：八字段（02 §7.4 / 老基线 L216-226），**逐字一致**。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: str
    path_or_url: str = ""
    collection: str = ""
    read_only: bool = False
    status: str = "ready"
    last_sync_at: datetime | None = None


class KnowledgeBaseIn(BaseModel):
    """``POST /api/knowledge-bases`` 请求体（老基线 L229-233）。

    ``回归修复``：``min_length=1`` 数的是**字符数**、挡不住纯空白 ⇒ ``"   "`` 通过校验，
    端点体落库前 ``strip()`` 才清 ⇒ 库里出现 ``name == ""`` 的知识库（``path_or_url`` 同理）。
    两个字段各加 ``field_validator``：``strip()`` 后为空即 ``ValueError`` ⇒ Pydantic
    ``RequestValidationError`` ⇒ **422**（``02-API契约冻结清单.md`` 全局登记的异常语义）。
    ``min_length`` / ``max_length`` 与端点体内的落库 ``strip()`` **一律保留**。
    """

    name: str = Field(min_length=1, max_length=255)
    kind: str
    path_or_url: str = Field(min_length=1, max_length=1024)
    collection: str = ""

    @field_validator("name", "path_or_url")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """纯空白（含全角空格等 Unicode 空白）不是合法值；两端空白仍由端点体 strip() 清。"""
        if not value.strip():
            raise ValueError("不能为纯空白")
        return value


# ---------------------------------------------------------------------------
# notes
# ---------------------------------------------------------------------------


@router.get("/notes", response_model=list[NoteOut])
async def list_notes(category: str | None = None, db: AsyncSession = Depends(get_db)):
    """笔记列表（``id`` 倒序，不含回收站）；``category`` 给了就按它过滤。

    响应九字段与 02 §7.4 的 ``NoteOut`` 逐字一致；冻结前端 ``api.ts::Note`` 里
    ``source_document_id`` / ``tags`` / ``generation_meta`` 标了可选（「列表接口可能不回」），
    本实现**照回**（老基线就是全字段），不影响前端。
    """
    return await note_service.list_notes(db, category)


@router.post("/notes", response_model=NoteOut)
async def create_note(payload: NoteIn, db: AsyncSession = Depends(get_db)):
    """新建笔记。落库后维护双链（``回归修复`` 的边写入调用点）。

    ``tags`` / ``generation_meta`` 只有给了值才写（显式 ``None`` 会写成 JSON ``'null'``
    而非 SQL ``NULL``，见 ``note_service`` docstring）。
    """
    return await note_service.create_note(
        db,
        title=payload.title,
        content=payload.content,
        category=payload.category,
        source_document_id=payload.source_document_id,
        tags=payload.tags,
        generation_meta=payload.generation_meta,
    )


@router.put("/notes/{note_id}", response_model=NoteOut)
async def update_note(note_id: int, payload: NoteUpdate, db: AsyncSession = Depends(get_db)):
    """更新笔记。不存在或已在回收站 ⇒ 404「笔记不存在」。

    ``note_id`` 是 **int 路径参数**：``/api/notes/not_a_number`` 由 FastAPI 直接 422
    （全局处理器给中文「请求参数不正确」），不是 500、也不会被当成 id 去查库。
    """
    note = await note_service.update_note(
        db,
        note_id,
        title=payload.title,
        content=payload.content,
        category=payload.category,
        tags=payload.tags,
        generation_meta=payload.generation_meta,
    )
    if note is None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    return note


@router.delete("/notes/{note_id}")
async def delete_note(note_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """删除笔记（条目 25）：**先进回收站**（软删），彻底删除走 ``DELETE /api/trash/note/{id}``。

    只置 ``Note.deleted_at``：行与链接边都保留 ⇒ ``POST /api/trash/restore`` 原样恢复。
    已在回收站里的笔记再删一次 ⇒ 404「笔记不存在」（回收站里的内容对笔记域不可见）。
    """
    note = await note_service.soft_delete_note(db, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="笔记不存在")
    return {"ok": True, "message": "已移入回收站，可在回收站恢复或彻底删除"}


# ---------------------------------------------------------------------------
# wiki
# ---------------------------------------------------------------------------


def _wiki_hit(raw: dict) -> WikiLinkHit:
    return WikiLinkHit(
        type=raw["type"],
        id=int(raw["id"]),
        title=raw["title"],
        snippet=raw.get("snippet") or "",
        category=raw.get("category") or "",
        direction=raw.get("direction") or "incoming",
    )


async def _wiki_target(
    db: AsyncSession, kind: Literal["note", "document"], item_id: int | None, title: str | None
) -> tuple[str, int | None]:
    """把请求参数解析成 ``(type, id)``：id 直接校验存在；只有 title 时走与建边同源的解析。

    ``item_id <= 0`` 按「没给 id」处理（模块 docstring 的适配点 2）：冻结前端在
    ``source_id`` 缺失时会传 ``0``，而它同时会把 ``title`` 带上——那正是标题解析的用途。
    正数 id 校验失败（行不存在 / 在回收站）⇒ 404「来源不存在」（02 §4）。
    """
    if item_id is not None and int(item_id) > 0:
        model = Note if kind == "note" else Document
        row = await db.get(model, int(item_id))
        # 回收站里的内容对回链查询不可见（条目 25），按不存在处理
        if row is None or row.deleted_at is not None:
            raise HTTPException(status_code=404, detail="来源不存在")
        return kind, int(item_id)
    if title and title.strip():
        resolved_type, resolved_id = await wikilink.resolve_title(db, 1, title.strip())
        if resolved_id is not None:
            return resolved_type or kind, int(resolved_id)
    return kind, None


@router.get("/wiki/backlinks", response_model=WikiLinksOut)
async def wiki_backlinks(
    type: Literal["note", "document"] = "note",
    id: int | None = None,
    title: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """谁链到了这篇笔记 / 文档。优先用 id；历史引用没有 id 时可按标题解析。

    只回**已解析**的入边（``resolved=True``）；自环、已消失的来源与回收站里的来源不进列表。
    目标解析不出来（既没有 id 也没有标题，或标题未创建）⇒ 空列表 + 200（不是 404）。
    """
    target_type, target_id = await _wiki_target(db, type, id, title)
    if target_id is None:
        return WikiLinksOut(items=[])
    items = await wikilink.list_backlinks(db, target_type, target_id)
    return WikiLinksOut(items=[_wiki_hit(item) for item in items])


@router.get("/wiki/related", response_model=WikiLinksOut)
async def wiki_related(
    type: Literal["note", "document"] = "note",
    id: int | None = None,
    title: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """与该来源通过链接边相连的一跳（引用卡「相关笔记」）：出链 + 入链，不走向量。

    出链排在入链之前、同向按标题升序（``wikilink.list_related`` 的口径）。
    """
    source_type, source_id = await _wiki_target(db, type, id, title)
    if source_id is None:
        return WikiLinksOut(items=[])
    items = await wikilink.list_related(db, source_type, source_id)
    return WikiLinksOut(items=[_wiki_hit(item) for item in items])


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------


@router.get("/graph", response_model=GraphOut)
async def knowledge_graph_view(db: AsyncSession = Depends(get_db)):
    """全库关系图谱：节点 + 边一次给全，筛选与布局交给前端。

    节点 = 全部在库笔记 + 文档（软删的不进）；未创建的 ``[[标题]]`` 出 ghost 节点；
    边 = ``wiki``（双链）+ ``source``（笔记溯源文档）。空库返回三个空列表 + 全 0 统计，
    不是 404——「图是空的」本身是合法状态。
    """
    data = await knowledge_graph.build_graph(db)
    return GraphOut(
        nodes=[GraphNode(**item) for item in data["nodes"]],
        edges=[GraphEdge(**item) for item in data["edges"]],
        stats=GraphStats(**data["stats"]),
    )


# ---------------------------------------------------------------------------
# knowledge-bases
# ---------------------------------------------------------------------------


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseOut])
async def list_knowledge_bases(db: AsyncSession = Depends(get_db)):
    """知识库列表。**表空时补一条本机库（id=1）**——老基线 L1132-1145 的口径。

    这条「空表自举」是必要的：本机知识库是 ``kb_id=1`` 的语义锚点（笔记 / 文档缺省归属），
    而它是**惰性建行**（首次查询列表时才落库），没有独立的种子迁移。

    **并发下的自举是幂等的**（``回归修复``）：两个请求都可能在对方提交前查到空表，
    于是都去插 ``id=1`` ⇒ 后者撞主键抛 ``IntegrityError``（改前裸抛 500）。这里捕获它、
    ``rollback`` 后**重查**：

    - 重查**确实有 id=1 行** ⇒ 别人已经建好了，照常返回全量（等价于自举成功）；
    - 重查**没有 id=1 行** ⇒ 这次冲突不是并发自举造成的（别的约束 / 别的行），**原样抛出**，
      绝不吞成 200。
    """
    rows = list((await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.id))).scalars().all())
    if not rows:
        local = KnowledgeBase(
            id=1,
            name="本机知识库",
            kind="local",
            path_or_url="",
            collection="",
            read_only=False,
            status="ready",
        )
        db.add(local)
        try:
            await db.commit()
        except IntegrityError:
            # 回滚把会话从失败事务里放出来，重查必须能跑（这条在两种方言下口径一致：
            # MySQL 的 1062 与 SQLite 的 UNIQUE 都归到 SQLAlchemy 的 IntegrityError）。
            await db.rollback()
            rows = list(
                (await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.id))).scalars().all()
            )
            if not any(row.id == 1 for row in rows):
                raise
            return rows
        await db.refresh(local)
        rows = [local]
    return rows


async def _find_knowledge_base_by_key(db: AsyncSession, path_key: str | None) -> KnowledgeBase | None:
    """按唯一身份摘要回查一行；越界「唯一域」的 key（空 path ⇒ ``None``）不查。

    ``path_or_url_key`` 上有唯一索引，所以按 key 查到的至多一行。
    """
    if path_key is None:
        return None
    result = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.path_or_url_key == path_key)
    )
    return result.scalars().first()


@router.post("/knowledge-bases", response_model=KnowledgeBaseOut)
async def create_knowledge_base(payload: KnowledgeBaseIn, db: AsyncSession = Depends(get_db)):
    """新建知识库。只允许登记 ``imported`` / ``external`` 两类，其余 400 可读中文。

    本机库（``kind="local"``）不由此端点创建——它由 ``GET /api/knowledge-bases``
    的空表自举产生，故这里显式拒绝 ``local``：否则会出现第二条「本机库」。
    登记进来的库一律 ``read_only=True`` / ``status="ready"``（老基线 L1154-1165 逐字）。

    **唯一身份（S6 · 186）**：身份 = ``path_or_url`` strip 之后的完整逐字值，落库时经
    ``app.core.kb_identity.kb_path_key`` 写成摘要列 ``path_or_url_key``，唯一性由该列上的
    唯一索引 ``uk_knowledge_bases_path_or_url_key`` 兜底（SQLite / MySQL 同一条索引）。

    真撞唯一约束 ⇒ ``IntegrityError`` ⇒ ``rollback``（把会话从失败事务里放出来）
    ⇒ 按 key 回查：

    - 回查到的行其 ``path_or_url`` 经 ``app.core.kb_identity.kb_canonical_path``（strip 后
      完整逐字值，与唯一索引同一 canonical bytes）判定**与本次请求同值** ⇒ 409
      ``{"code": "duplicate_knowledge_base", "message": "该地址已登记为知识库，请勿重复创建。",
      "existing_id": <int>}``；
    - 摘要碰撞（key 撞但原地址不同）/ 回查不到 / 撞的是别的约束 ⇒ **原样抛出**，
      ⛔ 绝不吞成 200（反向同理：非唯一冲突的 ``IntegrityError`` 不会被这里变成 409）。
    """
    kind = (payload.kind or "").strip().lower()
    if kind not in ("imported", "external"):
        raise HTTPException(status_code=400, detail="只能登记导入库或外部库")
    # 这两个值先落到**局部变量**：rollback 之后 ORM 实例的加载状态不可依赖，
    # 异常分支里的回查与逐字比对必须只吃本次请求的纯 Python 值。
    path_or_url = payload.path_or_url.strip()
    path_key = kb_path_key(path_or_url)
    item = KnowledgeBase(
        name=payload.name.strip(),
        kind=kind,
        path_or_url=path_or_url,
        path_or_url_key=path_key,
        collection=(payload.collection or "").strip(),
        read_only=True,
        status="ready",
    )
    db.add(item)
    try:
        await db.commit()
    except IntegrityError:
        # MySQL 的 1062 与 SQLite 的 UNIQUE 都归到 SQLAlchemy 的 IntegrityError（口径一致）。
        await db.rollback()
        existing = await _find_knowledge_base_by_key(db, path_key)
        existing_path = None if existing is None else kb_canonical_path(existing.path_or_url)
        if existing is not None and existing_path == kb_canonical_path(path_or_url):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "duplicate_knowledge_base",
                    "message": "该地址已登记为知识库，请勿重复创建。",
                    "existing_id": int(existing.id),
                },
            ) from None
        raise
    await db.refresh(item)
    return item
