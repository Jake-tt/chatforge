"""user token version and deletion mark

Expand only: a column with a server default and a nullable one. The previous revision
never reads them, so it keeps serving while the migration job runs. Its tokens carry no
version claim, which the new code reads as version 0, the default here.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-29
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("token_version", sa.Integer(), server_default="0",
                                   nullable=False))
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("deleted_at")
        batch.drop_column("token_version")
