"""user memory graph

Expand only: new tables nothing in the previous revision reads, and a column with a server
default it never reads.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("memory_enabled", sa.Boolean(), server_default=sa.true(),
                                   nullable=False))
    op.create_table(
        "memory_entities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE",
                                name=op.f("fk_memory_entities_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_entities")),
        sa.UniqueConstraint("user_id", "key", name="uq_memory_entities_user_id_key"),
    )
    op.create_table(
        "memory_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("predicate", sa.String(length=100), nullable=False),
        sa.Column("object_id", sa.Uuid(), nullable=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE",
                                name=op.f("fk_memory_facts_user_id_users")),
        sa.ForeignKeyConstraint(["subject_id"], ["memory_entities.id"], ondelete="CASCADE",
                                name=op.f("fk_memory_facts_subject_id_memory_entities")),
        sa.ForeignKeyConstraint(["object_id"], ["memory_entities.id"], ondelete="CASCADE",
                                name=op.f("fk_memory_facts_object_id_memory_entities")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_facts")),
    )
    op.create_index("ix_memory_facts_user_id", "memory_facts", ["user_id"])
    op.create_index("ix_memory_facts_subject_id", "memory_facts", ["subject_id"])
    op.create_table(
        "memory_fact_sources",
        sa.Column("fact_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["fact_id"], ["memory_facts.id"], ondelete="CASCADE",
                                name=op.f("fk_memory_fact_sources_fact_id_memory_facts")),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE",
                                name=op.f("fk_memory_fact_sources_conversation_id_conversations")),
        sa.PrimaryKeyConstraint("fact_id", "conversation_id",
                                name=op.f("pk_memory_fact_sources")),
    )


def downgrade() -> None:
    op.drop_table("memory_fact_sources")
    op.drop_index("ix_memory_facts_subject_id", table_name="memory_facts")
    op.drop_index("ix_memory_facts_user_id", table_name="memory_facts")
    op.drop_table("memory_facts")
    op.drop_table("memory_entities")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("memory_enabled")
