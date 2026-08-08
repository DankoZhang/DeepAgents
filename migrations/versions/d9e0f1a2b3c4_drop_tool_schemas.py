"""drop tool_definition.input_schema / output_schema

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-08-08 12:35:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d9e0f1a2b3c4"
down_revision: Union[str, Sequence[str], None] = "c8d9e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tool_definition") as batch_op:
        batch_op.drop_column("input_schema")
        batch_op.drop_column("output_schema")


def downgrade() -> None:
    json_type = sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()), "postgresql"
    )
    with op.batch_alter_table("tool_definition") as batch_op:
        batch_op.add_column(sa.Column("input_schema", json_type, nullable=True))
        batch_op.add_column(sa.Column("output_schema", json_type, nullable=True))
