"""Markdown 导出域（条目 17 / 阶段 4 · 条目 4.5.4）：把笔记 + 文档原文写成本机文件夹里的 Markdown。

对外只有一个入口 :func:`app.export.markdown.export_markdown`，由
``app/api/export_routes.py`` 映射成 ``POST /api/export/markdown``。

口径（逐条对照老基线 ``<legacy-repo>\\backend\\app\\rag\\export.py``）：

- 在用户选的目录下新建带时间戳的 ``知澜导出-YYYYMMDD-HHMMSS/``，内分 ``notes/``、
  ``documents/`` 两棵树，按分类建子文件夹（空分类归「未分类」）；
- 笔记写 YAML frontmatter（title / category / tags / created_at，带溯源时含
  ``source_document_id``）+ 正文原样；序列化手写，导出的文件再导入时元数据仍能读回；
- 文档优先 ``raw_content``（S2 清洗留档的原文），没有时按 ``ordinal`` 拼
  ``Chunk.content`` 兜底；一律写成 ``<stem>.md``，frontmatter 的 ``source_file``
  记录原始文件名；
- **失败不静默**：路径不存在 / 是文件 / 不可写都抛带中文原因的
  :class:`ExportPathError`；单个文件写失败收集进 ``errors`` 继续导其余；
- 范围导出：categories / tags / 日期区间 / 自选 id 各维度取交集（不传或空 = 全库）；
  范围内没有任何可导出的内容时抛 :class:`ExportScopeError`——在建输出目录**之前**判定，
  空范围绝不创建目录；
- **回收站里的行不导出**（``deleted_at`` 非空）：导出的是活库，与条目 25 同口径。
"""

from __future__ import annotations

from app.export.markdown import (
    UNCATEGORIZED,
    ExportPathError,
    ExportResult,
    ExportScopeError,
    export_markdown,
    resolve_root,
    sanitize_name,
)

__all__ = [
    "UNCATEGORIZED",
    "ExportPathError",
    "ExportResult",
    "ExportScopeError",
    "export_markdown",
    "resolve_root",
    "sanitize_name",
]
