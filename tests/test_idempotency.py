"""One committed request result despite retries and concurrent callers."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from brokerage_lab.db import OrderRow, ReservationRow
from brokerage_lab.demo import seed_demo
from brokerage_lab.models import IdempotencyRecord, OutboxMessage
from brokerage_lab.services import IdempotencyConflict, create_order
from brokerage_lab.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.postgres


def test_replay_normalizes_defaults_and_preserves_response(client):
    headers = {"Idempotency-Key": "same-intent"}
    response = client.post(
        "/demo/orders", json={"quantity": 8}, headers=headers
    )
    repeated = client.post(
        "/demo/orders",
        json={
            "quantity": 8,
            "side": "BUY",
            "currency": "EUR",
            "instrument": "SYNTH-100",
        },
        headers=headers,
    )
    assert response.status_code == repeated.status_code == 201
    assert response.json() == repeated.json()
    assert response.headers["location"] == repeated.headers["location"]
    changed = client.post(
        "/demo/orders", json={"quantity": 1}, headers=headers
    )
    assert changed.status_code == 422
    assert (
        client.get("/demo/account").json()["active_reservations"] == "800.00"
    )


def test_key_is_required_and_scoped_to_authenticated_client(client):
    assert client.post("/demo/orders", json={"quantity": 8}).status_code == 422
    headers = {"Idempotency-Key": "shared-text"}
    first = client.post("/demo/orders", json={"quantity": 8}, headers=headers)
    second = client.post(
        "/accounts/other-demo-account/orders",
        json={"quantity": 8},
        headers=headers | {"X-API-Key": "test-other-key"},
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["order_id"] != second.json()["order_id"]
    assert (
        client.get(
            first.headers["location"], headers={"X-API-Key": "test-other-key"}
        ).status_code
        == 404
    )


def test_twenty_concurrent_retries_create_one_atomic_effect(postgres_engine):
    seed_demo(postgres_engine)
    barrier = Barrier(20)

    def submit(_):
        barrier.wait(timeout=10)
        return create_order(
            "demo-account",
            8,
            SqlAlchemyUnitOfWork(postgres_engine),
            client_id="demo-client",
            key="concurrent",
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        responses = list(pool.map(submit, range(20)))
    assert all(response == responses[0] for response in responses)
    with Session(postgres_engine) as db:
        for table in [
            OrderRow,
            ReservationRow,
            IdempotencyRecord,
            OutboxMessage,
        ]:
            assert db.scalar(select(func.count()).select_from(table)) == 1
        assert db.scalar(select(ReservationRow.amount)) == 800


def test_lost_local_response_replays_committed_result(postgres_engine):
    seed_demo(postgres_engine)
    original_commit = SqlAlchemyUnitOfWork.commit

    def commit_then_disconnect(uow):
        original_commit(uow)
        raise ConnectionError("Response was lost after commit")

    with patch.object(SqlAlchemyUnitOfWork, "commit", commit_then_disconnect):
        with pytest.raises(ConnectionError):
            create_order(
                "demo-account",
                8,
                SqlAlchemyUnitOfWork(postgres_engine),
                client_id="demo-client",
                key="lost-response",
            )
    recovered = create_order(
        "demo-account",
        8,
        SqlAlchemyUnitOfWork(postgres_engine),
        client_id="demo-client",
        key="lost-response",
    )
    with Session(postgres_engine) as db:
        assert db.scalar(select(OrderRow.id)) == recovered["order_id"]
        assert db.scalar(select(func.count()).select_from(OrderRow)) == 1


def test_failure_before_commit_rolls_back_all_four_records(postgres_engine):
    seed_demo(postgres_engine)

    def fail(uow):
        uow.session.flush()
        raise RuntimeError("Before commit")

    with patch.object(SqlAlchemyUnitOfWork, "commit", fail):
        with pytest.raises(RuntimeError):
            create_order(
                "demo-account",
                8,
                SqlAlchemyUnitOfWork(postgres_engine),
                client_id="demo-client",
                key="rollback",
            )
    with Session(postgres_engine) as db:
        for table in [
            OrderRow,
            ReservationRow,
            IdempotencyRecord,
            OutboxMessage,
        ]:
            assert db.scalar(select(func.count()).select_from(table)) == 0


def test_same_key_changed_account_is_conflict(postgres_engine):
    seed_demo(postgres_engine)
    from brokerage_lab.ledger import fund_account

    with Session(postgres_engine) as db, db.begin():
        fund_account(db, "second-owned", "demo-client")
    create_order(
        "demo-account",
        1,
        SqlAlchemyUnitOfWork(postgres_engine),
        client_id="demo-client",
        key="one-intent",
    )
    with pytest.raises(IdempotencyConflict):
        create_order(
            "second-owned",
            1,
            SqlAlchemyUnitOfWork(postgres_engine),
            client_id="demo-client",
            key="one-intent",
        )
