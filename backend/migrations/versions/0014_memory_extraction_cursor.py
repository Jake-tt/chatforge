"""memory extraction cursor

Expand only: a column with a server default the previous revision never reads.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("memory_extracted_through_seq", sa.Integer(),
                                   server_default="0", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("memory_extracted_through_seq")
