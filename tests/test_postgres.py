"""Real PostgreSQL migration, rollback, mapping and local API checks."""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from brokerage_lab.api import create_app
from brokerage_lab.db import (
    AccountRow,
    OrderRow,
    ReservationRow,
    load_account,
    order_from_row,
    order_to_row,
)
from brokerage_lab.demo import DEMO_ACCOUNT_ID, reset_demo, seed_demo
from brokerage_lab.domain import Order

pytestmark = pytest.mark.postgres


def test_migration_and_demo_api(postgres_engine):
    assert {"accounts", "orders", "cash_reservations", "instruments", "alembic_version"} <= set(
        inspect(postgres_engine).get_table_names()
    )
    seed_demo(postgres_engine)
    with TestClient(create_app(engine=postgres_engine)) as client:
        assert client.get("/health/ready").status_code == 200
        response = client.get("/demo/account")
        assert response.status_code == 200
        assert response.json() == {
            "account_id": DEMO_ACCOUNT_ID,
            "currency": "EUR",
            "posted_cash": "1000.00",
            "active_reservations": "0.00",
            "available_cash": "1000.00",
        }


def test_missing_demo_returns_english_404(postgres_engine):
    with TestClient(create_app(engine=postgres_engine)) as client:
        response = client.get("/demo/account")
        assert response.status_code == 404
        assert response.json() == {"detail": "Demo account not found; run the demo seed command"}


def test_order_mapping_and_cash_read(postgres_engine):
    seed_demo(postgres_engine)
    order = Order.demo_buy("order-db", DEMO_ACCOUNT_ID, 8)
    with Session(postgres_engine) as session, session.begin():
        session.add(order_to_row(order))
        session.flush()
        session.add(
            ReservationRow(
                order_id=order.id, account_id=order.account_id, amount=Decimal("800.00"), currency="EUR"
            )
        )
    with Session(postgres_engine) as session:
        assert order_from_row(session.get(OrderRow, order.id)) == order
        account = load_account(session, DEMO_ACCOUNT_ID)
        assert account.available_cash.amount == Decimal("200.00")
    with TestClient(create_app(engine=postgres_engine)) as client:
        assert client.get("/demo/account").json()["active_reservations"] == "800.00"


def test_independent_connections_do_not_see_uncommitted_data_and_rollback(postgres_engine):
    with Session(postgres_engine) as writer, Session(postgres_engine) as reader:
        assert writer.scalar(text("SELECT pg_backend_pid()")) != reader.scalar(
            text("SELECT pg_backend_pid()")
        )
        writer.add(AccountRow(id="rollback-account", posted_cash=Decimal("10.00"), currency="EUR"))
        writer.flush()
        assert reader.get(AccountRow, "rollback-account") is None
        writer.rollback()
    with Session(postgres_engine) as session:
        assert session.get(AccountRow, "rollback-account") is None


def test_database_rejects_negative_cash(postgres_engine):
    with pytest.raises(IntegrityError), Session(postgres_engine) as session, session.begin():
        session.add(AccountRow(id="invalid", posted_cash=Decimal("-1.00"), currency="EUR"))
        session.flush()


def test_reset_is_repeatable_and_scoped_to_demo(postgres_engine):
    seed_demo(postgres_engine)
    demo_order = Order.demo_buy("reset-order", DEMO_ACCOUNT_ID, 8)
    with Session(postgres_engine) as session, session.begin():
        session.add(AccountRow(id="keep-account", posted_cash=Decimal("42.00"), currency="EUR"))
        session.add(order_to_row(demo_order))
        session.flush()
        session.add(
            ReservationRow(
                order_id=demo_order.id, account_id=DEMO_ACCOUNT_ID, amount=Decimal("800.00"), currency="EUR"
            )
        )
    for _ in range(2):
        reset_demo(postgres_engine, app_env="development", confirmed=True)
    with Session(postgres_engine) as session:
        assert session.get(AccountRow, "keep-account").posted_cash == Decimal("42.00")
        assert session.get(AccountRow, DEMO_ACCOUNT_ID).posted_cash == Decimal("1000.00")
        assert session.scalars(select(OrderRow)).all() == []
        assert session.scalars(select(ReservationRow)).all() == []


def test_seed_does_not_overwrite_existing_balance_and_reset_requires_confirmation(postgres_engine):
    seed_demo(postgres_engine)
    with Session(postgres_engine) as session, session.begin():
        session.get(AccountRow, DEMO_ACCOUNT_ID).posted_cash = Decimal("700.00")
    seed_demo(postgres_engine)
    for env, confirmed in [("production", True), ("development", False)]:
        with pytest.raises(ValueError, match="development mode and explicit confirmation"):
            reset_demo(postgres_engine, app_env=env, confirmed=confirmed)
    with Session(postgres_engine) as session:
        assert session.get(AccountRow, DEMO_ACCOUNT_ID).posted_cash == Decimal("700.00")
