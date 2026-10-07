"""v2 业务表初始迁移：一次建齐老仓库 0001–0020 终态 schema。

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-17

在 0001 空迁移之后建立全部业务表（结构对齐老仓库 models + alembic 终态）：

- notes / documents / chunks / chats / messages：核心五表（含软删、清洗、压缩等列）
- knowledge_bases / session_groups / wiki_links / usage_events / session_events
- 回收站无独立表（notes/documents/chats.deleted_at 软删）；设置项在 .env

downgrade 按外键依赖反序 drop，可干净回到 0001 空库。
knowledge_bases 播种 id=1「本机知识库」（与老仓库 0005 同口径），供应用默认引用。

**方言无关化（2026-09-18 · 阶段 3 条目 3.0 / B5）**：原写法 ``mysql.LONGTEXT()`` / ``mysql.JSON()``
在 SQLite 上**无法编译**（建表直接失败）。已改为
``sa.Text().with_variant(mysql.LONGTEXT(), "mysql")`` / ``sa.JSON()``，
并把 ``session_events.id`` 写成 ``BigInteger().with_variant(Integer(), "sqlite")``
（SQLite 只认 ``INTEGER PRIMARY KEY`` 为 rowid 别名，否则插入不自动赋值）。
**MySQL 侧最终 DDL 逐字不变**（实测列类型仍是 longtext / json / bigint）；表选项
``mysql_charset/collate/engine`` 保留——非 MySQL 方言下由 SQLAlchemy 忽略。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_ARGS = dict(
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
    mysql_engine="InnoDB",
)


def upgrade() -> None:
    # --- notes ---
    op.create_table(
        "notes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
        sa.Column("category", sa.String(length=255), nullable=False),
        sa.Column("kb_id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("source_document_id", sa.Integer(), nullable=True),
        sa.Column("generation_meta", sa.JSON(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_ARGS,
    )
    op.create_index("ix_notes_category", "notes", ["category"])
    op.create_index("ix_notes_kb_id", "notes", ["kb_id"])
    op.create_index("ix_notes_source_document_id", "notes", ["source_document_id"])

    # --- documents ---
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("vectorized", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("vector_error", sa.String(length=255), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("raw_content", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=True),
        sa.Column("clean_report", sa.JSON(), nullable=True),
        sa.Column("kb_id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("category", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_hash", name="uk_documents_content_hash"),
        **TABLE_ARGS,
    )
    op.create_index("ix_documents_status", "documents", ["status"])
    op.create_index("ix_documents_filename", "documents", ["filename"])
    op.create_index("ix_documents_kb_id", "documents", ["kb_id"])
    op.create_index("ix_documents_category", "documents", ["category"])

    # --- chunks ---
    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "ordinal", name="uk_chunks_document_ordinal"),
        **TABLE_ARGS,
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])

    # --- chats ---
    op.create_table(
        "chats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("clarify_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("context_summary", sa.Text(), nullable=True),
        sa.Column("compressed_up_to", sa.Integer(), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_ARGS,
    )

    # --- messages ---
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
        sa.Column("refs", sa.JSON(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("filters", sa.JSON(), nullable=True),
        sa.Column("reasoning", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=True),
        sa.Column("thinking", sa.JSON(), nullable=True),
        sa.Column("skill_ids", sa.JSON(), nullable=True),
        sa.Column("agent_trace", sa.JSON(), nullable=True),
        sa.Column("images", sa.JSON(), nullable=True),
        sa.Column("answer_source", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_ARGS,
    )
    op.create_index("ix_messages_chat_id", "messages", ["chat_id"])

    # --- session_groups（老仓库条目 40 / 迁移 0015）---
    op.create_table(
        "session_groups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("sort", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_ARGS,
    )

    # --- knowledge_bases（老仓库 0005）---
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("path_or_url", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("collection", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("read_only", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ready"),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_ARGS,
    )
    bind = op.get_bind()
    existing = bind.execute(sa.text("SELECT id FROM knowledge_bases WHERE id = 1")).fetchone()
    if existing is None:
        bind.execute(
            sa.text(
                "INSERT INTO knowledge_bases (id, name, kind, path_or_url, collection, read_only, status) "
                "VALUES (1, '本机知识库', 'local', '', 'zhilan_knowledge', 0, 'ready')"
            )
        )

    # --- wiki_links（老仓库 0008）---
    op.create_table(
        "wiki_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("kb_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("target_title", sa.String(length=255), nullable=False),
        sa.Column("target_note_id", sa.Integer(), nullable=True),
        sa.Column("target_document_id", sa.Integer(), nullable=True),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_type", "source_id", "target_title", name="uk_wiki_links_source_target"),
        **TABLE_ARGS,
    )
    op.create_index("ix_wiki_links_target_note_id", "wiki_links", ["target_note_id"])
    op.create_index("ix_wiki_links_target_document_id", "wiki_links", ["target_document_id"])
    op.create_index("ix_wiki_links_source", "wiki_links", ["source_type", "source_id"])

    # --- usage_events（老仓库 0016）---
    op.create_table(
        "usage_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("calls", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        **TABLE_ARGS,
    )
    op.create_index("ix_usage_events_created_at", "usage_events", ["created_at"])

    # --- session_events（老仓库 0018 + 0020）---
    op.create_table(
        "session_events",
        # SQLite 只把 ``INTEGER PRIMARY KEY`` 当 rowid 别名（自动赋值）；``BIGINT PRIMARY KEY``
        # 是普通列 ⇒ 插入不自动填值、NOT NULL 违约。故 SQLite 侧退化为 INTEGER（仍是 64 位），
        # MySQL 侧保持 BIGINT。同一口径见 ``app/models/__init__.py::_big_int_pk``。
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=True),
        sa.Column("step", sa.Integer(), nullable=True),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("trace", sa.String(length=64), nullable=True),
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
        sa.Column("attempt_id", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "seq", name="uk_session_events_chat_seq"),
        sa.UniqueConstraint("chat_id", "request_id", name="uk_session_events_chat_request_id"),
        sa.ForeignKeyConstraint(
            ["chat_id"], ["chats.id"], name="fk_session_events_chat_id", ondelete="CASCADE"
        ),
        **TABLE_ARGS,
    )
    op.create_index("ix_session_events_chat_id_seq", "session_events", ["chat_id", "seq"])
    op.create_index("ix_session_events_chat_type", "session_events", ["chat_id", "type"])
    op.create_index("ix_session_events_trace", "session_events", ["trace"])
    op.create_index("ix_session_events_tool_call", "session_events", ["chat_id", "tool_call_id"])
    op.create_index("ix_session_events_attempt", "session_events", ["chat_id", "attempt_id"])


def downgrade() -> None:
    # 先删带外键的子表，再删父表；表级删除会一并清掉索引。
    # 不能单独 drop 外键依赖的索引（MySQL 1553，老仓库 0001 已踩坑）。
    op.drop_table("session_events")
    op.drop_table("usage_events")
    op.drop_table("wiki_links")
    op.drop_table("knowledge_bases")
    op.drop_table("session_groups")
    op.drop_table("messages")
    op.drop_table("chats")
    op.drop_table("chunks")
    op.drop_table("documents")
    op.drop_table("notes")
