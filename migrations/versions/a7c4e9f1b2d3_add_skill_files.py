#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   a7c4e9f1b2d3_add_skill_files.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   a7c4e9f1b2d3_add_skill_files.py

add skill_definition.files

Revision ID: a7c4e9f1b2d3
Revises: d3a8f1c2b4e6
Create Date: 2026-08-16 15:53:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a7c4e9f1b2d3"
down_revision: Union[str, Sequence[str], None] = "d3a8f1c2b4e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Skill 目录包附属文件：相对路径 → 正文。"""
    files_type = sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()), "postgresql"
    )
    op.add_column(
        "skill_definition",
        sa.Column(
            "files",
            files_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("skill_definition", schema=None) as batch_op:
        batch_op.drop_column("files")
