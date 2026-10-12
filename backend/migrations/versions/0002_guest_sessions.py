"""guest sessions: conversations can be owned by an anonymous guest

A conversation is now owned by exactly one of (user_id, guest_id). Existing rows all have
user_id set, so the new check constraint holds without a data backfill.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "guests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("claimed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["claimed_by_user_id"], ["users.id"], ondelete="SET NULL",
                                name="fk_guests_claimed_by_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_guests"),
    )
    with op.batch_alter_table("conversations") as batch:
        batch.alter_column("user_id", existing_type=sa.Uuid(), nullable=True)
        batch.add_column(sa.Column("guest_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key("fk_conversations_guest_id_guests", "guests",
                                 ["guest_id"], ["id"], ondelete="CASCADE")
        batch.create_check_constraint(op.f("ck_conversations_single_owner"),
                                      "(user_id IS NULL) <> (guest_id IS NULL)")
        batch.create_index("ix_conversations_guest_id_updated_at", ["guest_id", "updated_at"])


def downgrade() -> None:
    # Guest-owned conversations have no member to fall back to; they are dropped.
    op.execute("DELETE FROM conversations WHERE user_id IS NULL")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_index("ix_conversations_guest_id_updated_at")
        batch.drop_constraint(op.f("ck_conversations_single_owner"), type_="check")
        batch.drop_constraint("fk_conversations_guest_id_guests", type_="foreignkey")
        batch.drop_column("guest_id")
        batch.alter_column("user_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_table("guests")
