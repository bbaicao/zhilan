"""documents 域端点。

条目 2.1（上传 / 文件夹扫描 / 文件夹导入）+ 条目 2.4（reindex / clean-preview /
clean-report / generate-note / generate-note/stream / DELETE 软删）。

严格对齐 02 契约 §1.1 与老仓库行为：

- ``GET /documents`` → **裸数组** ``list[DocumentOut]``（老仓库不是包装对象）；
- ``POST /documents/upload`` → ``DocumentOut``；>20MB → 413；重复 → 409 且 detail
  是对象 ``{code, message, existing_id, filename}``；其余 RagError 直接 raise
  （main.py 已把 RagError→400 中文）；
- ``POST /documents/import-folder/scan`` → ``FolderScanOut``；路径非法 → 400；
- ``POST /documents/import-file`` → ``DocumentOut``；不存在→404、非文件→400、
  不支持后缀→400、空→400、超限→413；``fallback_category`` 传**文件夹名**；
- ``POST /documents/reindex`` → ``ReindexOut``；指定 id 不存在/在回收站 → 400；
- ``POST /documents/clean-preview`` → ``CleanPreviewOut``（**不写库**）；
- ``GET /documents/{id}/clean-report`` → ``CleanReportOut``；已删/不存在 → 404；
- ``POST /documents/{id}/generate-note`` → ``GeneratedNoteOut``（**不落库**）；
- ``POST /documents/{id}/generate-note/stream`` → SSE 四事件（status/token/done/error）；
- ``DELETE /documents/{id}`` → 软删（``deleted_at``），**不删向量**（彻底删除走回收站，阶段 4）。

``DocumentOut`` 严格 12 字段、一字不多不少；内部列 ``index_status`` / ``file_path`` /
``index_error`` 绝不进响应体。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from anyio import CancelScope
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_serializer
from app.api.response_time import response_time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat_schemas import sse
from app.cache import invalidate_all
from app.core.errors import DuplicateDocument, RagError
from app.db.session import get_db
from app.generation import note_gen as note_gen_mod
from app.models import Chunk, Document, active_chunks
from app.observe.guard import StreamGuard
from app.pipeline import reindex as reindex_mod
from app.pipeline import wikilink
from app.pipeline.folder_scan import FolderScan, FolderScanError, scan_folder
from app.pipeline.ingest import IngestResult, ingest_document, list_documents, prepare_document
from app.pipeline.parsing import SUPPORTED_FORMATS_HINT, SUPPORTED_SUFFIXES

log = logging.getLogger(__name__)

router = APIRouter()

# 与 chat 上传同款上限（20MB）；单测可 monkeypatch 以触发 413
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

#: 超限 413 的固定中文（**文案逐字冻结**；三条路径 + 预判共一份真相，避免漂移）
_TOO_LARGE_DETAIL = "文件超过限制，请压缩后重试"


# ---------------------------------------------------------------------------
# 响应模型（严格 12 字段）
# ---------------------------------------------------------------------------


class DocumentOut(BaseModel):
    """documents 文档响应体：02 §7.4 规定的 12 字段，一字不多不少。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    file_type: str
    status: str
    chunk_count: int
    vectorized: bool
    vector_error: str | None
    category: str
    tags: list | dict | None
    created_at: datetime  # 契约 02:379 的 datetime；ISO 字符串由 FastAPI 序列化负责
    message: str | None
    kb_id: int

    @field_serializer("created_at")
    def serialize_time(self, value: datetime) -> datetime:
        return response_time(value)


class FolderFileInfo(BaseModel):
    path: str
    name: str
    size_bytes: int


class FolderSkipInfo(BaseModel):
    path: str
    name: str
    reason: str


class FolderScanOut(BaseModel):
    path: str
    files: list[FolderFileInfo]
    skipped: list[FolderSkipInfo]
    truncated: bool


class FolderScanRequest(BaseModel):
    path: str
    recursive: bool = True


class FileImportRequest(BaseModel):
    path: str


class ReindexIn(BaseModel):
    """``POST /documents/reindex`` 请求体；空 body / ``document_id=None`` = 全量。"""

    model_config = ConfigDict(extra="ignore")

    document_id: int | None = None


class ReindexOut(BaseModel):
    documents: int
    notes: int
    vectors: int
    failed: int
    orphans_removed: int
    recleaned: int = 0
    messages: list[str] = Field(default_factory=list)


class CleanPreviewOut(BaseModel):
    filename: str
    file_type: str
    chunk_count: int
    content_hash: str
    raw_content: str
    clean_content: str
    report: dict
    chunks: list[str]
    removed_ratio: float
    frontmatter: dict | None


class CleanReportOut(BaseModel):
    id: int
    filename: str
    raw_content: str
    clean_content: str
    report: dict | None
    chunk_count: int


class GeneratedNoteOut(BaseModel):
    title: str
    content: str
    category: str
    tags: list[str] | None
    source_document_id: int
    source_filename: str
    completeness: dict


class DeleteDocumentOut(BaseModel):
    ok: bool
    message: str


# ---------------------------------------------------------------------------
# 响应构造
# ---------------------------------------------------------------------------


def _to_out(doc: Document, message: str | None) -> DocumentOut:
    """ORM → DocumentOut：显式取 12 字段，``message`` 响应期填充（不进库）。"""
    return DocumentOut(
        id=doc.id,
        filename=doc.filename,
        file_type=doc.file_type,
        status=doc.status,
        chunk_count=doc.chunk_count,
        vectorized=doc.vectorized,
        vector_error=doc.vector_error,
        category=doc.category,
        tags=doc.tags,
        created_at=doc.created_at,
        message=message,
        kb_id=doc.kb_id,
    )


# ---------------------------------------------------------------------------
# 上传体读取（`回归修复`）
# ---------------------------------------------------------------------------

#: 分块读上传体的块大小（1 MiB）：读到的块**拼成 bytes** 再交给 `ingest_document` /
#: `prepare_document`（两处签名不变），避免「先把远超上限的整份请求体读进内存」。
_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _read_upload_bytes(file: UploadFile) -> bytes:
    """按 ``file.size`` **预判** 413，再**分块**读上传体（`回归修复`）。

    改前是 ``data = await file.read()`` 之后才判 ``len(data)`` ⇒ 远超 20MB 的请求先被整份读进
    内存再拒（uvicorn/h11 默认不限请求体）。两条纪律：

    - ``UploadFile.size`` 由 Starlette 的 multipart 解析器填，**不是契约**（某些版本/部署可能给
      ``None``，也可能与实际体长不一致）⇒ 预判只是**提前失败**的优化，判据不靠它；
    - 真正的判据仍是调用方读完后的**二次判定**（``len(data) > MAX_UPLOAD_BYTES`` ⇒ 413，文案
      走 ``_TOO_LARGE_DETAIL``）；这里另外**边读边累计**、一超过上限就停止读取，不把整份读完。

    ⚠️ 上限在**调用时**读模块全局 ``MAX_UPLOAD_BYTES``（单测 monkeypatch 它即可触发 413）。
    """
    limit = MAX_UPLOAD_BYTES
    size = getattr(file, "size", None)
    if isinstance(size, int) and size > limit:
        raise HTTPException(status_code=413, detail=_TOO_LARGE_DETAIL)
    # ponytail: one growable buffer avoids retaining all 1 MiB chunks beside the joined body.
    buffer = bytearray()
    total = 0
    while total <= limit:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        buffer.extend(chunk)
        total += len(chunk)
    return bytes(buffer)


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get("/documents")
async def list_documents_endpoint(db=Depends(get_db)) -> list[DocumentOut]:
    """文档列表（裸数组）；失败行的 message 回填 index_error 供前端 tooltip。"""
    docs = await list_documents(db)
    return [
        _to_out(d, d.index_error if d.status == "failed" else "")
        for d in docs
    ]


@router.post("/documents/upload")
async def upload_document_endpoint(
    file: UploadFile = File(...),
    overwrite: bool = False,
    db=Depends(get_db),
) -> DocumentOut:
    """上传并入库；>20MB→413；重复→409（detail 含 code/existing_id/filename）。

    `回归修复`：先按 `file.size` 预判 413（超限**不读**），再分块读；读后的二次判定照旧保留
    （`file.size` 不是契约）。
    """
    data = await _read_upload_bytes(file)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=_TOO_LARGE_DETAIL)

    try:
        result: IngestResult = await ingest_document(
            db, file.filename or "upload", data, overwrite=overwrite
        )
    except DuplicateDocument as exc:
        # 409：detail 是对象（契约明文），existing_id 指向既有行
        raise HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_content",
                "message": str(exc),
                "existing_id": exc.document_id,
                "filename": exc.filename,
            },
        )
    # 其余 RagError / DocumentParseError 由 main.py 统一转 400 中文
    # 157 · 语义缓存失效面：**上传入库成功**即全失效（落库之后才清；失败路径不清）。
    # 裸名 + 显式 import = 产品入口契约（app/cache/__init__.py 的 invalidate_all）。
    invalidate_all(reason=f"document_upload:{result.document_id}")
    return _to_out(
        await db.get(Document, result.document_id),
        result.message,
    )


@router.post("/documents/import-folder/scan")
async def import_folder_scan_endpoint(
    body: FolderScanRequest, db=Depends(get_db)
) -> FolderScanOut:
    """扫描文件夹：返回可入库文件 / 跳过项 / 是否截断；路径非法 → 400。"""
    try:
        scan: FolderScan = scan_folder(
            body.path, recursive=body.recursive, max_bytes=MAX_UPLOAD_BYTES
        )
    except FolderScanError as exc:
        # FolderScanError 是 RagError 子类，但此处语义是「参数非法」→ 400
        raise HTTPException(status_code=400, detail=str(exc))

    return FolderScanOut(
        path=scan.path,
        files=[FolderFileInfo(path=f.path, name=f.name, size_bytes=f.size_bytes) for f in scan.files],
        skipped=[FolderSkipInfo(path=s.path, name=s.name, reason=s.reason) for s in scan.skipped],
        truncated=scan.truncated,
    )


@router.post("/documents/import-file")
async def import_file_endpoint(body: FileImportRequest, db=Depends(get_db)) -> DocumentOut:
    """导入单个本地文件：不存在→404、非文件→400、不支持→400、空→400、超限→413。

    ``fallback_category`` 传**文件所在文件夹名**（与老仓库回退规则一致）。
    """
    p = Path(body.path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在：{body.path}")
    if not p.is_file():
        raise HTTPException(status_code=400, detail=f"不是文件：{body.path}")

    suffix = p.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"暂不支持 {suffix or '该'} 格式，请上传 {SUPPORTED_FORMATS_HINT}。",
        )

    # `回归修复`：本地文件先按 `st_size` 预判（超限**不读**）；取不到大小就退回「读完再判」，
    # 判据不放松（下面那两段空 / 超限判定照旧）。
    try:
        size_bytes: int | None = p.stat().st_size
    except OSError:
        size_bytes = None
    if size_bytes is not None and size_bytes > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=_TOO_LARGE_DETAIL)

    try:
        data = p.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"读取文件失败：{exc}")

    if not data:
        raise HTTPException(status_code=400, detail="文件内容为空，无法入库。")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=_TOO_LARGE_DETAIL)

    fallback_category = p.parent.name
    # 文件夹导入「不复制」：直接记原路径为留档（stored_path），重试时回读原文件；
    # 原文件被移动/删除才会触发「留档文件已丢失」提示，符合预期。
    result: IngestResult = await ingest_document(
        db, p.name, data, fallback_category=fallback_category, stored_path=str(p)
    )
    # 157 · 语义缓存失效面：导入本地文件同样是**新增材料** ⇒ 全失效。
    # （旧判据「导入不得清缓存」已随本笔正面改写为「导入要清」，见 t3_4 载体例 4。）
    invalidate_all(reason=f"document_import_file:{result.document_id}")
    return _to_out(await db.get(Document, result.document_id), result.message)


# ---------------------------------------------------------------------------
# 条目 2.4 · reindex / clean-preview / clean-report / generate-note / delete
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """naive UTC（与全库 ``deleted_at`` / ``created_at`` 同口径）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _get_live_document(db: AsyncSession, document_id: int) -> Document:
    """取在库文档；不存在或已软删 → 404「文档不存在」。"""
    doc = await db.get(Document, document_id)
    if doc is None or doc.deleted_at is not None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc


async def _chunks_text(db: AsyncSession, document_id: int) -> str:
    """按 ordinal 拼接该文档当前内容版本的 chunks（``\\n\\n`` join）。"""
    rows = (
        (
            await db.execute(
                select(Chunk.content)
                .join(Document, Document.id == Chunk.document_id)
                .where(Chunk.document_id == document_id, active_chunks())
                .order_by(Chunk.ordinal.asc())
            )
        )
        .scalars()
        .all()
    )
    return "\n\n".join(rows)


@router.post("/documents/reindex")
async def reindex_documents_endpoint(
    body: ReindexIn | None = None, db=Depends(get_db)
) -> ReindexOut:
    """重建索引：空 body = 全量（回收站除外）；指定 id 不存在/已删 → 400。"""
    document_id = body.document_id if body is not None else None
    # RagError（文档不存在）由 main.py 统一转 400 中文
    result = await reindex_mod.reindex_documents(db, document_id=document_id)
    # 157 · 语义缓存失效面：重建索引会重切块（chunk_id 与内容都可能变）⇒ 全失效。
    invalidate_all(reason=f"document_reindex:{'all' if document_id is None else document_id}")
    return ReindexOut(
        documents=result.documents,
        notes=result.notes,
        vectors=result.vectors,
        failed=result.failed,
        orphans_removed=result.orphans_removed,
        recleaned=result.recleaned,
        messages=list(result.messages),
    )


@router.post("/documents/clean-preview")
async def clean_preview_endpoint(
    file: UploadFile = File(...), db=Depends(get_db)
) -> CleanPreviewOut:
    """清洗预览：复用 ``prepare_document``，**不写库**。>20MB → 413。

    `回归修复`：与 upload 同一把尺（先按 `file.size` 预判、再分块读、读后二次判定）。
    """
    data = await _read_upload_bytes(file)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=_TOO_LARGE_DETAIL)

    filename = file.filename or "upload"
    # 空文件 / 解析失败 → RagError → 400 中文
    # 回归修复：解析段纯 CPU/IO（MinerU OCR 全在这棵调用树里）⇒ 丢线程，预览大文件不冻结事件循环
    prepared = await asyncio.to_thread(prepare_document, filename, data)

    is_md = prepared.file_type in ("md", "markdown")
    frontmatter: dict | None = None
    if is_md:
        created = prepared.created_at
        frontmatter = {
            "category": prepared.category,
            "tags": list(prepared.tags or []),
            "created_at": created.isoformat() if created is not None else None,
            "warnings": list(prepared.meta_warnings or []),
        }

    clean_text = prepared.text
    return CleanPreviewOut(
        filename=prepared.filename,
        file_type=prepared.file_type,
        chunk_count=len(prepared.pieces),
        content_hash=prepared.content_hash,
        raw_content=prepared.raw,
        clean_content=clean_text,
        report=prepared.clean_result.report(),
        chunks=[piece.text for piece in prepared.pieces],
        removed_ratio=prepared.clean_result.removed_ratio,
        frontmatter=frontmatter,
    )


@router.get("/documents/{document_id}/clean-report")
async def clean_report_endpoint(
    document_id: int, db=Depends(get_db)
) -> CleanReportOut:
    """清洗报告：raw / clean（按 ordinal 拼 chunks）/ report / chunk_count。"""
    doc = await _get_live_document(db, document_id)
    clean_content = await _chunks_text(db, document_id)
    return CleanReportOut(
        id=doc.id,
        filename=doc.filename,
        raw_content=doc.raw_content or "",
        clean_content=clean_content,
        report=doc.clean_report if isinstance(doc.clean_report, dict) else None,
        chunk_count=doc.chunk_count,
    )


def _note_out_from(note: note_gen_mod.GeneratedNote) -> GeneratedNoteOut:
    return GeneratedNoteOut(
        title=note.title,
        content=note.content,
        category=note.category,
        tags=note.tags,
        source_document_id=note.source_document_id,
        source_filename=note.source_filename,
        completeness=note.completeness,
    )


def _require_note_model():
    """未配对话模型 → 400（与 chat 同款文案）。"""
    from app.api.chat_routes import require_chat_ready

    require_chat_ready()


def _resolve_note_model():
    """笔记生成用的对话模型 —— **辅助链路** ⇒ 走 `resolve_aux_model`（不带思考参数）。

    ⚠️ **不要改回 `resolve_chat_model()`**：笔记生成是「一次材料 → 一份草稿」的机械任务，
    不是用户要的答案；给它的请求体塞上厂商思考参数会让草稿变慢变贵，而且与
    「推理参数只作用于最终答案」这条口径直接冲突（A15 钉的就是本函数覆盖的两条路径：
    `generate-note` 同步 + `generate-note/stream` 流式）。
    """
    from app.api.chat_routes import resolve_aux_model

    return resolve_aux_model()


@router.post("/documents/{document_id}/generate-note")
async def generate_note_endpoint(document_id: int, db=Depends(get_db)) -> GeneratedNoteOut:
    """同步生成笔记草稿：404 文档不存在；未配模型 400；无正文 400；**不落库**。"""
    doc = await _get_live_document(db, document_id)
    _require_note_model()
    body_text = await _chunks_text(db, document_id)

    note = await note_gen_mod.generate_note(
        document_id=doc.id,
        filename=doc.filename,
        body=body_text,
        category=doc.category or "",
        tags=doc.tags,
        model=_resolve_note_model(),
    )
    return _note_out_from(note)


class _NoteStreamingResponse(StreamingResponse):
    async def stream_response(self, send) -> None:
        try:
            await super().stream_response(send)
        finally:
            # Starlette 的 send 断连不会自动关闭悬在 yield 的生成器。
            with CancelScope(shield=True):
                await self.body_iterator.aclose()


@router.post("/documents/{document_id}/generate-note/stream")
async def generate_note_stream_endpoint(
    document_id: int, db=Depends(get_db)
) -> StreamingResponse:
    """SSE 四事件子协议（02 §7.3）：status / token / done / error。

    404 / 400 在**流开始前**；客户端断开时 finally 取消在途生成任务。
    """
    doc = await _get_live_document(db, document_id)
    _require_note_model()
    model = _resolve_note_model()
    body_text = await _chunks_text(db, document_id)
    category = doc.category or ""
    tags = doc.tags
    filename = doc.filename

    async def _events() -> AsyncIterator[bytes]:
        queue: asyncio.Queue = asyncio.Queue()
        guard = StreamGuard()

        async def on_status(detail: str) -> None:
            await queue.put({"type": "status", "detail": detail})

        async def on_token(text: str) -> None:
            await queue.put({"type": "token", "text": text})

        async def _run() -> None:
            try:
                note = await note_gen_mod.generate_note(
                    document_id=doc.id,
                    filename=filename,
                    body=body_text,
                    category=category,
                    tags=tags,
                    model=model,
                    on_token=on_token,
                    on_status=on_status,
                )
                payload = _note_out_from(note).model_dump()
                await queue.put({"type": "done", **payload})
            except Exception as exc:  # noqa: BLE001 —— I5：错误也以 SSE error 收尾
                detail = str(exc) if isinstance(exc, RagError) else (
                    f"笔记生成失败，请稍后重试。（{exc.__class__.__name__}）"
                )
                log.warning("generate-note/stream 失败：%s", detail)
                await queue.put({"type": "error", "detail": detail})

        task = asyncio.create_task(_run())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if task.done() and queue.empty():
                        break
                    continue
                # 先序列化再登记：done 编码失败仍须允许 error 兜底。
                frame = sse(event)
                if event.get("type") == "done":
                    guard.done()
                elif event.get("type") == "error":
                    guard.error()
                yield frame
                if guard.finished:
                    break
        except Exception as exc:  # noqa: BLE001 —— consumer / serializer 也必须收尾
            log.warning("generate-note/stream 消费失败：%s", exc.__class__.__name__)
        finally:
            # 客户端断开 / 流结束：取消在途生成（契约 §7.3 行为细节）
            task.cancel()
            with CancelScope(shield=True):
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        # CancelledError / GeneratorExit 会先传播，真正断连不尝试补发终帧。
        if not guard.finished:
            frame = sse({"type": "error", "detail": "笔记生成流程异常结束，请重试。"})
            if guard.error():
                yield frame

    return _NoteStreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/documents/{document_id}")
async def delete_document_endpoint(
    document_id: int, db=Depends(get_db)
) -> DeleteDocumentOut:
    """软删：只置 ``deleted_at``，**不删向量**（彻底删除走回收站，阶段 4）。"""
    doc = await _get_live_document(db, document_id)
    doc.deleted_at = _utc_now()
    db.add(doc)
    await db.commit()
    await wikilink.maintain_source_links(db, "document", document_id)
    # 157 · 语义缓存失效面：软删后该文档的块不再可检索，但缓存条目里还留着旧答案 ⇒ 全失效。
    invalidate_all(reason=f"document_soft_delete:{document_id}")
    return DeleteDocumentOut(
        ok=True, message="已移入回收站，可在回收站恢复或彻底删除"
    )
