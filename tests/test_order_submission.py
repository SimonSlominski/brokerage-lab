"""Real PostgreSQL evidence for the local order submission transaction."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from brokerage_lab.api import create_app
from brokerage_lab.db import AccountRow, OrderRow, ReservationRow, order_to_row
from brokerage_lab.demo import DEMO_ACCOUNT_ID, seed_demo
from brokerage_lab.domain import InsufficientFundsError, Order
from brokerage_lab.repositories import SqlAlchemyAccountRepository
from brokerage_lab.services import submit_order
from brokerage_lab.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.postgres


def test_order_reserves_cash_and_can_be_read(postgres_engine):
    seed_demo(postgres_engine)
    with TestClient(
        create_app(engine=postgres_engine),
        headers={"X-API-Key": "test-demo-key"},
    ) as client:
        result = client.post(
            "/demo/orders",
            json={"quantity": 8},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert result.status_code == 201
        body = result.json()
        UUID(body["order_id"])
        assert body == {
            "order_id": body["order_id"],
            "account_id": DEMO_ACCOUNT_ID,
            "instrument": "SYNTH-100",
            "side": "BUY",
            "quantity": 8,
            "currency": "EUR",
            "unit_price": "100.00",
            "reservation_amount": "800.00",
            "business_status": "PENDING",
            "communication_status": "NOT_SENT",
        }
        assert result.headers["location"] == (f"/orders/{body['order_id']}")
        assert client.get(result.headers["location"]).json() == body
        assert client.get("/demo/account").json() == {
            "account_id": DEMO_ACCOUNT_ID,
            "currency": "EUR",
            "posted_cash": "1000.00",
            "active_reservations": "800.00",
            "available_cash": "200.00",
        }
    with Session(postgres_engine) as session:
        assert session.get(OrderRow, body["order_id"]).quantity == 8
        reservation = session.get(ReservationRow, body["order_id"])
        assert reservation.account_id == DEMO_ACCOUNT_ID
        assert reservation.amount == Decimal("800.00")


@pytest.mark.parametrize("first_quantity", [None, 8])
def test_insufficient_funds_create_no_partial_records(
    postgres_engine, first_quantity
):
    seed_demo(postgres_engine)
    with TestClient(
        create_app(engine=postgres_engine),
        headers={"X-API-Key": "test-demo-key"},
    ) as client:
        if first_quantity:
            assert (
                client.post(
                    "/demo/orders",
                    json={"quantity": first_quantity},
                    headers={"Idempotency-Key": str(uuid4())},
                ).status_code
                == 201
            )
        before = client.get("/demo/account").json()
        quantity = 3 if first_quantity else 11
        rejected = client.post(
            "/demo/orders",
            json={"quantity": quantity},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert rejected.status_code == 409
        assert rejected.json() == {"detail": "Insufficient available cash"}
        assert client.get("/demo/account").json() == before
    with Session(postgres_engine) as session:
        expected = 1 if first_quantity else 0
        assert len(session.scalars(select(OrderRow)).all()) == expected
        assert len(session.scalars(select(ReservationRow)).all()) == expected


def test_exact_remaining_cash_can_be_reserved(postgres_engine):
    seed_demo(postgres_engine)
    with TestClient(
        create_app(engine=postgres_engine),
        headers={"X-API-Key": "test-demo-key"},
    ) as client:
        assert (
            client.post(
                "/demo/orders",
                json={"quantity": 8},
                headers={"Idempotency-Key": str(uuid4())},
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/demo/orders",
                json={"quantity": 2},
                headers={"Idempotency-Key": str(uuid4())},
            ).status_code
            == 201
        )
        assert client.get("/demo/account").json()["available_cash"] == "0.00"
        assert (
            client.post(
                "/demo/orders",
                json={"quantity": 1},
                headers={"Idempotency-Key": str(uuid4())},
            ).status_code
            == 409
        )


def test_missing_demo_account_has_no_side_effects(postgres_engine):
    with TestClient(
        create_app(engine=postgres_engine),
        headers={"X-API-Key": "test-demo-key"},
    ) as client:
        response = client.post(
            "/demo/orders",
            json={"quantity": 8},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "Account not found"}
    with Session(postgres_engine) as session:
        assert session.scalars(select(OrderRow)).all() == []
        assert session.scalars(select(ReservationRow)).all() == []


def test_order_read_is_limited_to_demo_account(postgres_engine):
    seed_demo(postgres_engine)
    with Session(postgres_engine) as session, session.begin():
        session.add(
            AccountRow(
                id="other-account",
                posted_cash=Decimal("1000.00"),
                currency="EUR",
            )
        )
        session.flush()
        session.add(
            order_to_row(Order.demo_buy("other-order", "other-account", 1))
        )
    with TestClient(
        create_app(engine=postgres_engine),
        headers={"X-API-Key": "test-demo-key"},
    ) as client:
        for order_id in ["missing-order", "other-order"]:
            result = client.get(f"/demo/orders/{order_id}")
            assert result.status_code == 404
            assert result.json() == {"detail": "Order not found"}


@pytest.mark.parametrize("after_reservation_flush", [False, True])
def test_failure_during_submission_rolls_back_all_writes(
    postgres_engine, monkeypatch, after_reservation_flush
):
    seed_demo(postgres_engine)
    original = SqlAlchemyAccountRepository.add_reservation

    def fail(self, account_id, reservation):
        if after_reservation_flush:
            original(self, account_id, reservation)
        raise OperationalError("hidden SQL", {}, Exception("secret"))

    monkeypatch.setattr(SqlAlchemyAccountRepository, "add_reservation", fail)
    with TestClient(
        create_app(engine=postgres_engine),
        headers={"X-API-Key": "test-demo-key"},
    ) as client:
        result = client.post(
            "/demo/orders",
            json={"quantity": 8},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert result.status_code == 503
        assert "secret" not in result.text
        assert "hidden SQL" not in result.text
        assert (
            client.get("/demo/account").json()["available_cash"] == "1000.00"
        )
    with Session(postgres_engine) as session:
        assert session.scalars(select(OrderRow)).all() == []
        assert session.scalars(select(ReservationRow)).all() == []


def test_two_simultaneous_orders_cannot_overspend(postgres_engine):
    seed_demo(postgres_engine)
    barrier = Barrier(2)
    backend_pids = []

    def synchronize_lock_attempts(
        connection, cursor, statement, parameters, context, executemany
    ):
        if "FOR UPDATE" in statement:
            backend_pids.append(
                connection.connection.driver_connection.info.backend_pid
            )
            # Both independent transactions reach their locking read together.
            barrier.wait(timeout=5)

    def submit():
        try:
            submit_order(
                DEMO_ACCOUNT_ID,
                8,
                SqlAlchemyUnitOfWork(postgres_engine),
                client_id="demo-client",
            )
            return "created"
        except InsufficientFundsError:
            return "insufficient_funds"

    event.listen(
        postgres_engine, "before_cursor_execute", synchronize_lock_attempts
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            outcomes = [future.result(timeout=10) for future in futures]
    finally:
        event.remove(
            postgres_engine,
            "before_cursor_execute",
            synchronize_lock_attempts,
        )
    assert sorted(outcomes) == ["created", "insufficient_funds"]
    assert len(set(backend_pids)) == 2
    with Session(postgres_engine) as session:
        assert len(session.scalars(select(OrderRow)).all()) == 1
        reservations = session.scalars(select(ReservationRow)).all()
        assert len(reservations) == 1
        assert reservations[0].amount == Decimal("800.00")
        assert session.get(AccountRow, DEMO_ACCOUNT_ID).posted_cash == Decimal(
            "1000.00"
        )
    with TestClient(
        create_app(engine=postgres_engine),
        headers={"X-API-Key": "test-demo-key"},
    ) as client:
        assert client.get("/demo/account").json()["available_cash"] == "200.00"
