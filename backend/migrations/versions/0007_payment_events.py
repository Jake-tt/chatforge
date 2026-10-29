"""payment events: idempotent payment webhooks

Expand only: a new table nothing in the previous revision reads.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-29
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payment_events",
        sa.Column("id", sa.String(128), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_events")),
    )
    op.create_index("ix_payment_events_user_id_occurred_at", "payment_events",
                    ["user_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_payment_events_user_id_occurred_at", table_name="payment_events")
    op.drop_table("payment_events")
