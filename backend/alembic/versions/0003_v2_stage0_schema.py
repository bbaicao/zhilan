"""阶段 0.2：补齐 v2 新 schema（index_status / source_type / 归因与 Agent 表）。

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-16

在 0002 业务表之上增量演进（不要求与老库同构）：

- documents.index_status（pending/processing/ready/failed，与 status 并存）
- chunks.page / start_offset / end_offset
- messages.source_type（internal_db / external）
- 新表 message_attributions / agent_runs / agent_steps

downgrade 按外键依赖反序 drop 新表；列删除用 drop_column。

**方言无关化（2026-09-18 · 阶段 3 条目 3.0 / B5）**：三处 ``mysql.JSON()`` 改为 ``sa.JSON()``
（MySQL 侧仍渲染 ``json``，SQLite 侧可建表）；表选项 ``mysql_charset/collate/engine`` 保留，
非 MySQL 方言下由 SQLAlchemy 忽略。本迁移的 ``upgrade``/``downgrade`` 只用
``add_column`` / ``drop_column`` / ``create_table`` / ``create_index`` —— SQLite 3.35+ 原生支持。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_ARGS = dict(
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
    mysql_engine="InnoDB",
)


def upgrade() -> None:
    # --- documents.index_status ---
    op.add_column(
        "documents",
        sa.Column("index_status", sa.String(length=32), nullable=False, server_default="pending"),
    )
    op.create_index("ix_documents_index_status", "documents", ["index_status"])

    # --- chunks page/offset ---
    op.add_column("chunks", sa.Column("page", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("start_offset", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("end_offset", sa.Integer(), nullable=True))

    # --- messages.source_type ---
    op.add_column("messages", sa.Column("source_type", sa.String(length=16), nullable=True))
    op.create_index("ix_messages_source_type", "messages", ["source_type"])

    # --- message_attributions ---
    op.create_table(
        "message_attributions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", "ordinal", name="uk_message_attributions_message_ordinal"),
        **TABLE_ARGS,
    )
    op.create_index("ix_message_attributions_message_id", "message_attributions", ["message_id"])
    op.create_index(
        "ix_message_attributions_message_ordinal", "message_attributions", ["message_id", "ordinal"]
    )
    op.create_index("ix_message_attributions_chunk_id", "message_attributions", ["chunk_id"])

    # --- agent_runs ---
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False, server_default="off"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_ARGS,
    )
    op.create_index("ix_agent_runs_chat_id", "agent_runs", ["chat_id"])
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"])

    # --- agent_steps ---
    op.create_table(
        "agent_steps",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=True),
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_ARGS,
    )
    op.create_index("ix_agent_steps_run_id", "agent_steps", ["run_id"])
    op.create_index("ix_agent_steps_run_step", "agent_steps", ["run_id", "step_no"])
    op.create_index("ix_agent_steps_tool_call", "agent_steps", ["tool_call_id"])


def downgrade() -> None:
    # 反序 drop：先子表再父表；不要单独 drop 支撑 FK 的索引
    op.drop_table("agent_steps")
    op.drop_table("agent_runs")
    op.drop_table("message_attributions")

    op.drop_index("ix_messages_source_type", table_name="messages")
    op.drop_column("messages", "source_type")

    op.drop_column("chunks", "end_offset")
    op.drop_column("chunks", "start_offset")
    op.drop_column("chunks", "page")

    op.drop_index("ix_documents_index_status", table_name="documents")
    op.drop_column("documents", "index_status")
