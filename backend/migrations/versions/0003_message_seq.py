"""message sequence numbers: per-conversation seq + message_count

Messages were ordered by created_at, which can tie and says nothing about gaps. Each message
now gets a 1-based `seq` within its conversation, allocated by atomically incrementing
`conversations.message_count`.

Expand → backfill → contract, so it is safe while the previous revision is still serving:
1. add both columns as nullable / defaulted (old code keeps working; it ignores them)
2. backfill seq with ROW_NUMBER() in created_at order, and message_count from it
3. make seq NOT NULL + unique per conversation; drop the old created_at index

Caveat: rows inserted by the old revision between steps 2 and 3 would have NULL seq and
fail step 3. The migration job runs before traffic shifts and takes seconds, but a busy
system would split step 3 into a follow-up migration released after the new code.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.add_column(sa.Column("message_count", sa.Integer(), server_default="0",
                                   nullable=False))
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("seq", sa.Integer(), nullable=True))

    # Window functions work on both Postgres and SQLite (3.25+).
    op.execute("""
        UPDATE messages SET seq = (
            SELECT ranked.rn FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY conversation_id ORDER BY created_at, id
                ) AS rn
                FROM messages
            ) AS ranked
            WHERE ranked.id = messages.id
        )
    """)
    op.execute("""
        UPDATE conversations SET message_count = COALESCE(
            (SELECT MAX(seq) FROM messages WHERE messages.conversation_id = conversations.id), 0
        )
    """)

    with op.batch_alter_table("messages") as batch:
        batch.alter_column("seq", existing_type=sa.Integer(), nullable=False)
        batch.create_unique_constraint("uq_messages_conversation_id_seq",
                                       ["conversation_id", "seq"])
        batch.drop_index("ix_messages_conversation_id_created_at")


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.create_index("ix_messages_conversation_id_created_at",
                           ["conversation_id", "created_at"])
        batch.drop_constraint("uq_messages_conversation_id_seq", type_="unique")
        batch.drop_column("seq")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("message_count")
