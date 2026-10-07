"""阶段 5 · 条目 5.1：跨会话记忆表 ``memories``。

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-19

新表一张，列与索引**与 ``app/models/__init__.py::Memory`` 逐列对齐**（建表走
``Base.metadata.create_all`` 的测试路径与此迁移必须产出同一结构，否则
``tests/test_models_schema.py`` 的全等断言与真库会分叉）。

三条要点：

- **复合唯一** ``uk_memories_scope_hash (scope, text_hash)`` 是去重的唯一依据
  （**不是** ``text_hash`` 单列——那会把将来不同作用域的同一句话误判为重复）；
- ``text`` 用 ``LONGTEXT``：与模型侧 ``_long_text()`` 的 MySQL 渲染一致
  （该辅助在 SQLite 退化为 ``TEXT``；本迁移的 SQLite 路径由 alembic 的
  ``render_as_batch`` 承接，类型在两种方言下都能建）；
- **``downgrade`` 是破坏性的**：整张表被 drop ⇒ **全部记忆丢失**。这是**已登记的
  可接受代价**（本机数据、非用户资产、不做备份），但不要把它当成「无损回滚」——
  如需保留记忆，必须在执行降级前自行备份该表。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_ARGS = dict(
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
    mysql_engine="InnoDB",
)


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(length=32), server_default="global", nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("source_chat_id", sa.Integer(), nullable=True),
        sa.Column("source_message_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "text_hash", name="uk_memories_scope_hash"),
        **TABLE_ARGS,
    )
    op.create_index("ix_memories_created_at", "memories", ["created_at"])


def downgrade() -> None:
    # 反序：先 drop 索引再 drop 表（与 0003 的 downgrade 同一写法）
    op.drop_index("ix_memories_created_at", table_name="memories")
    op.drop_table("memories")
