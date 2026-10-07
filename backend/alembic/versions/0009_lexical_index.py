"""U3：普通 SQL 词项索引与笔记内部片段，批量回填既有内容。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-04

只新增派生表，不修改正文/分块/引用或构建指针。回填可重跑；
MySQL DDL 不保证整笔回滚，checkfirst 允许失败后重试，词项回填仍与事务同行。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from app.models import LexicalMaterial, LexicalTerm
    from app.pipeline.lexical_index import backfill_connection

    connection = op.get_bind()
    LexicalMaterial.__table__.create(connection, checkfirst=True)
    LexicalTerm.__table__.create(connection, checkfirst=True)
    for index in LexicalTerm.__table__.indexes:
        index.create(connection, checkfirst=True)
    backfill_connection(connection)


def downgrade() -> None:
    op.drop_table("lexical_terms")
    op.drop_table("lexical_materials")
