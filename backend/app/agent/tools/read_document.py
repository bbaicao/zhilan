"""read_document：按 document_id 有界阅读知识库文档分块（专项工具调用第 2 批 · 第 2 笔）。

**契约工具名**：``read_document``。

## 口径（开工包 §0.7 D-e/D-f/D-g + D9/D11/D12 · §2.2）

- 参数：``document_id``（必填 int）+ ``start_chunk``（**1 起**，内部 ``ordinal = start_chunk - 1``）
  + ``max_chunks``（默认 **4**，天花板 **8**）+ 可选 ``filters``（六维，只能收窄宿主范围）；
- 正文源 = ``chunks.content``（``ordinal`` 升序、窗口内），**不读也不暴露** ``raw_content`` / ``file_path``；
- 窗口尾部还有块 ⇒ 显式标记「还有 N 块」（D-e/D-g，不静默截断、不空正文冒充成功）；
- **逐块**产 :class:`~app.retrieval.types.Material`（真实 ``chunk_id`` / ``block_ordinal`` /
  ``document_id``，``score = 0.0``，其余分数字段与 ``url`` / ``note_id`` 恒 ``None``）——
  **禁止**占位/编造 chunk id（归因 FK + 静默回滚，D9）；
- 范围：``intersect_scope_filters``（**唯一实现**，就地 ``try/except ValueError`` ⇒ ``ok=False``）
  + ``document_where`` 下推 + ``matches_document`` 回表复核（F6-B/C 同型第四条路径）；
- 存活（D11）：``deleted_at IS NULL`` 且 ``index_status == 'ready'``；
- 坏参数（D12）：六态里走 handler 的五态一律 ``ok=False`` + 中文可读 + 零材料
  （非整数 ``document_id`` 由宿主/pydantic 拒绝，不进 handler）；
  其中**三处取值越界**改走 ``_errors`` 的**唯一实现**（R2-01/R2-02：四件齐备 + 现算 schema 摘要）。

## 观察文本（A14 逐字三件，模板外多一句即红）

1. 块号区间：``块号区间（第 a–b 块 / 共 N 块）``（a/b 为 1 起闭区间，N 为全文总块数）；
2. 正文预览：窗口内块正文 ``\\n\\n`` 拼接（超 :data:`OBSERVATION_BODY_LIMIT` 时带既有截断标记）；
3. 截断提示（如有）：``还有 {remaining} 块``。

**不要**添加任何未被保证的断言式句子。成功路径的材料观察会由
``ToolCollector.absorb`` 用本轮快照编号重渲染（I3）；本模板是 handler 产物的冻结形状。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.agent.registry import ToolSpec
from app.agent.tools._errors import arg_error
from app.agent.types import ToolObservation
from app.models import Chunk, Document, active_chunks
from app.retrieval.keyword import READY_STATUS
from app.retrieval.types import (
    Material,
    document_where,
    intersect_scope_filters,
    matches_document,
)

log = logging.getLogger(__name__)

TOOL_NAME = "read_document"

DESCRIPTION = (
    "按 document_id 分页阅读知识库文档的分块正文（块号从 1 起，单次有界）；"
    "filters 只能在本轮范围内进一步收窄"
)

#: 单次默认读几块（D-e）
DEFAULT_MAX_CHUNKS = 4

#: 单次块数天花板（D-e；超出**不**夹紧，按坏参数拒绝——D12「不得吞成不限」）
MAX_CHUNKS = 8

#: 观察文本里正文预览的硬上限（超限截断 + 显式标记，D-g；材料正文不截断）
OBSERVATION_BODY_LIMIT = 4000

#: 第一件：块号区间（A14 逐字；a/b = 1 起闭区间，N = 全文总块数）
RANGE_TEMPLATE = "块号区间（第 {start}–{end} 块 / 共 {total} 块）"

#: 第三件（如有）：窗口尾部截断提示（D-e「还有 N 块」）
REMAINING_TEMPLATE = "还有 {remaining} 块"

#: 正文截断标记（与 ``runner.TOOL_CONTEXT_TRUNCATION_MARK`` 同族口径，D-g）
TRUNCATION_MARK = "（已截断，原 {chars} 字符）"

#: 「不存在」与「不在范围」**共用同一条文案**（``回归修复``）：越权者不得借错误文案探活
#: 范围外文档的**存在性 / 软删态 / 就绪态** —— 这两个原因必须**不可区分**。范围内的软删 /
#: 未就绪仍给既有可读诊断（那是用户自己的资料，不构成越权面，见 D-C8）。
NOT_FOUND_TEMPLATE = "读取文档失败：文档不存在（document_id={document_id}）。"

#: filters 六维描述文案（D-f：与 ``KnowledgeSearchArgs.filters`` **同一份**）
FILTERS_DESCRIPTION = (
    "可选过滤条件（JSON 对象）。认识的键只有这六个维度："
    "kb_ids（整数列表）/ kb_id（单个整数）、"
    "source_types（列表，取值只能是 document 或 note）/ source_type（单个）、"
    "categories（字符串列表）、tags（字符串列表，同一条资料需同时具备全部标签）、"
    "created_after / created_before（ISO 8601 日期或时间字符串）。"
    "只能在本轮范围里继续收窄，覆盖不了它；"
    "范围冲突、未知键或坏类型会返回失败观察，不会静默放宽"
)


class ReadDocumentArgs(BaseModel):
    """工具入参（同时是给模型的 JSON Schema 来源）。

    ⚠️ ``start_chunk`` / ``max_chunks`` **故意不加** ``ge`` / ``le``：A11 要求
    ``start_chunk=0`` 与 ``max_chunks=99`` 走 **handler** 产出中文失败观察，
    而不是被 pydantic 在装配层用英文校验错误拒掉（那条链路归 P2/P&E 形态）。
    """

    document_id: int = Field(description="文档 id（知识库 documents.id）")
    start_chunk: int = Field(
        default=1,
        description="起始块号（从 1 起；对应 chunks.ordinal = start_chunk - 1）",
    )
    max_chunks: int = Field(
        default=DEFAULT_MAX_CHUNKS,
        description=f"本次最多读几块（默认 {DEFAULT_MAX_CHUNKS}，不能超过 {MAX_CHUNKS}）",
    )
    filters: dict[str, Any] | None = Field(default=None, description=FILTERS_DESCRIPTION)


Args = ReadDocumentArgs


def _clip_body(text: str, limit: int = OBSERVATION_BODY_LIMIT) -> str:
    """正文预览有界截断 + **显式**标记（D-g：不得静默截断）。标记计入上限。"""
    value = str(text or "")
    if len(value) <= limit:
        return value
    mark = TRUNCATION_MARK.format(chars=len(value))
    keep = max(0, limit - len(mark))
    return value[:keep].rstrip() + mark


def render_observation(
    *,
    start: int,
    end: int,
    total: int,
    body: str,
    remaining: int,
) -> str:
    """拼 A14 三件观察文本（块号区间 + 正文预览 + 截断提示（如有））。"""
    parts = [
        RANGE_TEMPLATE.format(start=start, end=end, total=total),
        body,
    ]
    if remaining > 0:
        parts.append(REMAINING_TEMPLATE.format(remaining=remaining))
    return "\n".join(parts)


def build_spec(
    *,
    db: Any = None,
    filters: dict[str, Any] | None = None,
) -> ToolSpec:
    """装配 read_document 工具声明。``db`` 为空时调用返回可读失败观察（不抛）。"""

    async def handler(args: ReadDocumentArgs) -> ToolObservation:
        # --- 坏参数（D12 / A11）：先校验、再回给模型；不夹紧、不吞成「不限」 ---
        start_chunk = int(args.start_chunk)
        max_chunks = int(args.max_chunks)
        if start_chunk < 1:
            return arg_error(
                tool=TOOL_NAME,
                field="start_chunk",
                constraint="不小于 1 的整数",
                received=start_chunk,
                parameters=ReadDocumentArgs,
            )
        if max_chunks < 1:
            return arg_error(
                tool=TOOL_NAME,
                field="max_chunks",
                constraint="不小于 1 的整数",
                received=max_chunks,
                parameters=ReadDocumentArgs,
            )
        if max_chunks > MAX_CHUNKS:
            return arg_error(
                tool=TOOL_NAME,
                field="max_chunks",
                constraint=f"不大于 {MAX_CHUNKS} 的整数",
                received=max_chunks,
                parameters=ReadDocumentArgs,
            )
        if db is None:
            return ToolObservation(text="读取文档缺少数据库会话，本次无法读取。", ok=False)

        # --- 范围求交（D-f）：就地捕获，绝不冲出 handler（I5） ---
        try:
            scope = intersect_scope_filters(filters, args.filters)
        except ValueError as exc:
            return ToolObservation(text=f"读取文档未执行：{exc}", ok=False)
        except Exception as exc:  # noqa: BLE001 —— 工具失败转观察，绝不炸穿（I5）
            log.warning("读取文档范围求交出现非预期错误：%s: %s", exc.__class__.__name__, exc)
            return ToolObservation(text=f"读取文档未执行：{exc}", ok=False)

        document_id = int(args.document_id)
        start_ordinal = start_chunk - 1

        try:
            # 先按 id 取行（含软删 / 非 ready），才能把 D12 六态拆成可读中文原因。
            head = (
                await db.execute(
                    select(
                        Document.id,
                        Document.filename,
                        Document.category,
                        Document.kb_id,
                        Document.tags,
                        Document.created_at,
                        Document.index_status,
                        Document.deleted_at,
                    ).where(Document.id == document_id)
                )
            ).first()
            if head is None:
                return ToolObservation(
                    text=NOT_FOUND_TEMPLATE.format(document_id=document_id),
                    ok=False,
                )
            (
                _doc_id,
                filename,
                category,
                kb_id,
                tags,
                created_at,
                index_status,
                deleted_at,
            ) = head

            # 范围判定**前移**（``回归修复``）：越权与「不存在」**同一条文案** ⇒ 拿不到
            # 「已删除 / 尚未就绪 / 存在」这三态中的任何一态。改前这三次判定都在范围回表**之前**，
            # 范围外的文档能借文案把「存在 / 已删 / 未就绪」探出来（P1 信息泄漏）。
            if not matches_document(
                scope,
                kb_id=kb_id,
                category=category,
                tags=tags,
                created_at=created_at,
            ):
                return ToolObservation(
                    text=NOT_FOUND_TEMPLATE.format(document_id=document_id),
                    ok=False,
                )

            if deleted_at is not None:
                return ToolObservation(
                    text=f"读取文档失败：文档已删除（document_id={document_id}）。",
                    ok=False,
                )
            if (index_status or "") != READY_STATUS:
                return ToolObservation(
                    text=(
                        f"读取文档失败：文档尚未就绪（document_id={document_id}，"
                        f"index_status={index_status or ''}）。"
                    ),
                    ok=False,
                )

            # 总块数（真实行计数，不信任 chunk_count 缓存列）
            total = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(Chunk)
                        .join(Document, Document.id == Chunk.document_id)
                        .where(Chunk.document_id == document_id, active_chunks())
                    )
                ).scalar_one()
                or 0
            )

            # 窗口：ordinal ∈ [start_ordinal, start_ordinal + max_chunks)
            # 联表 documents 以便 document_where 同维下推（与 matches_document 一套语义）。
            rows = (
                await db.execute(
                    select(
                        Chunk.id,
                        Chunk.content,
                        Chunk.ordinal,
                        Chunk.page,
                        # 工具升级-3 · 回归检查 · D-回归检查：**显式列子集必须同笔扩列** ——
                        # 只取 4 列 + 解 4 元组会让新列「经检索链路读不出」，同样不报错（K-回归检查）。
                        Chunk.line_start,
                        Chunk.line_end,
                    )
                    .join(Document, Document.id == Chunk.document_id)
                    .where(
                        Chunk.document_id == document_id,
                        active_chunks(),
                        Chunk.ordinal >= start_ordinal,
                        Chunk.ordinal < start_ordinal + max_chunks,
                        Document.deleted_at.is_(None),
                        Document.index_status == READY_STATUS,
                        *document_where(scope),
                    )
                    .order_by(Chunk.ordinal.asc())
                )
            ).all()
        except Exception as exc:  # noqa: BLE001 —— 工具失败转观察，绝不炸穿（I5）
            # 回归修复：固定形状 + 原文只进日志（异常里可能带 SQL / 路径 / 连接串）
            log.warning("读取文档执行异常：%s", exc)
            return ToolObservation(
                text=f"读取文档 执行失败（{exc.__class__.__name__}）。", ok=False
            )

        if not rows:
            # 越范围 / 空窗：不得空正文冒充成功（D12/D-g）
            if total <= 0:
                return ToolObservation(
                    text=f"读取文档失败：文档没有可读分块（document_id={document_id}）。",
                    ok=False,
                )
            return ToolObservation(
                text=(
                    f"读取文档失败：起始块号超出文档范围"
                    f"（document_id={document_id}，start_chunk={start_chunk}，共 {total} 块）。"
                ),
                ok=False,
            )

        materials = []
        contents: list[str] = []
        for chunk_id, content, ordinal, page, line_start, line_end in rows:
            text = content or ""
            # 工具升级-3 · 回归检查：有行号时在**该块正文前**加一行 `行 {a}-{b}`。
            # 两列都为空时正文与改前**逐字相同**（文档块走这一支 ⇒ 既有观察文本零变化）。
            # ⚠️ 不得改 `render_observation` 的参数 —— 那是另一条已钉住的观察格式（模板外多一句即红）。
            if line_start is not None and line_end is not None:
                text = f"行 {int(line_start)}-{int(line_end)}\n{text}"
            contents.append(text)
            # D9：真实 chunks 行身份；score=0.0；url/note_id/各子分恒 None（dataclass 缺省）
            materials.append(
                Material(
                    chunk_id=int(chunk_id),
                    document_id=document_id,
                    content=text,
                    block_ordinal=int(ordinal or 0),
                    page=int(page) if page is not None else None,
                    filename=(filename or "").strip() or f"文档 {document_id}",
                    category=category or "",
                    kb_id=int(kb_id or 1),
                    score=0.0,
                )
            )

        end = start_chunk - 1 + len(rows)  # 1 起闭区间右端
        remaining = max(0, total - end)
        body = _clip_body("\n\n".join(contents))
        text = render_observation(
            start=start_chunk,
            end=end,
            total=total,
            body=body,
            remaining=remaining,
        )
        return ToolObservation(text=text, materials=materials)

    return ToolSpec(
        name=TOOL_NAME,
        description=DESCRIPTION,
        parameters_schema=ReadDocumentArgs.model_json_schema(),
        handler=handler,
        dangerous=False,
        parameters=ReadDocumentArgs,
        needs_web=False,
        needs_confirm=False,
    )


#: 无会话的默认声明（运维可见的注册表 / 既有单测用；真轮次走 ``build_spec(db=...)``）
spec = build_spec()


__all__ = [
    "Args",
    "DEFAULT_MAX_CHUNKS",
    "DESCRIPTION",
    "FILTERS_DESCRIPTION",
    "MAX_CHUNKS",
    "OBSERVATION_BODY_LIMIT",
    "REMAINING_TEMPLATE",
    "RANGE_TEMPLATE",
    "ReadDocumentArgs",
    "TOOL_NAME",
    "TRUNCATION_MARK",
    "build_spec",
    "render_observation",
    "spec",
]
