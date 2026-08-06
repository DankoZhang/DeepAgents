"""indexes + timestamps for reverse lookups

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-08-06 23:40:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("methodology_agent") as batch_op:
        batch_op.create_index("ix_methodology_agent_agent_id", ["agent_id"])

    with op.batch_alter_table("agent_tool") as batch_op:
        batch_op.create_index("ix_agent_tool_tool_id", ["tool_id"])

    with op.batch_alter_table("agent_skill") as batch_op:
        batch_op.create_index("ix_agent_skill_skill_id", ["skill_id"])

    with op.batch_alter_table("agent_middleware") as batch_op:
        batch_op.create_index("ix_agent_middleware_middleware_id", ["middleware_id"])

    with op.batch_alter_table("conversation") as batch_op:
        batch_op.create_index("ix_conversation_methodology_id", ["methodology_id"])

    # 统一时间戳字段（存量行用当前 UTC 回填）
    now = sa.text("CURRENT_TIMESTAMP")
    for table in ("agent_definition", "tool_definition", "middleware_definition"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "created_time",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=now,
                )
            )
            batch_op.add_column(
                sa.Column(
                    "updated_time",
                    sa.DateTime(timezone=True),
                    nullable=False,
                    server_default=now,
                )
            )


def downgrade() -> None:
    for table in ("agent_definition", "tool_definition", "middleware_definition"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("updated_time")
            batch_op.drop_column("created_time")

    with op.batch_alter_table("conversation") as batch_op:
        batch_op.drop_index("ix_conversation_methodology_id")

    with op.batch_alter_table("agent_middleware") as batch_op:
        batch_op.drop_index("ix_agent_middleware_middleware_id")

    with op.batch_alter_table("agent_skill") as batch_op:
        batch_op.drop_index("ix_agent_skill_skill_id")

    with op.batch_alter_table("agent_tool") as batch_op:
        batch_op.drop_index("ix_agent_tool_tool_id")

    with op.batch_alter_table("methodology_agent") as batch_op:
        batch_op.drop_index("ix_methodology_agent_agent_id")
