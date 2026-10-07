"""0001 空初始迁移：占位验证迁移链可跑。

Revision ID: 0001
Revises:
Create Date: 2026-09-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """阶段 1：空 op，不建业务表。"""
    pass


def downgrade() -> None:
    """阶段 1：空 op，无表可删。"""
    pass
