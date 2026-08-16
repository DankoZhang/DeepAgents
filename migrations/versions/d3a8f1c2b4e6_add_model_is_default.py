#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   d3a8f1c2b4e6_add_model_is_default.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   d3a8f1c2b4e6_add_model_is_default.py

add model is_default

Revision ID: d3a8f1c2b4e6
Revises: c960f621dfd0
Create Date: 2026-08-13 20:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d3a8f1c2b4e6"
down_revision: Union[str, Sequence[str], None] = "c960f621dfd0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """每个用户至多一个默认模型：加列、回填种子/最早模型、建部分唯一索引。"""
    op.add_column(
        "model_definition",
        sa.Column(
            "is_default",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT owner_user_id, id FROM model_definition "
            "ORDER BY CASE WHEN id LIKE 'model_default__%' THEN 0 ELSE 1 END, "
            "created_time, id"
        )
    ).fetchall()
    seen: set[str] = set()
    for owner_user_id, model_id in rows:
        if owner_user_id in seen:
            continue
        seen.add(owner_user_id)
        bind.execute(
            sa.text("UPDATE model_definition SET is_default = :flag WHERE id = :id"),
            {"flag": True, "id": model_id},
        )

    dialect = bind.dialect.name
    if dialect == "sqlite":
        op.execute(
            "CREATE UNIQUE INDEX uq_model_one_default_per_owner "
            "ON model_definition (owner_user_id) WHERE is_default = 1"
        )
    else:
        op.execute(
            "CREATE UNIQUE INDEX uq_model_one_default_per_owner "
            "ON model_definition (owner_user_id) WHERE is_default IS TRUE"
        )


def downgrade() -> None:
    op.drop_index("uq_model_one_default_per_owner", table_name="model_definition")
    with op.batch_alter_table("model_definition", schema=None) as batch_op:
        batch_op.drop_column("is_default")
