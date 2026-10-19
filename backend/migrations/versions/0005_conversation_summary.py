"""conversation summary: rolling summary of older messages

Expand only: a nullable column and one with a server default. The previous revision never
reads them, so it keeps serving while the migration job runs, and no backfill is needed
(0 means "nothing summarized yet").

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("summary", sa.Text(), nullable=True))
        batch.add_column(sa.Column("summarized_through_seq", sa.Integer(),
                                   server_default="0", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("summarized_through_seq")
        batch.drop_column("summary")
