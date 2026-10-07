"""导出路由（阶段 4 · 条目 4.5.4）：``POST /api/export/markdown`` 的真实现。

由 ``app/api/system_routes.py`` include 进来（挂 ``/api`` 前缀下），最终路径
``/api/export/markdown``。

三条契约口径：

- **入参**：``path`` 是**必填字段**（pydantic 无默认值 ⇒ 缺了就是 422，走全局校验处理器，
  与老基线 ``ExportIn`` 同形）；``include_notes`` / ``include_documents`` 默认 ``True``，
  两个都不勾 ⇒ **400 + 可读中文**；范围字段（categories / tags / date_from / date_to /
  note_ids / document_ids）全部可选，不传或空 = 全库；
- **出参**：``output_dir`` / ``notes_exported`` / ``documents_exported`` / ``errors``
  —— 与冻结前端 ``ExportMarkdownOut`` 逐字一致（失败不静默，逐条进 ``errors``）；
- **校验失败**：:class:`ExportPathError`（路径不存在 / 是文件 / 不可写 / 两类都不选）与
  :class:`ExportScopeError`（日期写法不认 / 范围为空）都映射成 **400 + str(exc)**，
  不静默吞掉。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.export import ExportPathError, ExportScopeError, export_markdown

router = APIRouter()


class ExportIn(BaseModel):
    """请求体：字段与冻结前端 ``exportMarkdown(data)`` 逐字一致。"""

    # 必填：缺 path ⇒ 422（契约测试 test_export_markdown_no_path_422）
    path: str
    include_notes: bool = True
    include_documents: bool = True
    # 范围导出：全部可选；列表默认空 = 该维度不过滤
    categories: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None
    note_ids: list[int] = Field(default_factory=list)
    document_ids: list[int] = Field(default_factory=list)


class ExportOut(BaseModel):
    """响应体：字段与冻结前端 ``ExportMarkdownOut`` 逐字一致。"""

    output_dir: str
    notes_exported: int = 0
    documents_exported: int = 0
    errors: list[str] = Field(default_factory=list)


@router.post("/export/markdown", response_model=ExportOut)
async def export_markdown_api(
    payload: ExportIn, db: AsyncSession = Depends(get_db)
) -> ExportOut:
    """导出整库（或所选范围）为指定文件夹里的 Markdown，按分类分子文件夹。"""
    try:
        result = await export_markdown(
            db,
            payload.path,
            include_notes=payload.include_notes,
            include_documents=payload.include_documents,
            categories=payload.categories,
            tags=payload.tags,
            date_from=payload.date_from,
            date_to=payload.date_to,
            note_ids=payload.note_ids,
            document_ids=payload.document_ids,
        )
    except (ExportPathError, ExportScopeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return ExportOut(
        output_dir=result.output_dir,
        notes_exported=result.notes_exported,
        documents_exported=result.documents_exported,
        errors=result.errors,
    )
