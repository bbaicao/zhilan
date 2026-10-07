"""阶段 2 · 条目 2.1：documents 加 file_path / index_error 两列。

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-17

两列均为**内部列**，不进任何对外响应体：

- ``file_path``：上传文件留档的绝对路径（文件夹导入记原文件绝对路径）；
  是「失败可重试」的前提（解析/切块失败后能直接读留档重跑）。
- ``index_error``：入库失败的可读中文原因，落列供列表接口回填 tooltip。

风格照 0003：只 ``add_column``，downgrade 用 ``drop_column``。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("file_path", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("index_error", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "index_error")
    op.drop_column("documents", "file_path")
