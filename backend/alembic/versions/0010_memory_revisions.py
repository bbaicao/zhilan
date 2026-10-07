"""U6：记忆版本、冲突、有效时间与来源摘要；不修改既有事实正文。

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-04
"""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_COLUMNS = (
    sa.Column("fact_key", sa.String(200), nullable=True),
    sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    sa.Column("valid_from", sa.DateTime(), nullable=True),
    sa.Column("valid_until", sa.DateTime(), nullable=True),
    sa.Column("supersedes_id", sa.Integer(), nullable=True),
    sa.Column("source_hash", sa.String(64), nullable=True),
)


def upgrade() -> None:
    # ponytail: MySQL DDL 可部分提交；已存在列/约束跳过，允许失败后原地重试。
    connection = op.get_bind()
    existing = {col["name"] for col in sa.inspect(connection).get_columns("memories")}
    with op.batch_alter_table("memories") as batch:
        for column in _COLUMNS:
            if column.name not in existing:
                batch.add_column(sa.Column(
                    column.name, column.type, nullable=column.nullable,
                    server_default=column.server_default.arg if column.server_default is not None else None,
                ))
    constraints = {
        item["name"]: item["column_names"]
        for item in sa.inspect(connection).get_unique_constraints("memories")
    }
    with op.batch_alter_table("memories") as batch:
        if constraints.get("uk_memories_scope_hash") != ["scope", "text_hash", "version"]:
            if "uk_memories_scope_hash" in constraints:
                batch.drop_constraint("uk_memories_scope_hash", type_="unique")
            batch.create_unique_constraint("uk_memories_scope_hash", ["scope", "text_hash", "version"])
        if "uk_memories_scope_fact_version" not in constraints:
            batch.create_unique_constraint("uk_memories_scope_fact_version", ["scope", "fact_key", "version"])


def downgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(sa.text(
        "SELECT 1 FROM memories GROUP BY scope, text_hash HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicate:
        raise RuntimeError("Memory revision history cannot fit 0009 uniqueness; export history before downgrade")
    with op.batch_alter_table("memories") as batch:
        batch.drop_constraint("uk_memories_scope_fact_version", type_="unique")
        batch.drop_constraint("uk_memories_scope_hash", type_="unique")
        batch.create_unique_constraint("uk_memories_scope_hash", ["scope", "text_hash"])
        for column in reversed(_COLUMNS):
            batch.drop_column(column.name)
