"""attachments converted in the background

Expand only. New columns either have a server default (`status`, "ready": every existing
row was converted at upload) or are nullable, and `markdown`/`sha256` become nullable for
rows still being converted. The previous revision always fills both, never reads the new
columns, and only ever sees "ready" rows. Existing rows get `source_sha256` = `sha256`
(for documents they were the same hash).

Revision ID: 0012
Revises: 0010
Create Date: 2026-11-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("attachments") as batch:
        batch.add_column(sa.Column("status", sa.String(length=16), server_default="ready",
                                   nullable=False))
        batch.add_column(sa.Column("error_code", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("pending_data", sa.LargeBinary(), nullable=True))
        batch.add_column(sa.Column("source_sha256", sa.String(length=64), nullable=True))
        batch.alter_column("sha256", existing_type=sa.String(length=64), nullable=True)
        batch.alter_column("markdown", existing_type=sa.Text(), nullable=True)
        batch.create_index("ix_attachments_source_sha256", ["source_sha256"])
    op.execute("UPDATE attachments SET source_sha256 = sha256")


def downgrade() -> None:
    # Rows still converting have no Markdown; they can't survive the NOT NULL.
    op.execute("DELETE FROM attachments WHERE status <> 'ready'")
    with op.batch_alter_table("attachments") as batch:
        batch.drop_index("ix_attachments_source_sha256")
        batch.alter_column("markdown", existing_type=sa.Text(), nullable=False)
        batch.alter_column("sha256", existing_type=sa.String(length=64), nullable=False)
        batch.drop_column("source_sha256")
        batch.drop_column("pending_data")
        batch.drop_column("error_code")
        batch.drop_column("status")
