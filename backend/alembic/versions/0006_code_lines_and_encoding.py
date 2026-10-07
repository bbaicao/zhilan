"""工具升级-3 · 回归检查：代码块的行号/符号路径 + 文档级编解码名。

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-25

本支迁移**一次建齐两组列**（包内 D-回归检查 的归属裁定：0006 由 回归检查 那一笔建齐）：

1. chunks.line_start / line_end / symbol_path —— 代码块可回溯到源文件行号（D-回归检查 取 (a) 加列）。
   三列可空：**只有 chunk_code() 填写**，chunk_document() 产出的块一律 NULL
   （文档块没有「源文件行号」这个概念）。
   ⚠️ 不得挪用既有的 start_offset：那是**归一化全文字符偏移**，与行号不是同一坐标系（K-回归检查）。
   ⚠️ heading_path **不是列**（它是 ChunkMeta 的 dataclass 字段，pipeline/ingest.py 从不搬它）
   ⇒ symbol_path 必须自成一列，不能指望「顺便进库」（K-回归检查）。

2. documents.source_encoding —— 入库时探测到的编解码名（D-回归检查 落位裁定：**只挂 Document**）。
   理由：编码是**文档级**属性（一个文件一种编码）；挂 Chunk 会产生「同一文档 N 个块各存一份相同编码」的冗余。
   String(32) 可空（历史行 / 未探测到时为 NULL）。
   ⚠️ **本列只做「存下来」**：写回（按原编码/原 BOM/原行尾写回）在**阶段 5**，不在本阶段。
   ⇒ 本阶段的判据只到「**A8 前置腿**」——测试直接 select(Document.source_encoding) 读得到探测名。

两列组都**不建索引**：行号列只作展示与回溯，不参与过滤/排序；编解码名同理。

风格照 0004_documents_file_path.py：只 add_column，downgrade 用 drop_column（反序）。
⚠️ downgrade 是破坏性的（丢这两组列的数据），但**不影响任何既有列**——
chunks 的内容/ordinal 与 documents 的其余字段都在。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("line_start", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("line_end", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("symbol_path", sa.String(length=255), nullable=True))
    op.add_column("documents", sa.Column("source_encoding", sa.String(length=32), nullable=True))


def downgrade() -> None:
    # 反序（与 0004 同一写法）
    op.drop_column("documents", "source_encoding")
    op.drop_column("chunks", "symbol_path")
    op.drop_column("chunks", "line_end")
    op.drop_column("chunks", "line_start")
