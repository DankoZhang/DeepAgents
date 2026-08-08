"""add tool_definition.requires_hitl

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-08-08 13:30:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e0f1a2b3c4d5"
down_revision: Union[str, Sequence[str], None] = "d9e0f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tool_definition") as batch_op:
        batch_op.add_column(
            sa.Column(
                "requires_hitl",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    # 已有危险内置工具默认开启 HITL（与种子一致）
    op.execute(
        sa.text(
            "UPDATE tool_definition SET requires_hitl = true "
            "WHERE name IN ('run_shell_command', 'write_workspace_file')"
        )
    )
    with op.batch_alter_table("tool_definition") as batch_op:
        batch_op.alter_column("requires_hitl", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("tool_definition") as batch_op:
        batch_op.drop_column("requires_hitl")
