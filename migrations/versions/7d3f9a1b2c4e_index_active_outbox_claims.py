"""Index active outbox claims for worker polling."""

from alembic import op

revision = "7d3f9a1b2c4e"
down_revision = "4fc08dd2596b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_outbox_pending_claim",
        "outbox_messages",
        ["run_id", "available_at", "created_at"],
        unique=False,
        postgresql_where="status = 'PENDING'",
    )
    op.create_index(
        "ix_outbox_expired_claim",
        "outbox_messages",
        ["run_id", "lease_until", "created_at"],
        unique=False,
        postgresql_where="status = 'CLAIMED'",
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_expired_claim", table_name="outbox_messages")
    op.drop_index("ix_outbox_pending_claim", table_name="outbox_messages")
