"""drop model_definition.context_length

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-08-07 17:50:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, Sequence[str], None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("model_definition") as batch_op:
        batch_op.drop_column("context_length")


def downgrade() -> None:
    with op.batch_alter_table("model_definition") as batch_op:
        batch_op.add_column(sa.Column("context_length", sa.Integer(), nullable=True))
