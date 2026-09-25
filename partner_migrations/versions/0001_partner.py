"""Independent durable partner orders and report snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "partner_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "partner_orders",
        sa.Column("client_order_id", sa.String(100), primary_key=True),
        sa.Column(
            "provider_order_id", sa.String(100), nullable=False, unique=True
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(100)),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("deliveries", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_partner_orders_run_id", "partner_orders", ["run_id"])
    op.create_table(
        "partner_reports",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("body", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("partner_reports")
    op.drop_table("partner_orders")
