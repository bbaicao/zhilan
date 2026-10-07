"""list_documents：列出范围内可读的文档清单（专项工具调用第 2 批 · ② · D-d/D-f/D-g）。

**契约工具名**：``list_documents``（不要改成 ``documents_list``）。

## 分工（一句话）

本模块只做「把可选 filters 收窄进宿主范围、查出范围内已就绪文档、渲染纯文本观察」；
**不产材料**（D10：纯文本进 ``tool_context``，与 ``mcp`` 分支同型），因此结果
**不会**进引用卡片 / 归因——列清单不是读正文。

## 可见性（D-d / D11，与检索两路对齐）

- 只列**文档**、不列笔记；
- ``deleted_at IS NULL`` **且** ``index_status == 'ready'``（与
  ``retrieval.keyword._load_corpus`` / ``retrieval.vector`` 同一门禁；
  ``ingest.list_documents`` 不滤 ``ready`` 是端点口径，**有意不同**，见开工包坑 27）；
- ``id desc``；上限 :data:`LIST_DOCUMENTS_LIMIT`，超限**显式**截断标记（D-g，与
  ``runner.TOOL_CONTEXT_TRUNCATION_MARK`` 同族：标明原量、不静默截断）；
- **无分页 / offset**（D-d：重复调用不扩大覆盖面）；
- 宿主范围只含 note ⇒ 可读「范围内没有可列出的文档」，**不回落全库**。

## 依赖注入

``build_spec(db=..., filters=...)`` 把这一轮要用的会话与范围闭合进 handler：

- ``db``：本轮 ``AsyncSession``；
- ``filters``：本轮宿主范围。工具参数 ``filters`` **只能收窄**它，由
  :func:`app.retrieval.types.intersect_scope_filters` 求交（唯一实现，不得另写第二套）；
  求交**可能抛** ``ValueError`` ⇒ **就地**捕获转 ``ok=False``（D-f / I5，照
  ``knowledge_search.py`` 既有范式）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agent.registry import ToolSpec
from app.agent.types import ToolObservation
from app.models import Document
from app.retrieval.keyword import READY_STATUS
from app.retrieval.types import (
    document_where,
    includes_documents,
    intersect_scope_filters,
    matches_document,
)

log = logging.getLogger(__name__)

TOOL_NAME = "list_documents"

DESCRIPTION = (
    "列出当前范围内已就绪的文档清单（文件名、id、块数、创建时间），"
    "filters 只能在本轮范围内进一步收窄"
)

#: 单次最多列出几条（**唯一真相源**；用例按符号导入，不得写第二份数字）。
#: 无分页/offset ⇒ 重复调用不扩大覆盖面（D-d / D-q）。
LIST_DOCUMENTS_LIMIT = 20

#: 空结果 / 范围内无文档可列时的观察（含「宿主范围只含 note」这一路；D-d）。
#: 不是失败：``ok=True``，且正文不得夹带任何文档 id。
EMPTY_TEXT = "范围内没有可列出的文档"

#: 段首一行（``{n}`` = 范围内匹配总数，即上限截断**之前**的份数）。
HEADER_TEMPLATE = "范围内共 {n} 份文档（按 id 倒序）"

#: 每条一行。除段首 / 条目 / 截断标记三类之外不得多任何字段
#: （``file_path`` / ``raw_content`` / ``index_error`` 一律不出现）。
LINE_TEMPLATE = "[{i}] {filename}（id={id}，{chunk_count} 块，{created_at}）"

#: 超上限时的显式截断标记（``{N}`` = 截断**之前**的总份数；与
#: ``runner.TOOL_CONTEXT_TRUNCATION_MARK`` 同族：标明原量，不得静默截断）。
TRUNCATION_MARK = "（已截断，共 {N} 份）"


class ListDocumentsArgs(BaseModel):
    """工具入参（同时是给模型的 JSON Schema 来源）。

    **只有可选** ``filters``——无分页 / offset / limit 参数（D-d）。
    ``filters`` 形状抄 :class:`app.agent.tools.knowledge_search.KnowledgeSearchArgs`
    （同一份六维描述文案，D-f）。
    """

    filters: dict[str, Any] | None = Field(
        default=None,
        description=(
            "可选过滤条件（JSON 对象）。认识的键只有这六个维度："
            "kb_ids（整数列表）/ kb_id（单个整数）、"
            "source_types（列表，取值只能是 document 或 note）/ source_type（单个）、"
            "categories（字符串列表）、tags（字符串列表，同一条资料需同时具备全部标签）、"
            "created_after / created_before（ISO 8601 日期或时间字符串）。"
            "只能在本轮范围里继续收窄，覆盖不了它；"
            "范围冲突、未知键或坏类型会返回失败观察，不会静默放宽"
        ),
    )


Args = ListDocumentsArgs


def format_created_at(value: datetime | None) -> str:
    """``created_at`` → 观察文本里的时刻（ISO 8601；缺失时给空串，不编造）。"""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def render_observation(rows: list, *, total: int) -> str:
    """把可见文档行渲染成观察文本（模板恰好三类行，A9 逐字判）。

    - 段首一行 :data:`HEADER_TEMPLATE`（``n`` = 截断前总份数）；
    - 每条一行 :data:`LINE_TEMPLATE`（编号 1 起，对应 ``rows`` 顺序 = ``id desc``）；
    - ``total > len(rows)`` 时末尾另起一行 :data:`TRUNCATION_MARK`（``N`` = 截断前总份数）。
    """
    lines = [HEADER_TEMPLATE.format(n=total)]
    for index, row in enumerate(rows, start=1):
        lines.append(
            LINE_TEMPLATE.format(
                i=index,
                filename=row.filename,
                id=row.id,
                chunk_count=row.chunk_count,
                created_at=format_created_at(row.created_at),
            )
        )
    if total > len(rows):
        lines.append(TRUNCATION_MARK.format(N=total))
    return "\n".join(lines)


async def load_visible_documents(db: Any, scope: dict | None) -> list:
    """取范围内**可列出**的文档行：``ready`` + 未软删 + ``document_where`` 下推
    + :func:`matches_document` 回表复核（F6-B/C 同型第四条路径）。

    只投影清单需要的列，**绝不**拖出 ``file_path`` / ``raw_content`` / ``index_error``
    （A9 投影腿）。返回已按 ``id desc`` 排好、且已过 matches 的行。
    """
    stmt = (
        select(
            Document.id,
            Document.filename,
            Document.category,
            Document.tags,
            Document.chunk_count,
            Document.created_at,
            Document.kb_id,
        )
        .where(
            Document.deleted_at.is_(None),
            Document.index_status == READY_STATUS,
            *document_where(scope),
        )
        .order_by(Document.id.desc())
    )
    rows = list((await db.execute(stmt)).all())
    return [
        row
        for row in rows
        if matches_document(
            scope,
            kb_id=row.kb_id,
            category=row.category,
            tags=row.tags,
            created_at=row.created_at,
        )
    ]


def build_spec(
    *,
    db: Any = None,
    filters: dict[str, Any] | None = None,
) -> ToolSpec:
    """装配 list_documents 工具声明。``db`` 为空时调用会返回可读的失败观察（不抛）。"""

    async def handler(args: ListDocumentsArgs) -> ToolObservation:
        if db is None:
            return ToolObservation(text="文档列表缺少数据库会话，本次无法列出。", ok=False)

        # 工具参数**只能收窄**本轮宿主范围，不能覆盖它（专项 F6-A / 回归修复）。
        # ⚠️ 这一行是**可能抛 ValueError 的调用点** ⇒ 校验/捕获必须**就地**做，
        # 否则异常会冲出 handler，违反 I5「工具失败转观察，绝不炸穿」（D-f）。
        # 入口用 ``intersect_scope_filters``（第 0 批泛化后的**唯一实现**），
        # 不得改它、不得写第二套。
        try:
            scope = intersect_scope_filters(filters, args.filters)
        except ValueError as exc:
            return ToolObservation(text=f"文档列表未执行：{exc}", ok=False)
        except Exception as exc:  # noqa: BLE001 —— 兜底：工具失败转观察，绝不炸穿（I5）
            log.warning("文档列表范围求交出现非预期错误：%s: %s", exc.__class__.__name__, exc)
            return ToolObservation(text=f"文档列表未执行：{exc}", ok=False)

        # 范围只含 note（或其它非 document 来源）⇒ 可读空清单，**不回落全库**（D-d）。
        if not includes_documents(scope):
            return ToolObservation(text=EMPTY_TEXT)

        try:
            rows = await load_visible_documents(db, scope)
        except Exception as exc:  # noqa: BLE001 —— 工具失败转观察，绝不炸穿（I5）
            # 回归修复：固定形状 + 原文只进日志
            log.warning("文档列表执行异常：%s", exc)
            return ToolObservation(
                text=f"文档列表 执行失败（{exc.__class__.__name__}）。", ok=False
            )

        total = len(rows)
        if total == 0:
            # 空正文不得冒充成功之外的任何形态（D-g）：给固定可读句，不写「共 0 份」模板。
            return ToolObservation(text=EMPTY_TEXT)

        shown = rows[:LIST_DOCUMENTS_LIMIT]
        # 纯文本观察、**不产材料**（D10）——materials 保持默认空列表。
        return ToolObservation(text=render_observation(shown, total=total))

    return ToolSpec(
        name=TOOL_NAME,
        description=DESCRIPTION,
        parameters_schema=ListDocumentsArgs.model_json_schema(),
        handler=handler,
        dangerous=False,
        parameters=ListDocumentsArgs,
        needs_web=False,
        needs_confirm=False,
    )


#: 无会话的默认声明（运维可见的注册表 / 既有单测用；真轮次走 ``build_spec(db=...)``）
spec = build_spec()


__all__ = [
    "Args",
    "DESCRIPTION",
    "EMPTY_TEXT",
    "HEADER_TEMPLATE",
    "LINE_TEMPLATE",
    "LIST_DOCUMENTS_LIMIT",
    "ListDocumentsArgs",
    "TOOL_NAME",
    "TRUNCATION_MARK",
    "build_spec",
    "format_created_at",
    "load_visible_documents",
    "render_observation",
    "spec",
]
