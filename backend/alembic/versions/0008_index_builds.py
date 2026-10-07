"""U2：索引构建与正文版本，保留旧正文、分块和引用。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-04

历史资料保持 NULL 构建身份；升级不回填正文或移动引用。
SQLite 旧 UNIQUE 约束只能经重建 chunks 撤销：在事务外关闭外键后，
以 BEGIN IMMEDIATE 包住全部 DDL，完成前检查外键、失败则回滚，再恢复原设置。
MySQL 使用原生 ALTER；DDL 不构成可共同回滚事务，失败时须停写核查结构。

downgrade 仅支持空构建记录及 NULL 身份的旧资料。任何已生成的构建、
版本化分块或启用指针都会在任何 DDL 前拒绝，避免自动删除历史版本或引用。
"""

from contextlib import contextmanager
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BUILD_INDEX = "uk_chunks_document_build_ordinal"
LEGACY_CONSTRAINT = "uk_chunks_document_ordinal"


@contextmanager
def _ddl_scope():
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        yield
        return

    # ponytail: 只在 SQLite 的结构维护窗口关闭 FK；外层迁移事务先提交，
    # 单次 BEGIN IMMEDIATE 保证表替换可回滚，避免 DROP chunks 级联删除历史引用。
    with op.get_context().autocommit_block():
        foreign_keys = int(bind.exec_driver_sql("PRAGMA foreign_keys").scalar())
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
        if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
            raise RuntimeError("U2 迁移无法关闭 SQLite 外键检查，已拒绝重建分块表")
        try:
            bind.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield
                if bind.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                    raise RuntimeError("U2 迁移外键完整性检查失败，已回滚结构变更")
                bind.exec_driver_sql("COMMIT")
            except BaseException:
                bind.exec_driver_sql("ROLLBACK")
                raise
        finally:
            bind.exec_driver_sql(f"PRAGMA foreign_keys={foreign_keys}")
            if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != foreign_keys:
                raise RuntimeError("U2 迁移未能恢复 SQLite 外键检查设置")


def upgrade() -> None:
    with _ddl_scope():
        op.add_column("documents", sa.Column("active_build_id", sa.String(36), nullable=True))
        op.add_column("documents", sa.Column("content_build_id", sa.String(36), nullable=True))
        op.create_table(
            "document_builds",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("document_id", sa.Integer(), nullable=False),
            sa.Column("base_build_id", sa.String(36), nullable=True),
            sa.Column("content_build_id", sa.String(36), nullable=True),
            sa.Column("state", sa.String(16), nullable=False, server_default="preparing"),
            sa.Column("fingerprint", sa.JSON().with_variant(mysql.JSON(), "mysql"), nullable=False),
            sa.Column("space_id", sa.String(64), nullable=True),
            sa.Column("point_ids", sa.JSON().with_variant(mysql.JSON(), "mysql"), nullable=False),
            sa.Column("snapshot", sa.JSON().with_variant(mysql.JSON(), "mysql"), nullable=False),
            sa.Column("vectorized", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("error", sa.String(255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
            mysql_charset="utf8mb4",
            mysql_collate="utf8mb4_unicode_ci",
            mysql_engine="InnoDB",
        )
        op.create_index("ix_document_builds_document_id", "document_builds", ["document_id"])
        with op.batch_alter_table("chunks") as batch:
            batch.add_column(sa.Column("build_id", sa.String(36), nullable=True))
            batch.drop_constraint(LEGACY_CONSTRAINT, type_="unique")
        op.create_index(BUILD_INDEX, "chunks", ["document_id", "build_id", "ordinal"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    # 纯 SELECT 预检在任何 DDL 前执行：不能以降版本为名删去已留存的版本/引用。
    for query in (
        "SELECT id FROM document_builds LIMIT 1",
        "SELECT id FROM chunks WHERE build_id IS NOT NULL LIMIT 1",
        "SELECT id FROM documents WHERE active_build_id IS NOT NULL OR content_build_id IS NOT NULL LIMIT 1",
    ):
        if bind.execute(sa.text(query)).first() is not None:
            raise RuntimeError(
                "U2 降级已拒绝（零结构改动）：存在索引构建或版本化资料。"
                "请先在停写窗口恢复、留存并核验版本材料，再执行版本维护；不能自动删除历史版本。"
            )
    if bind.execute(sa.text(
        "SELECT document_id, ordinal FROM chunks GROUP BY document_id, ordinal HAVING COUNT(*) > 1 LIMIT 1"
    )).first() is not None:
        raise RuntimeError("U2 降级已拒绝（零结构改动）：旧分块唯一约束存在冲突，请先执行版本维护")

    with _ddl_scope():
        op.drop_index(BUILD_INDEX, table_name="chunks")
        with op.batch_alter_table("chunks") as batch:
            batch.drop_column("build_id")
            batch.create_unique_constraint(LEGACY_CONSTRAINT, ["document_id", "ordinal"])
        op.drop_index("ix_document_builds_document_id", table_name="document_builds")
        op.drop_table("document_builds")
        op.drop_column("documents", "content_build_id")
        op.drop_column("documents", "active_build_id")
