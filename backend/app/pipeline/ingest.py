"""条目 2.1 核心：文档入库服务（解析 → 清洗 → 切块 → 状态机 → 落库）。

行为基线照老仓库 ``app/rag/ingest.py``，但按 v2 新状态机**重写**：

- **状态机** ``documents.index_status`` 四态 ``pending → processing → ready|failed``，
  与历史列 ``status`` **同步写**（前端只读 ``status``，且禁止出现 "pending" 文案——
  否则 ``StatusBadge`` 会显示原始英文串）；
- **留档**：落盘后在 ``documents.file_path`` 记绝对路径，是「失败可重试」的前提；
- **重复内容判定**：按 ``content_hash`` 查既有行（含回收站），四分支：409 / 就地重试 /
  覆盖重建 / 回收站提示；
- **向量写入**：``index_chunks``（条目 2.3）embed 全部 chunks 并 upsert Qdrant，
  point id = ``str(chunk.id)``；失败只回填 ``vectorized=false`` / ``vector_error``，
  **不**阻止 document 进 ready。

依赖的切块函数 ``chunk_document`` / ``ChunkMeta`` 由条目 2.2 的并行子代理实现，
本模块在 ``prepare_document`` 内按需 import（不在模块顶层 import，避免该依赖未就绪时
拖垮整个模块的导入；绝不自行实现、绝不用 ``chunk_text`` 顶替）。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.core import derived_counters
from app.core.errors import DuplicateDocument, RagError
from app.models import Chunk, Document, IndexBuild, active_chunks
from app.pipeline import storage
from app.pipeline import wikilink
from app.pipeline.cleaning import clean
from app.pipeline.frontmatter import Frontmatter, date_from_name, parse_frontmatter
from app.pipeline.parsing import parse_document_pages

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 正文身份：保留有效空白，与切块参数无关；旧版只用于识别待重建资料。
# ---------------------------------------------------------------------------

CONTENT_HASH_VERSION = "u1-body-sha256-v1"


def normalize_for_hash(text: str) -> str:
    """保留正文原样；有效空白属于内容身份。"""
    return text or ""


def content_hash(text: str) -> str:
    """保真正文 UTF-8 的完整 sha256，不依赖块大小或重叠。"""
    return hashlib.sha256(normalize_for_hash(text).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 准备产物
# ---------------------------------------------------------------------------


@dataclass
class Prepared:
    """一次入库准备的结果：解析 + 清洗 + 切块 + 元数据。"""

    filename: str
    file_type: str
    clean_result: object  # app.pipeline.cleaning.CleanResult
    pieces: list  # list[ChunkMeta]
    content_hash: str
    category: str
    tags: list
    created_at: datetime | None
    meta_warnings: list
    # 工具升级-3 · 回归检查/回归检查：代码 / 配置 / 前端资源后缀探测到的编解码名。
    # 探测在本模块内做（回归检查 交付「探测 + 带到 Prepared」），落库在本笔（回归检查）随 0006 一并完成。
    # 文档后缀恒为 None（它们走既有 _decode 路径，不产生编码名口径）。
    source_encoding: str | None = None
    source_sha256: str = ""
    processing_fingerprint: dict | None = None

    @property
    def text(self) -> str:
        return self.clean_result.text

    @property
    def raw(self) -> str:
        return self.clean_result.raw


def prepare_document(filename: str, data: bytes, fallback_category: str = "") -> Prepared:
    """解析 → 清洗 → 切块 → 算指纹，产出 ``Prepared``。

    步骤顺序（照老仓库）：
    1. ``parse_document_pages`` 取 (全文, 按页列表)；
    2. 后缀是 ``md``/``markdown`` 走 ``parse_frontmatter``，用返回的 ``body`` 作正文
       （YAML 在**清洗之前**剥离，否则检索命中的片段满是元数据行）；
    3. ``clean`` 跑清洗流水线；
    4. 切块：``chunk_document``（条目 2.2 提供，``ChunkMeta`` 含 ordinal/page/offset）；
    5. 内容指纹 = 保真正文 UTF-8 的完整 sha256；原文件字节另记完整摘要；
    6. category = frontmatter.category or fallback_category；created_at 同理；
       切块为空 → RagError（「文件内容为空，无法入库。」）。
    """
    from app.pipeline.builds import processing_fingerprint

    processing = processing_fingerprint()
    text, pages = parse_document_pages(filename, data)

    # frontmatter **只对 markdown 解析**（照老仓库 `_prepare` L119-124）：其它格式的
    # 正文里以 `---` 开头的内容是普通文本，按 YAML 剥掉就是真丢内容。md 时 YAML 在
    # 清洗之前剥离，raw_content / 分块都不含元数据（元数据已结构化落列）。
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix in ("md", "markdown"):
        fm = parse_frontmatter(text)
    else:
        fm = Frontmatter(body=text)

    # 切块函数由条目 2.2 并行子代理实现；此处按需 import（见模块 docstring）。
    from app.pipeline.chunking import chunk_code, chunk_document
    from app.pipeline.parsing import CODE_SUFFIXES, detect_encoding

    source_encoding: str | None = None
    if Path(filename).suffix.lower() in CODE_SUFFIXES:
        # 工具升级-3 · 回归检查 · D-回归检查：代码 / 配置 / 前端资源**不走清洗、不走 chunk_document**。
        # 理由（现读实测）：clean() 开启时会去掉行首缩进（cleaning.py 的 S2 空白归一），
        # 两个以上空格的行还会被降级成表格（S6）；chunk_document 又把 `# 注释` 当成标题
        # ⇒ 代码会被改写、行号对不上源文件，A14 必然是假的。
        # clean(text, enable=False) 是现成的「原文照入」档（text == raw，不跑 S2–S6）。
        clean_result = clean(fm.body, enable=False)
        pieces = chunk_code(clean_result.text)
        # 编码名只探测、不改内容；落库在 _finish_ingest（随 0006 一并，见 D-回归检查 归属）。
        source_encoding = detect_encoding(data)
    else:
        clean_result = clean(fm.body, pages=pages)
        pieces = chunk_document(
            clean_result.text, pages=pages or None,
            source_map=clean_result.source_map, page_spans=clean_result.page_spans,
        )
    if not pieces:
        raise RagError("文件内容为空，无法入库。")

    clean_result.chunk_provenance = [
        {
            "ordinal": piece.ordinal,
            "pages": list(dict.fromkeys(
                span["page"] for span in piece.source_ranges if span["page"] is not None
            )),
            "canonical_start": piece.start_offset,
            "canonical_end": piece.end_offset,
            "heading_path": list(piece.heading_path),
            "line_start": piece.line_start,
            "line_end": piece.line_end,
            "symbol_path": piece.symbol_path,
            "raw_ranges": list(piece.source_ranges),
        }
        for piece in pieces
    ]
    ch = content_hash(clean_result.text)
    category = fm.category or fallback_category
    created_at = fm.created_at or date_from_name(filename)
    if processing != processing_fingerprint():
        raise RagError("解析期间处理规则已变化，请重新尝试；原资料保持可读。")

    return Prepared(
        filename=filename,
        file_type=Path(filename).suffix.lower().lstrip(".") or "txt",
        clean_result=clean_result,
        pieces=pieces,
        content_hash=ch,
        category=category,
        tags=list(fm.tags),
        created_at=created_at,
        meta_warnings=list(fm.warnings),
        source_encoding=source_encoding,
        source_sha256=hashlib.sha256(data).hexdigest(),
        processing_fingerprint=processing,
    )


# ---------------------------------------------------------------------------
# 状态机：四个显式小函数，每个同步 index_status + status 两列 + 写 index_error + commit
# ---------------------------------------------------------------------------


async def mark_pending(session: AsyncSession, doc: Document) -> None:
    """建行（已落盘、未解析）：index_status=pending，status=processing。"""
    if doc.index_status == "ready":
        return
    doc.index_status = "pending"
    doc.status = "processing"
    doc.index_error = None
    session.add(doc)
    await session.commit()


async def mark_processing(session: AsyncSession, doc: Document) -> None:
    """开始解析切块：index_status=processing，status=processing。"""
    if doc.index_status == "ready":
        return
    doc.index_status = "processing"
    doc.status = "processing"
    session.add(doc)
    await session.commit()


async def mark_ready(session: AsyncSession, doc: Document) -> None:
    """成功（chunks 已落库）：index_status=ready，status=ready。"""
    doc.index_status = "ready"
    doc.status = "ready"
    doc.index_error = None
    session.add(doc)
    await session.commit()


async def mark_failed(session: AsyncSession, doc: Document, error: str) -> None:
    """失败：index_status=failed，status=failed，index_error=可读中文原因（≤255）。"""
    if doc.index_status == "ready":
        return
    doc.index_status = "failed"
    doc.status = "failed"
    doc.index_error = (error or "入库失败")[:255]
    session.add(doc)
    await session.commit()


# ---------------------------------------------------------------------------
# 入库结果 / 向量写入（条目 2.3）
# ---------------------------------------------------------------------------


@dataclass
class IngestResult:
    document_id: int
    filename: str
    chunk_count: int
    vectorized: bool
    message: str


# 模块级薄包装：index_chunks / purge_document 通过这两个名字取依赖，单测 monkeypatch 即可注入
# FakeEmbedding + 本地 path Qdrant，不必连真 embedding API / 真 Qdrant。
def get_embedding_client(*, fake: bool = False):
    from app.vector import get_embedding_client as factory

    return factory(fake=fake)


def get_vector_store(**kwargs):
    from app.vector import get_vector_store as factory

    return factory(**kwargs)


# 回归修复：Qdrant 同步写段（``ensure_collection`` + ``upsert(wait=True)``）丢线程执行，
# 不再冻结事件循环。锁保证并发入库轮的向量写**不重叠** —— 修复前这些同步段整段跑在事件
# 循环线程上、天然串行；本地模式（``QdrantClient(path=…)``）的客户端未声明线程安全，
# 丢线程后必须显式保住这一串行语义。单例的**创建**（``get_vector_store``，含 portalocker
# 文件锁）仍留在事件循环上，同样天然串行、无竞态。
_VECTOR_WRITE_LOCK = threading.Lock()


def _upsert_points(store, points: list, dim: int) -> None:
    """Qdrant 写段（同步）：建集合 + 写点。调用方 ``asyncio.to_thread`` 丢线程。"""
    with _VECTOR_WRITE_LOCK:
        store.ensure_collection(dim=dim)
        store.upsert(points)


async def index_chunks(session: AsyncSession, document: Document) -> tuple[bool, str | None]:
    """复用当前正文块重建向量；完整校验后发布，失败不改变已有可读版本。"""
    try:
        vectorized, error, _published = await _build_document(session, document)
        return vectorized, error
    except Exception as exc:  # noqa: BLE001 — 任何向量路径异常都转 vector_error
        return False, f"向量化失败：{exc}"[:255]


async def _build_document(
    session: AsyncSession, document: Document, *, snapshot=None, chunks=None
) -> tuple[bool, str | None, bool]:
    """上传、重清洗、重切块和纯向量重建共用的 prepare/validate/publish 入口。"""
    from app.pipeline import builds

    build_id = None
    store = None
    try:
        embedding = get_embedding_client()
        build = await builds.stage_build(
            session, document, embedding=embedding, snapshot=snapshot, chunks=chunks
        )
        build_id = build.id
        had_ready = document.index_status == "ready"
        try:
            store = get_vector_store()
            await builds.validate_build(
                session, build_id, embedding=embedding, store=store, upsert=_upsert_points
            )
        except Exception as exc:
            error = f"向量化失败：{exc}"[:255]
            if had_ready:
                raise RagError(error) from None
            # 首次入库允许关键词降级；失败点仅清本轮，正文仍经过准备校验。
            await session.rollback()
            if store is not None:
                await builds.clean_build_points(session, build_id, store=store)
            build = await session.get(IndexBuild, build_id, populate_existing=True)
            build.state = "validated"
            build.vectorized = False
            build.error = error
            await session.commit()
        await builds.publish_build(session, build_id, embedding=get_embedding_client())
        build = await session.get(IndexBuild, build_id, populate_existing=True)
        return bool(build.vectorized), build.error, True
    except asyncio.CancelledError:
        if build_id:
            await builds.fail_build(
                session, build_id, "本轮索引构建已取消。", store=store, state="cancelled"
            )
        else:
            await session.rollback()
        raise
    except Exception as exc:
        error = f"索引构建失败：{exc}"[:255]
        if build_id:
            await builds.fail_build(session, build_id, error, store=store)
            # 提交已落库但返回失败，按实际启用版本统计，不能误记失败或清新点。
            build = await session.get(IndexBuild, build_id, populate_existing=True)
            active = await session.scalar(
                select(Document.active_build_id).where(Document.id == build.document_id)
            )
            if active == build_id:
                return bool(build.vectorized), build.error, True
        else:
            await session.rollback()
        return False, error, False


# ---------------------------------------------------------------------------
# 内部：重复判定 / 终写入
# ---------------------------------------------------------------------------


def _current_identity(doc: Document) -> bool:
    report = doc.clean_report if isinstance(doc.clean_report, dict) else {}
    identity = report.get("content_identity")
    return isinstance(identity, dict) and identity.get("version") == CONTENT_HASH_VERSION


def _legacy_matches(
    raw_content: str | None, file_path: str | None, prepared: Prepared
) -> bool:
    """旧版摘要不作正文证据；只认相等原文或候选留档的完整原文件摘要。"""
    if raw_content == prepared.raw:
        return True
    if file_path:
        try:
            data = storage.resolve_stored(file_path).read_bytes()
            return hashlib.sha256(data).hexdigest() == prepared.source_sha256
        except (OSError, RagError):
            pass
    return False


async def _find_by_hash(
    session: AsyncSession, content_hash: str, *, prepared: Prepared | None = None
) -> Document | None:
    """查正文身份及旧版原文候选（含回收站）；不自动回填旧摘要。"""
    predicate = Document.content_hash == content_hash
    if prepared is not None:
        # ponytail: 旧版原文回查需一次全文等值扫描；存量经明确重建后改走摘要索引。
        predicate = or_(predicate, Document.raw_content == prepared.raw)
    rows = (
        await session.execute(
            select(Document).options(
                load_only(
                    Document.id,
                    Document.filename,
                    Document.index_status,
                    Document.deleted_at,
                    Document.content_hash,
                    Document.raw_content,
                    Document.clean_report,
                    Document.file_path,
                )
            ).where(predicate).order_by(
                (Document.content_hash == content_hash).desc(), Document.id.asc()
            )
        )
    ).scalars().all()
    for row in rows:
        if row.content_hash == content_hash or not _current_identity(row):
            return row
    return None


async def _find_failed_by_path(session: AsyncSession, stored: Path) -> Document | None:
    """按留档路径查最近的 ``failed`` 行（取 id 最大的那条）。

    **为什么必须有这条**：解析阶段就失败的行走不到 ``content_hash``（那一步没跑完，
    该列是 NULL），只按 hash 查会漏——同一个坏文件每传一次就多留一行 failed 垃圾行。
    ``save_upload`` 的落档名是**内容寻址**的（`<sha256[:8]>-<安全名>`），所以
    「同一份内容重复上传 → 同一个 file_path」，file_path 于是成了失败行的可靠匹配键。
    文件夹导入不复制文件，file_path 是原文件绝对路径——对「同一个文件重试」同样成立。

    **``回归修复``：回收站里的行不认领**（``deleted_at IS NULL``，与 :func:`_find_by_hash`
    的口径对齐）。不排除的后果：软删一条 ``failed`` 行后重传同一文件 ⇒ 就地重试那行并置
    ``ready``，而 ``deleted_at`` 原样保留 ⇒ 接口报「已写入 N 个分块」成功，文档列表
    （按 ``deleted_at IS NULL`` 过滤）却看不见它；再传一次还会 409 指向这条幽灵行。
    正确行为 = **另建一行**新文档入库，回收站那行原样不动（用户想恢复就恢复、想彻底删就删）。
    """
    rows = (
        await session.execute(
            select(Document)
            .options(
                load_only(
                    Document.id,
                    Document.filename,
                    Document.kb_id,
                    Document.index_status,
                    Document.deleted_at,
                    Document.file_path,
                )
            )
            .where(
                Document.file_path == str(stored),
                Document.index_status == "failed",
                # 回归修复：回收站行不认领（口径对齐 _find_by_hash 的 deleted_at 判定）
                Document.deleted_at.is_(None),
            )
            .order_by(Document.id.desc())
        )
    ).scalars().all()
    return rows[0] if rows else None


async def _close_out_duplicate_row(
    session: AsyncSession, doc: Document, *, created: bool, reason: str
) -> None:
    """重复判定命中时，把**当前这一行**收口（``回归修复`` 的本体）。

    两条分支**都必须收口**，一行都不许停在 ``pending`` / ``processing``：

    - ``created=True``（本次刚建的 pending 行）：**丢掉它** —— 重复内容不该留第二行；
    - ``created=False``（就地认领的既有失败行）：**置 ``failed`` + 中文原因** ——
      修前这里是一句 ``if created: delete+commit``，认领行（``created=False``）在 raise 之前
      **没有任何收口**：它刚被 :func:`mark_pending` 提交成 ``pending``，而本仓
      **没有清扫器** ⇒ 永久卡在 pending（列表里永远转圈、无异常提示指向它）。

    收尾自身失败（连接断 / 提交冲突…）一律**只记日志**：调用方的 ``DuplicateDocument``
    （409 文案）与「在该行上重试入库」的主结论一字不改，绝不换成收尾异常。
    """
    # 🔴 入口就把主键快照成**局部标量**（REAL-1/REAL-5 的教训）：下面任何一次 rollback 都会
    # 让 ORM 对象过期，之后哪怕只是读 ``doc.id`` 都可能触发隐式刷新（同步 IO）⇒
    # AsyncSession 里抛 MissingGreenlet。兜底分支**一律只用这个标量**，不再碰对象属性。
    doc_pk = getattr(doc, "id", None)

    if created:
        try:
            await session.delete(doc)
            await session.commit()
            return
        except Exception as exc:  # noqa: BLE001 —— 收尾失败不换主异常
            log.warning("丢弃重复待入库行失败 document_id=%s: %s", doc_pk, exc)
    else:
        try:
            await mark_failed(session, doc, reason)
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("认领行收口失败 document_id=%s: %s", doc_pk, exc)

    # 兜底：先回滚清掉坏事务，再**按主键**置 failed。
    #
    # 为什么不用 ``mark_failed(session, doc, …)``：那个函数要读 ``doc.id`` / 写它的列，
    # 而 rollback 已经让对象过期 ⇒ 它会自己抛（就是 REAL-5 抓到的那个洞）。改用一条
    # ``UPDATE … WHERE id = :pk`` 的 Core 语句，**与 ORM 对象状态完全解耦**：
    # 过期与否都能执行，且不需要再把对象加载回 session。
    #
    # ⚠️ 残余窗口（如实登记，不粉饰）：连接**彻底**不可用时这一条 UPDATE 也会失败，
    # 那一行仍可能停在 pending —— 本仓没有清扫器。本笔不引入独立 session / 重连重试，
    # **该窗口未消除**；能保证的只有「收尾失败绝不换掉调用方的 409 / 主结论」。
    if doc_pk is None:  # pragma: no cover —— 未落库的对象不该走到这里
        return
    try:
        await session.rollback()
        await session.execute(
            update(Document)
            .where(Document.id == int(doc_pk))
            .values(
                index_status="failed",
                status="failed",
                index_error="入库失败：内容重复判定后收尾失败"[:255],
            )
        )
        await session.commit()
    except Exception as exc:  # noqa: BLE001 —— 只剩日志，绝不换掉主异常
        log.warning("收尾置 failed 仍失败 document_id=%s: %s", doc_pk, exc)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 —— 连接级异常，只剩日志
            pass


async def _persist_chunks(session: AsyncSession, doc: Document, prepared: Prepared) -> list[Chunk]:
    """把切块写库（独立抽出，便于状态机测试在「写 chunks」处注入失败）。"""
    chunks = [
        Chunk(
            document_id=doc.id,
            content=piece.text,
            ordinal=piece.ordinal,
            page=piece.page,
            start_offset=piece.start_offset,
            end_offset=piece.end_offset,
            # 工具升级-3 · 回归检查（K-回归检查 的写侧）：**加列必须同笔改这里** ——
            # 不搬就是「静默丢列」（不报错、行号全 NULL，A14 必红且难定位）。
            # 文档块的这三项恒为默认 None（只有 chunk_code() 填写）。
            line_start=getattr(piece, "line_start", None),
            line_end=getattr(piece, "line_end", None),
            symbol_path=getattr(piece, "symbol_path", None),
        )
        for piece in prepared.pieces
    ]
    session.add_all(chunks)
    return chunks


def _stale_point_ids(old_ids: list, new_ids: list | set) -> list[str]:
    """**真正要删的旧点** = 旧 chunk id 集 − 新 chunk id 集（保序、去重）。

    **为什么必须减一次**（A 路对抗审计实证的 BLOCKER，不是理论顾虑）：
    MySQL 的 ``chunks.id`` 是 ``AUTO_INCREMENT`` ⇒ 删掉的 id **不复用**，旧点确实是孤儿；
    但**桌面交付档是 SQLite**（``config.py`` 的 ``is_sqlite``），而 alembic 迁移建出来的表是
    ``id INTEGER NOT NULL PRIMARY KEY``（**没有 AUTOINCREMENT**）⇒ 它是 rowid 别名，SQLite 取
    ``max(rowid)+1`` ⇒ **删掉的行 id 会被下一批新块原样复用**（实测：持最大 id 的那篇文档，
    旧 id ``[6,7,8,9,10]`` ⇒ 新 id 也是 ``[6,7,8,9,10]``）。
    若照旧「把旧 id 全删掉」，删的正是**刚刚 upsert 进去的新点** ⇒ 该文档向量检索整体失明，
    比孤儿点严重得多。减去新 id 集之后两种方言都对：

    - SQLite（id 复用）：旧 ⊆ 新 ⇒ 返回空 ⇒ 不删；新点已按同一 id 覆盖旧点，**没有孤儿**；
    - MySQL（id 不复用）：旧 ∩ 新 = ∅ ⇒ 返回旧 id 全集 ⇒ 旧点被清，孤儿消除。
    """
    fresh = {str(value) for value in new_ids}
    out: list[str] = []
    seen: set[str] = set()
    for raw in old_ids:
        text = str(raw)
        if text in fresh or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _delete_points(store, ids: list[str]) -> None:
    """Qdrant 删点段（同步）：与 :func:`_upsert_points` **共用同一把写锁**。调用方丢线程。"""
    with _VECTOR_WRITE_LOCK:
        store.delete(ids)


def _as_int_or_none(raw) -> int | None:
    """能安全转 int 就转，否则 None（**不抛**）。

    为什么单独抽出来（审计 MINOR-6）：`str.isdigit()` 与 `int()` 的判据**不等价**
    （`'²'.isdigit()` 为真但 `int('²')` 抛 ValueError）⇒ 任何「先判再转」的写法都可能
    在边角 Unicode 数字上炸。**唯一可靠的判据就是 try/except**，本函数把它收在一处。
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def _orphan_point_ids(session: AsyncSession, candidates: list[str]) -> list[str]:
    """把候选 id 收敛成**真正的孤儿点**：MySQL 里已经没有对应 ``chunks`` 行的那些。

    **为什么还要这一道**（审计 MINOR-2）：`_stale_point_ids` 只做「旧 − 新」，它的前提是
    「被减掉的那些 id 不会再属于别人」。SQLite 档这条前提**只在「新块数 ≥ 旧块数」时成立**：
    若某文档旧 10 块、清洗后只剩 6 块，多出来的 4 个 id 会被释放，而 SQLite 的 rowid 是
    ``max(rowid)+1`` ⇒ **同一窗口里入库的另一篇文档可能正好拿到这几个 id** ⇒ 照旧删就会
    删掉**别人的点**（跨文档误删）。

    这里改用「孤儿点」的定义本身作判据：**点还在、对应 chunk 行没了** ⇒ 才删。
    一次 ``SELECT`` 就够，且**与方言无关**（不再依赖任何 id 复用假设）。

    **fail-safe**：查询失败 ⇒ 返回空列表（**一个都不删**）。宁可留孤儿点（检索面降级、
    下次重建还会再判一次），也绝不误删别人正在用的点（真丢数据）。
    """
    # 🔴 逐项判「是不是整数」，**用 try/except 而不是 ``str.isdigit()``**（审计 MINOR-6）：
    # 两个谓词**不等价** —— 实测 ``'²'.isdigit() is True`` 而 ``int('²')`` 抛 ValueError
    # （上标/角标数字、罗马数字等 Unicode 数字都这样）。混用会让这条**防御分支自己炸**：
    # 异常在函数内不被捕获（下面的 try 只包住 SELECT）⇒ 穿出 ``_finish_ingest``，
    # 而那时文档**已经 mark_ready 提交** ⇒ 接口报 500 而文档其实已入库。
    # 生产路径当前不可达（candidates 恒来自 ``select(Chunk.id)`` 的 ``str(int)``），
    # 但这条分支正是为「将来 UUID / 非整数点 id」准备的 ⇒ 它必须自己不炸。
    #
    # 🔴 **非整数 id 的处置 = fail-safe「不删」**（w-108 独立复核的登记项，本笔对齐）：
    # 本函数**判不出**非整数 id 的存活证据（存活判据 = 查 ``chunks`` 表的整数主键）⇒ 按
    # 「判不出来就不删」处置，**与 SELECT 失败那支同一口径**。此前写法是「原样保留进删除批」
    # = fail-open **删除**，与 docstring 的 fail-safe 声明**方向相反**。
    # 生产不可达（id 恒为十进制整数 str）⇒ 非回归；但语义必须自洽。
    numeric: list[int] = []
    for raw in candidates:
        value = _as_int_or_none(raw)
        if value is None:
            log.warning("孤儿点判定：候选 id 非整数（%r）⇒ 判不出存活证据，本次不删点", raw)
            return []
        numeric.append(value)
    try:
        alive = {
            int(cid)
            for cid in (
                await session.execute(select(Chunk.id).where(Chunk.id.in_(numeric)))
            ).scalars().all()
        }
    except Exception as exc:  # noqa: BLE001 —— 判不出来就一个都不删（孤儿好过丢数据）
        log.warning("孤儿点判定失败（本次不删点）: %s", exc)
        return []
    # 整数项：chunks 里还活着 ⇒ 不是孤儿 ⇒ 剔除；已消失 ⇒ 真孤儿 ⇒ 保留进删除批。
    # （非整数候选在上面的循环里已经 fail-safe 返回，到不了这里。）
    return [str(raw) for raw in candidates if _as_int_or_none(raw) not in alive]


async def _delete_stale_points(point_ids: list[str]) -> None:
    """删掉被重写覆盖的旧 Qdrant 点（``回归修复``）。**失败不抛**。

    调用点只有一处：:func:`_finish_ingest` 的 ``mark_ready`` **提交之后**（理由见那里的
    注释）。失败只记日志 + ``derived_counters`` 计数 —— 孤儿点只是检索面降级，不该反噬入库
    （I5，与 :func:`purge_document` 的删点失败同款）。

    ⚠️ 这里 **不碰 MySQL**：删行已经在调用方的事务里做完了。
    """
    try:
        store = get_vector_store()
        # 同步客户端丢线程：本地模式（``QdrantClient(path=…)``）的删点不该在事件循环上阻塞；
        # 且与写点**共用** ``_VECTOR_WRITE_LOCK`` —— 102 已判定本地客户端未声明线程安全，
        # 删点同属「写」类操作，丢线程后必须和 upsert 一样不重叠访问。
        await asyncio.to_thread(_delete_points, store, list(point_ids))
    except Exception as exc:  # noqa: BLE001 —— 孤儿点只是检索面降级
        log.warning("旧向量点删除失败（%d 个点）: %s", len(point_ids), exc)
        derived_counters.bump("stale_vector_points")


async def _finish_ingest(
    session: AsyncSession,
    doc: Document,
    prepared: Prepared,
    stored: Path,
    *,
    kb_id: int,
    source_data: bytes | None = None,
) -> IngestResult:
    """准备新版正文与块，校验完整向量后切换；失败保留已有 ready 版本。"""
    from app.pipeline import builds

    doc_id = int(doc.id)
    doc = await session.get(Document, doc_id, populate_existing=True)
    await mark_processing(session, doc)
    report = {
        **prepared.clean_result.report(),
        "canonical_text": prepared.text,
        "processing_fingerprint": prepared.processing_fingerprint,
        "content_identity": {
            "version": CONTENT_HASH_VERSION,
            "source_sha256": prepared.source_sha256,
            "body_sha256": prepared.content_hash,
        },
    }
    if prepared.meta_warnings:
        report["frontmatter_warnings"] = prepared.meta_warnings
    try:
        snapshot = builds.document_snapshot(doc)
        snapshot.update(
            filename=prepared.filename, file_type=prepared.file_type,
            file_path=str(stored), kb_id=kb_id, raw_content=prepared.raw,
            clean_report=report, content_hash=prepared.content_hash,
            category=prepared.category, tags=prepared.tags or None,
            chunk_count=len(prepared.pieces), source_encoding=prepared.source_encoding,
        )
        if prepared.created_at is not None:
            snapshot["created_at"] = prepared.created_at.isoformat()
        if source_data is not None:
            # 文件夹源文件可被用户改写；为本轮保留独立的内容寻址原件。
            archive = await asyncio.to_thread(storage.save_upload, prepared.filename, source_data)
            snapshot["source_archive_path"] = str(archive)
        chunks = await _persist_chunks(session, doc, prepared)
        vectorized, error, published = await _build_document(
            session, doc, snapshot=snapshot, chunks=chunks
        )
        if not published:
            raise RagError(error or "新版索引未发布，已保留原文档。")
    except BaseException as exc:
        await session.rollback()
        failed = await session.get(Document, doc_id, populate_existing=True)
        if failed is not None and failed.index_status != "ready":
            failed.content_hash = prepared.content_hash
            failed.clean_report = report
            await mark_failed(session, failed, "索引构建已取消。" if isinstance(exc, asyncio.CancelledError) else _zh(exc))
        raise
    doc = await session.get(Document, doc_id, populate_existing=True)
    return IngestResult(
        document_id=doc_id, filename=doc.filename, chunk_count=doc.chunk_count,
        vectorized=vectorized,
        message=f"已写入 {doc.chunk_count} 个分块。" + (f"{error}" if error else ""),
    )

async def rebuild_document_links(session: AsyncSession, doc: Document, text: str) -> bool:
    """入库与显式重清洗收口：业务提交后，用完整规范正文维护链接。"""
    return await wikilink.maintain_source_links(session, "document", int(doc.id), text=text)


def _zh(exc: Exception) -> str:
    """异常转可读中文原因（已中文的 RagError 原样保留）。"""
    if isinstance(exc, RagError):
        return str(exc)
    return f"入库失败：{exc}"


# ---------------------------------------------------------------------------
# 对外：入库（上传 / 文件夹导入同一条路）
# ---------------------------------------------------------------------------


async def ingest_document(
    session: AsyncSession,
    filename: str,
    data: bytes,
    *,
    overwrite: bool = False,
    kb_id: int = 1,
    fallback_category: str = "",
    stored_path: str | Path | None = None,
) -> IngestResult:
    """解析入库一条文档；返回 ``IngestResult``。

    **顺序取舍**（满足「失败留行、不留垃圾行」）：

    1. 先落盘（``save_upload`` 是**内容寻址**的：同内容必得同一 ``file_path``）；
    2. **先按 ``file_path`` 认领上一轮失败的行走**（在解析之前！理由见下），命中即就地重试；
    3. 没有可认领的行才建 ``pending`` 行并 commit —— 这样「解析/切块失败」也能把行置
       ``failed``，符合「失败留行」要求；
    4. 解析 → 清洗 → 切块（失败即把该行置 failed 并抛，不残留半成品）；
    5. 拿到 ``content_hash`` 后再做一次内容级重复判定，命中正常行才 409。

    **为什么「认领失败行」必须在解析之前**：解析阶段就失败的行走不到
    ``content_hash``（那一步没跑完，该列为 NULL），只能靠 ``file_path`` 认领；
    而且坏文件在解析处就直接抛，若把认领放在解析之后，**每次重传都只会新建一行
    failed**，垃圾行会越堆越多。

    重复分支（含回收站行）：
    - ``file_path`` 命中 failed 行 + 非覆盖 → **就地重试**：复用同 id 重跑，不报 409；
    - ``content_hash`` 命中已删行 + 非覆盖 → DuplicateDocument（回收站文案）；
    - ``content_hash`` 命中 ``failed`` 行 + 非覆盖 → 认领那一行就地重试（改文件名重传的场景）；
    - ``content_hash`` 命中正常行 + 非覆盖 → DuplicateDocument（老文案，含 id 与文件名）；
    - ``overwrite=True`` → 验证新版后原位发布，保留 id 与历史引用。
    """
    # 1. 落盘（文件夹导入传 stored_path 表示不复制，只记原路径）
    stored = Path(stored_path) if stored_path is not None else storage.save_upload(filename, data)

    # 2. 同一个文件的上一轮失败 → 就地重试（解析之前认领，理由见 docstring）
    failed = await _find_failed_by_path(session, stored)
    if failed is not None:
        if overwrite:
            # 覆盖重建：清掉失败行，走全新入库（新 id，与老仓库 overwrite 语义一致）
            await purge_document(session, failed.id)
        else:
            return await _ingest_into(
                session,
                failed,
                filename,
                data,
                stored,
                kb_id=kb_id,
                fallback_category=fallback_category,
                created=False,
                overwrite=overwrite,
            )

    # 3. 建 pending 行
    doc = Document(
        filename=filename,
        file_type=Path(filename).suffix.lower().lstrip(".") or "txt",
        status="processing",
        index_status="pending",
        kb_id=kb_id,
        file_path=str(stored),
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)

    return await _ingest_into(
        session,
        doc,
        filename,
        data,
        stored,
        kb_id=kb_id,
        fallback_category=fallback_category,
        created=True,
        overwrite=overwrite,
    )


async def _ingest_into(
    session: AsyncSession,
    doc: Document,
    filename: str,
    data: bytes,
    stored: Path,
    *,
    kb_id: int,
    fallback_category: str,
    created: bool,
    overwrite: bool = False,
) -> IngestResult:
    """在给定行上跑完入库：置 pending → 解析切块 → 内容级重复判定 → 落库。

    ``created`` 表示 ``doc`` 是不是本次刚建的行：只有刚建的行才在「命中别的行」时被
    丢弃（就地重试的行本身就是那条失败行，不该被删掉）。
    ``overwrite=True`` 时，内容指纹命中的**正常**行会被 purge 掉再继续入库。
    """
    await mark_pending(session, doc)

    try:
        # 回归修复：解析段（``prepare_document → parse_document_pages``：MinerU OCR /
        # nvidia-smi 探测 / 解码 / 清洗 / 切块全在这棵调用树里）是纯 CPU/IO、不碰 session
        # ⇒ 整体丢线程执行；持有 session 的前后段（mark_pending / 重复判定 / 落库）留在
        # 事件循环。名字走模块全局查找，测试桩（monkeypatch ``ingest.parse_document_pages``）
        # 照常生效。
        prepared = await asyncio.to_thread(
            prepare_document, filename, data, fallback_category=fallback_category
        )
    except Exception as exc:
        await mark_failed(session, doc, _zh(exc))
        raise

    # 内容级重复判定（正常路径的重复识别，口径照老仓库）
    existing = await _find_by_hash(session, prepared.content_hash, prepared=prepared)
    if existing is not None and existing.id != doc.id:
        # ``回归修复`` · REAL-1（两路审计实证）：**先快照标量**，别等到 raise 时再读 ORM 属性。
        # 上面 :func:`_close_out_duplicate_row` 的兜底分支会 ``session.rollback()``，而 rollback
        # **会让 session 内所有持久对象过期** ⇒ 之后再读 ``existing.id`` / ``existing.filename``
        # 会触发隐式刷新（同步 IO）⇒ 在 AsyncSession 里抛 ``MissingGreenlet``，把该给的 **409**
        # 换成 **500** —— 正是「收尾失败绝不换掉主异常」要防的那件事。快照成局部标量后与
        # session 状态彻底解耦（本文件内不再用 ``existing`` 的属性构造异常）。
        existing_id = int(existing.id)
        existing_name = str(existing.filename or "")
        legacy = not _current_identity(existing)
        if legacy and not await asyncio.to_thread(
            _legacy_matches, existing.raw_content, existing.file_path, prepared
        ):
            # 旧版去空白摘要同值不证明正文相等，覆盖请求也不能据此删除原资料。
            reason = f"文档（id={existing_id}，{existing_name}）使用旧版内容摘要，无法确认正文身份，请先重建索引后再上传。"
            await _close_out_duplicate_row(session, doc, created=created, reason=reason)
            raise DuplicateDocument(
                reason, document_id=existing_id, filename=existing_name
            )
        if existing.deleted_at is not None:
            # 回收站里有同内容：先提示恢复或彻底删除（老仓库语义，覆盖也先提示）
            # 回归修复：认领行也要收口（created=False ⇒ 置 failed），不许停在 pending
            await _close_out_duplicate_row(
                session,
                doc,
                created=created,
                reason="该内容在回收站中，可恢复或彻底删除后再上传",
            )
            raise DuplicateDocument(
                "该内容在回收站中，可恢复或彻底删除后再上传",
                document_id=existing_id,
                filename=existing_name,
            )
        if existing.index_status == "failed":
            # 同内容落在**另一条**失败行上（典型场景：改了文件名重传）——认领它重试，
            # 同样不报 409、不新增行
            # 回归修复：认领行同样要收口 —— 本分支把这一轮落到 existing 上，
            # 当前行（created=False 时就是认领行）若不收口就永久停在 pending。
            await _close_out_duplicate_row(
                session,
                doc,
                created=created,
                reason=f"入库失败：该内容已在另一条记录（id={existing_id}）上重试入库。",
            )
            existing = await session.get(Document, existing_id, populate_existing=True)
            return await _finish_ingest(
                session, existing, prepared, stored, kb_id=kb_id, source_data=data
            )
        if overwrite:
            await _close_out_duplicate_row(
                session, doc, created=created, reason="覆盖重建已转入既有文档版本。"
            )
            existing = await session.get(Document, existing_id, populate_existing=True)
            return await _finish_ingest(
                session, existing, prepared, stored, kb_id=kb_id, source_data=data
            )
        else:
            reason = f"知识库里已有同内容文档（id={existing_id}，{existing_name}）。"
            if legacy:
                reason += "该文档使用旧版内容摘要，请先重建索引。"
            await _close_out_duplicate_row(
                session,
                doc,
                created=created,
                reason=reason,
            )
            raise DuplicateDocument(
                reason,
                document_id=existing_id,
                filename=existing_name,
            )

    # 落库。失败由 _finish_ingest 负责置 failed 并抛出。
    return await _finish_ingest(
        session, doc, prepared, stored, kb_id=kb_id, source_data=data
    )


async def retry_document(session: AsyncSession, document_id: int) -> IngestResult:
    """从留档重读字节重跑入库（失败可重试的入口）。

    留档丢失 → 置 failed + index_error「留档文件已丢失，请重新上传。」并抛 RagError。
    """
    doc = await session.get(Document, document_id)
    if doc is None:
        raise RagError("文档不存在，无法重试。")

    if not doc.file_path:
        await mark_failed(session, doc, "留档文件已丢失，请重新上传。")
        raise RagError("留档文件已丢失，请重新上传。")
    try:
        stored = storage.resolve_stored(doc.file_path)
        data = stored.read_bytes()
    except RagError:
        await mark_failed(session, doc, "留档文件已丢失，请重新上传。")
        raise RagError("留档文件已丢失，请重新上传。")

    # 重置 pending 后重跑
    await mark_pending(session, doc)
    # 回归修复：先在事件循环上取好 ORM 标量再丢线程（工作线程内不碰 ORM 属性）
    filename = doc.filename
    fallback_category = doc.category or ""
    try:
        prepared = await asyncio.to_thread(
            prepare_document, filename, data, fallback_category=fallback_category
        )
    except Exception as exc:
        await mark_failed(session, doc, _zh(exc))
        raise

    return await _finish_ingest(
        session, doc, prepared, stored, kb_id=doc.kb_id or 1, source_data=data
    )


async def purge_document(session: AsyncSession, document_id: int) -> None:
    """硬删该文档全部历史空间的点与 SQL 材料；向量失败仍计数并继续硬删。

    - 软删（``deleted_at``）**不**走本函数，因而**不**删向量（与老仓库一致）；
    - 向量删除失败不抛，保证 MySQL 行仍能清掉（避免 Qdrant 不可用时文档卡死）；
    - 向量删除失败**计入** ``derived_counters.bump("stale_vector_points")``（回归修复：
      与 ``_delete_stale_points`` 同键同义 ⇒ 残留点在 ``failure_counts()`` 可见）。
    """
    chunk_ids = (
        await session.execute(select(Chunk.id).where(
            Chunk.document_id == document_id, Chunk.build_id.is_(None)
        ))
    ).scalars().all()
    builds = list((await session.execute(
        select(IndexBuild).where(IndexBuild.document_id == document_id)
    )).scalars().all())
    groups = [(None, [str(cid) for cid in chunk_ids])]
    groups.extend((build.space_id, list(build.point_ids)) for build in builds)
    for space, ids in groups:
        if not ids:
            continue
        try:
            store = get_vector_store()
            scoped = store.for_space(space) if space else store
            await asyncio.to_thread(_delete_points, scoped, ids)
        except Exception as exc:  # noqa: BLE001 — 向量清不掉不挡 MySQL 硬删
            log.warning(
                "purge_document 删除向量失败 document_id=%s（%d 个点）: %s",
                document_id,
                len(ids),
                exc,
            )
            # 回归修复：与同文件 ``_delete_stale_points`` 同键同义——向量没删干净必须
            # **可观测**（键义 = 失败计数，不表示点已删；孤儿点仍靠 reindex 兜底）。
            derived_counters.bump("stale_vector_points")

    await session.execute(delete(Chunk).where(Chunk.document_id == document_id))
    await session.execute(delete(IndexBuild).where(IndexBuild.document_id == document_id))
    # 去重查询可能已缓存只加载部分字段的文档；显式取齐删除后的关系快照。
    doc = await session.get(
        Document, document_id,
        options=[load_only(Document.kb_id, Document.filename)],
        populate_existing=True,
    )
    old_kb_id = int(doc.kb_id or 1) if doc is not None else None
    old_title = (doc.filename or "").rsplit(".", 1)[0] if doc is not None else None
    if doc is not None:
        await session.delete(doc)
    await session.commit()
    # 双链边随硬删清理（任务清单 13 / 回归修复）：出链整删、指向它的入边按当前事实
    # 重解析（可能转「未创建」）。必须在**行删掉并提交之后**调用——重解析查不到同名目标
    # 才会转未解析（``wikilink.remove_links_for_deleted`` docstring 的口径）。
    # 自带失败隔离：出错回滚并记日志，不影响硬删本身（老基线 `rag/ingest.py:465` 同款）。
    await wikilink.remove_links_for_deleted(
        session, "document", int(document_id), old_kb_id=old_kb_id, old_title=old_title
    )


async def rechunk_document(session: AsyncSession, document_id: int) -> IngestResult:
    """仅切块/位置参数变化时复用规范正文和位置图，不重解析原文件。"""
    from app.pipeline.builds import document_snapshot, processing_fingerprint
    from app.pipeline.chunking import chunk_code, chunk_document
    from app.pipeline.parsing import CODE_SUFFIXES

    doc = await session.get(Document, document_id, populate_existing=True)
    if doc is None or doc.deleted_at is not None:
        raise RagError("文档不存在，无法重新切块。")
    report = copy.deepcopy(doc.clean_report or {})
    processing = processing_fingerprint()
    text = report.get("canonical_text")
    if not isinstance(text, str):
        raise RagError("缺少规范正文留存，请重新上传或重新清洗。")
    pieces = (
        chunk_code(text) if "." + doc.file_type in CODE_SUFFIXES else
        chunk_document(text, source_map=report.get("source_map"),
                       page_spans=report.get("page_spans"))
    )
    if not pieces:
        raise RagError("文件内容为空，无法重新切块。")
    prepared = Prepared(
        filename=doc.filename, file_type=doc.file_type,
        clean_result=None, pieces=pieces, content_hash=doc.content_hash,
        category=doc.category or "", tags=doc.tags or [],
        created_at=doc.created_at, meta_warnings=[],
    )
    chunks = await _persist_chunks(session, doc, prepared)
    report["chunk_provenance"] = [
        {
            "ordinal": piece.ordinal,
            "pages": list(dict.fromkeys(
                span["page"] for span in piece.source_ranges if span["page"] is not None
            )),
            "canonical_start": piece.start_offset, "canonical_end": piece.end_offset,
            "heading_path": list(piece.heading_path), "line_start": piece.line_start,
            "line_end": piece.line_end, "symbol_path": piece.symbol_path,
            "raw_ranges": list(piece.source_ranges),
        } for piece in pieces
    ]
    snapshot = document_snapshot(doc)
    report["processing_fingerprint"] = processing
    snapshot.update(clean_report=report, chunk_count=len(chunks))
    # 当前正文版本的不可变原件也随纯重切块版本保留。
    if doc.active_build_id:
        previous = await session.get(IndexBuild, doc.active_build_id)
        if previous and previous.snapshot.get("source_archive_path"):
            snapshot["source_archive_path"] = previous.snapshot["source_archive_path"]
    ok, error, published = await _build_document(
        session, doc, snapshot=snapshot, chunks=chunks
    )
    if not published:
        raise RagError(error or "重新切块失败，已保留原版本。")
    return IngestResult(
        document_id=document_id, filename=doc.filename, chunk_count=len(chunks),
        vectorized=ok, message=error or f"已写入 {len(chunks)} 个分块。",
    )


async def list_documents(session: AsyncSession) -> list[Document]:
    """文档列表：过滤回收站、按 id desc（老仓库口径）。

    不把 ``raw_content`` 等大字段拖进列表（load_only）；失败行的 ``index_error``
    单独取，供端点回填 ``message`` 做失败 tooltip。
    """
    rows = (
        await session.execute(
            select(Document).options(
                load_only(
                    Document.id,
                    Document.filename,
                    Document.file_type,
                    Document.status,
                    Document.index_status,
                    Document.chunk_count,
                    Document.vectorized,
                    Document.vector_error,
                    Document.category,
                    Document.tags,
                    Document.created_at,
                    Document.kb_id,
                    Document.index_error,
                )
            ).where(Document.deleted_at.is_(None)).order_by(Document.id.desc())
        )
    ).scalars().all()
    return list(rows)
