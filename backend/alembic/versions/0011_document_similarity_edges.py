"""导入资料自动连线：新增 document_similarity_edges（同库两文档的相似边）。

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-08
"""

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_TABLE = "document_similarity_edges"
_UNIQUE = "uk_document_similarity_edges_triple"
_INDEX_SOURCE = "ix_document_similarity_edges_source"
_INDEX_TARGET = "ix_document_similarity_edges_target"


def upgrade() -> None:
    # ponytail: MySQL DDL 可部分提交；表已存在就跳过，允许失败后原地重试。
    connection = op.get_bind()
    if _TABLE in sa.inspect(connection).get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kb_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_document_id", sa.Integer(), nullable=False),
        sa.Column("target_document_id", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("space_id", sa.String(64), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "kb_id", "source_document_id", "target_document_id", name=_UNIQUE,
        ),
    )
    op.create_index(_INDEX_SOURCE, _TABLE, ["source_document_id"])
    op.create_index(_INDEX_TARGET, _TABLE, ["target_document_id"])


def downgrade() -> None:
    op.drop_index(_INDEX_TARGET, table_name=_TABLE)
    op.drop_index(_INDEX_SOURCE, table_name=_TABLE)
    op.drop_table(_TABLE)
