"""conversation recap

Expand only: nullable columns and one with a server default. The previous revision never
reads them, so it keeps serving while the migration job runs.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-06
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("recap", sa.Text(), nullable=True))
        batch.add_column(sa.Column("recap_questions", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("recap_through_seq", sa.Integer(), server_default="0",
                                   nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("recap_through_seq")
        batch.drop_column("recap_questions")
        batch.drop_column("recap")
