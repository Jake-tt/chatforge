"""message kind and clarify options

Expand only: a column with a server default and a nullable one. The previous revision
never reads them, so it keeps serving while the migration job runs; the replies it stores
get the default kind, "answer", which is what they are.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-06
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("kind", sa.String(length=16), server_default="answer",
                                   nullable=False))
        batch.add_column(sa.Column("options", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("options")
        batch.drop_column("kind")
