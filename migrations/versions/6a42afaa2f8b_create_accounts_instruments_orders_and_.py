"""Create accounts instruments orders and reservations"""

import sqlalchemy as sa
from alembic import op

revision = "6a42afaa2f8b"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(length=100), nullable=False),
        sa.Column(
            "posted_cash", sa.Numeric(precision=20, scale=2), nullable=False
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.CheckConstraint("currency = 'EUR'", name="ck_accounts_currency"),
        sa.CheckConstraint(
            "posted_cash >= 0", name="ck_accounts_cash_nonnegative"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "instruments",
        sa.Column("id", sa.String(length=100), nullable=False),
        sa.Column(
            "unit_price", sa.Numeric(precision=20, scale=2), nullable=False
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.CheckConstraint(
            "id = 'SYNTH-100' AND unit_price = 100.00 AND currency = 'EUR'",
            name="ck_instruments_demo",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.String(length=100), nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=False),
        sa.Column("instrument", sa.String(length=100), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column(
            "unit_price", sa.Numeric(precision=20, scale=2), nullable=False
        ),
        sa.Column(
            "reservation_amount",
            sa.Numeric(precision=20, scale=2),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("business_status", sa.String(length=16), nullable=False),
        sa.Column(
            "communication_status", sa.String(length=16), nullable=False
        ),
        sa.CheckConstraint(
            "business_status IN ('PENDING', 'ACCEPTED', 'FILLED', 'REJECTED')",
            name="ck_orders_business_status",
        ),
        sa.CheckConstraint(
            "communication_status IN ('NOT_SENT', 'IN_FLIGHT', "
            "'UNKNOWN', 'CONFIRMED')",
            name="ck_orders_communication_status",
        ),
        sa.CheckConstraint(
            "side = 'BUY' AND currency = 'EUR' AND unit_price = 100.00",
            name="ck_orders_demo_terms",
        ),
        sa.CheckConstraint(
            "quantity > 0 AND quantity <= 9999999999999999",
            name="ck_orders_quantity",
        ),
        sa.CheckConstraint(
            "reservation_amount = quantity * unit_price",
            name="ck_orders_value",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["instrument"],
            ["instruments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "account_id", name="uq_orders_id_account"),
    )
    op.create_index(
        op.f("ix_orders_account_id"), "orders", ["account_id"], unique=False
    )
    op.create_table(
        "cash_reservations",
        sa.Column("order_id", sa.String(length=100), nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.CheckConstraint(
            "amount > 0 AND currency = 'EUR'",
            name="ck_reservations_amount_currency",
        ),
        sa.ForeignKeyConstraint(
            ["order_id", "account_id"],
            ["orders.id", "orders.account_id"],
        ),
        sa.PrimaryKeyConstraint("order_id"),
    )
    op.create_index(
        op.f("ix_cash_reservations_account_id"),
        "cash_reservations",
        ["account_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        op.f("ix_cash_reservations_account_id"), table_name="cash_reservations"
    )
    op.drop_table("cash_reservations")
    op.drop_index(op.f("ix_orders_account_id"), table_name="orders")
    op.drop_table("orders")
    op.drop_table("instruments")
    op.drop_table("accounts")
