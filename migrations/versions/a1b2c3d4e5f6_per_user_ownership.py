"""per-user ownership isolation

Revision ID: a1b2c3d4e5f6
Revises: 2cc5396af21a
Create Date: 2026-08-06 16:00:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "2cc5396af21a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEGACY_OWNER = "__legacy__"


def _drop_named_unique_on(batch_op, table_name: str, columns: set[str]) -> None:
    """删除仅覆盖给定列的唯一约束/唯一索引（Postgres 上二者常同名，勿重复 DROP）。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dropped: set[str] = set()
    for uc in inspector.get_unique_constraints(table_name):
        if set(uc.get("column_names") or []) != columns:
            continue
        name = uc.get("name")
        if name:
            batch_op.drop_constraint(name, type_="unique")
            dropped.add(name)
    for ix in inspector.get_indexes(table_name):
        if not ix.get("unique"):
            continue
        if set(ix.get("column_names") or []) != columns:
            continue
        name = ix.get("name")
        if name and name not in dropped:
            batch_op.drop_index(name)


def _sqlite_has_solo_name_unique(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    for uc in inspector.get_unique_constraints(table_name):
        if set(uc.get("column_names") or []) == {"name"}:
            return True
    for ix in inspector.get_indexes(table_name):
        if ix.get("unique") and list(ix.get("column_names") or []) == ["name"]:
            return True
    return False


def _sqlite_rebuild_owner_unique(table_name: str, uq_name: str) -> None:
    """
    SQLite 无法直接删除未命名 UNIQUE(name)；整表重建为
    UNIQUE(owner_user_id, name)。
    """
    if not _sqlite_has_solo_name_unique(table_name):
        return
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = inspector.get_columns(table_name)
    col_names = [c["name"] for c in cols]
    tmp = f"_{table_name}_owner_mig"

    # 用当前列定义建临时表（去掉仅 name 的唯一，加上复合唯一）
    meta = sa.MetaData()
    new_cols = []
    for c in cols:
        kwargs = {
            "nullable": c.get("nullable", True),
            "primary_key": c.get("name") == "id",
        }
        new_cols.append(sa.Column(c["name"], c["type"], **kwargs))
    sa.Table(
        tmp,
        meta,
        *new_cols,
        sa.UniqueConstraint("owner_user_id", "name", name=uq_name),
    )
    meta.create_all(bind, tables=[meta.tables[tmp]])

    cols_csv = ", ".join(col_names)
    op.execute(sa.text(f"INSERT INTO {tmp} ({cols_csv}) SELECT {cols_csv} FROM {table_name}"))
    op.drop_table(table_name)
    op.rename_table(tmp, table_name)
    op.create_index(f"ix_{table_name}_owner_user_id", table_name, ["owner_user_id"])


def upgrade() -> None:
    tables = (
        "model_definition",
        "methodology",
        "agent_definition",
        "tool_definition",
        "skill_definition",
        "middleware_definition",
    )
    for table in tables:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column("owner_user_id", sa.String(length=128), nullable=True)
            )
        op.execute(
            sa.text(
                f"UPDATE {table} SET owner_user_id = :owner WHERE owner_user_id IS NULL"
            ).bindparams(owner=_LEGACY_OWNER)
        )

    op.execute(
        sa.text(
            "UPDATE conversation SET user_id = :owner "
            "WHERE user_id IS NULL OR user_id = ''"
        ).bindparams(owner=_LEGACY_OWNER)
    )

    specs = [
        ("model_definition", "uq_model_name", "uq_model_owner_name"),
        ("methodology", None, "uq_methodology_owner_name"),
        ("agent_definition", "uq_agent_name", "uq_agent_owner_name"),
        ("tool_definition", None, "uq_tool_owner_name"),
        ("skill_definition", "uq_skill_name", "uq_skill_owner_name"),
        ("middleware_definition", None, "uq_middleware_owner_name"),
    ]

    dialect = op.get_bind().dialect.name
    for table, _old_uq, new_uq in specs:
        with op.batch_alter_table(table) as batch_op:
            _drop_named_unique_on(batch_op, table, {"name"})
            batch_op.alter_column(
                "owner_user_id",
                existing_type=sa.String(length=128),
                nullable=False,
            )
            # Postgres / 已命名约束路径：直接建复合唯一 + 索引
            if dialect != "sqlite":
                batch_op.create_unique_constraint(new_uq, ["owner_user_id", "name"])
                batch_op.create_index(f"ix_{table}_owner_user_id", ["owner_user_id"])

        if dialect == "sqlite":
            # 先尝试建复合唯一（若旧 UNIQUE(name) 仍在，后续 rebuild 会清掉）
            try:
                with op.batch_alter_table(table) as batch_op:
                    batch_op.create_unique_constraint(
                        new_uq, ["owner_user_id", "name"]
                    )
                    batch_op.create_index(
                        f"ix_{table}_owner_user_id", ["owner_user_id"]
                    )
            except Exception:
                pass
            _sqlite_rebuild_owner_unique(table, new_uq)

    with op.batch_alter_table("conversation") as batch_op:
        batch_op.alter_column(
            "user_id", existing_type=sa.String(length=128), nullable=False
        )
        batch_op.create_index("ix_conversation_user_id", ["user_id"])


def downgrade() -> None:
    with op.batch_alter_table("conversation") as batch_op:
        batch_op.drop_index("ix_conversation_user_id")
        batch_op.alter_column(
            "user_id", existing_type=sa.String(length=128), nullable=True
        )

    for table, old_uq, new_uq in [
        ("middleware_definition", "uq_middleware_name", "uq_middleware_owner_name"),
        ("skill_definition", "uq_skill_name", "uq_skill_owner_name"),
        ("tool_definition", "uq_tool_name", "uq_tool_owner_name"),
        ("agent_definition", "uq_agent_name", "uq_agent_owner_name"),
        ("methodology", None, "uq_methodology_owner_name"),
        ("model_definition", "uq_model_name", "uq_model_owner_name"),
    ]:
        with op.batch_alter_table(table) as batch_op:
            try:
                batch_op.drop_index(f"ix_{table}_owner_user_id")
            except Exception:
                pass
            try:
                batch_op.drop_constraint(new_uq, type_="unique")
            except Exception:
                pass
            if old_uq:
                batch_op.create_unique_constraint(old_uq, ["name"])
            batch_op.drop_column("owner_user_id")
