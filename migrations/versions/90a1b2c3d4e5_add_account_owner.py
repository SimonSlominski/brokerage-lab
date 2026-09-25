"""Add account ownership, assigning only the known local demo account."""

import sqlalchemy as sa
from alembic import op

revision = "90a1b2c3d4e5"
down_revision = "6a42afaa2f8b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "owner_client_id",
            sa.String(100),
            nullable=False,
            server_default="unassigned",
        ),
    )
    op.execute(
        sa.text(
            "UPDATE accounts SET owner_client_id = 'demo-client' "
            "WHERE id = 'demo-account'"
        )
    )


def downgrade() -> None:
    op.drop_column("accounts", "owner_client_id")
