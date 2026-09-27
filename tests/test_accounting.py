"""Monetary expectations, immutable history and atomic execution processing."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from brokerage_lab.contracts import Execution, PartnerResult, utc_now
from brokerage_lab.db import AccountRow, OrderRow, ReservationRow
from brokerage_lab.demo import reset_demo, seed_demo
from brokerage_lab.executions import (
    ExecutionConflict,
    apply_partner_result,
    book_execution,
)
from brokerage_lab.ledger import (
    cash_from_ledger,
    positions,
    rebuild_cash,
    reverse_journal,
)
from brokerage_lab.models import (
    InstrumentMovement,
    JournalEntry,
    JournalTransaction,
    ProcessedExecution,
)
from brokerage_lab.services import create_order
from brokerage_lab.unit_of_work import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.postgres


@pytest.fixture
def execution(postgres_engine):
    seed_demo(postgres_engine)
    order = create_order(
        "demo-account",
        8,
        SqlAlchemyUnitOfWork(postgres_engine),
        client_id="demo-client",
        key="execution",
    )
    return Execution(
        execution_id="execution-1",
        order_id=order["order_id"],
        account_id="demo-account",
        quantity=8,
        amount="800.00",
        executed_at=utc_now(),
    )


def test_ten_deliveries_book_one_effect(postgres_engine, execution):
    barrier = Barrier(10)

    def receive(_):
        barrier.wait(timeout=10)
        with Session(postgres_engine) as db, db.begin():
            return book_execution(db, execution)

    with ThreadPoolExecutor(max_workers=10) as pool:
        ids = list(pool.map(receive, range(10)))
    assert len(set(ids)) == 1
    with Session(postgres_engine) as db:
        for model in (ProcessedExecution, InstrumentMovement):
            assert db.scalar(select(func.count()).select_from(model)) == 1
        assert db.get(AccountRow, "demo-account").posted_cash == 200
        assert cash_from_ledger(db, "demo-account") == Decimal("200.00")
        assert db.get(ReservationRow, execution.order_id) is None
        assert positions(db, "demo-account") == [
            {"instrument": "SYNTH-100", "quantity": 8}
        ]
        entries = db.execute(
            select(JournalEntry.bucket, JournalEntry.amount).where(
                JournalEntry.transaction_id == ids[0]
            )
        ).all()
        assert dict(entries) == {
            "CLIENT_CASH": Decimal("-800.00"),
            "CLEARING": Decimal("800.00"),
        }
        totals = db.execute(
            select(
                JournalEntry.transaction_id, func.sum(JournalEntry.amount)
            ).group_by(JournalEntry.transaction_id)
        ).all()
        assert all(total == 0 for _, total in totals)


def test_failure_rolls_back_every_effect(postgres_engine, execution):
    with pytest.raises(RuntimeError):
        with Session(postgres_engine) as db, db.begin():
            book_execution(db, execution)
            db.flush()
            raise RuntimeError("Injected before commit")
    with Session(postgres_engine) as db:
        assert db.get(AccountRow, "demo-account").posted_cash == 1000
        assert db.get(ReservationRow, execution.order_id).amount == 800
        assert (
            db.get(OrderRow, execution.order_id).business_status == "PENDING"
        )
        for model in (ProcessedExecution, InstrumentMovement):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(JournalTransaction)
                .where(JournalTransaction.kind == "EXECUTION")
            )
            == 0
        )
    with Session(postgres_engine) as db, db.begin():
        book_execution(db, execution)


def test_conflict_does_not_change_money(postgres_engine, execution):
    with Session(postgres_engine) as db, db.begin():
        book_execution(db, execution)
    changed = Execution.model_validate(
        execution.model_dump() | {"amount": Decimal("700.00")}
    )
    with pytest.raises(ExecutionConflict):
        with Session(postgres_engine) as db, db.begin():
            book_execution(db, changed)
    with Session(postgres_engine) as db:
        assert cash_from_ledger(db, "demo-account") == Decimal("200.00")


@pytest.mark.parametrize("with_entry", [False, True])
def test_unbalanced_journal_cannot_commit(
    postgres_engine, execution, with_entry
):
    with pytest.raises(DBAPIError, match="balanced"):
        with Session(postgres_engine) as db, db.begin():
            transaction_id = str(uuid4())
            db.add(
                JournalTransaction(
                    id=transaction_id,
                    account_id="demo-account",
                    kind="INVALID",
                    reference=transaction_id,
                    reason="Test",
                )
            )
            db.flush()
            if with_entry:
                db.add(
                    JournalEntry(
                        id=str(uuid4()),
                        transaction_id=transaction_id,
                        bucket="CLIENT_CASH",
                        amount=Decimal("1.00"),
                    )
                )


def test_immutable_history_and_linked_correction(postgres_engine, execution):
    with Session(postgres_engine) as db, db.begin():
        original = book_execution(db, execution)
    for table in (
        "journal_entries",
        "journal_transactions",
        "instrument_movements",
        "processed_executions",
    ):
        column = "provider" if table == "processed_executions" else "id"
        for statement in (
            f"DELETE FROM {table}",
            f"UPDATE {table} SET {column} = {column}",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                with postgres_engine.begin() as connection:
                    connection.execute(text(statement))
    with Session(postgres_engine) as db, db.begin():
        correction = reverse_journal(db, original, "Operator correction")
        assert reverse_journal(db, original, "Retry correction") == correction
    with Session(postgres_engine) as db:
        assert db.get(JournalTransaction, correction).correction_of == original
        assert cash_from_ledger(db, "demo-account") == Decimal("1000.00")
        assert positions(db, "demo-account")[0]["quantity"] == 0
        assert db.get(JournalTransaction, original).kind == "EXECUTION"


def test_projection_rebuild(postgres_engine, execution):
    with Session(postgres_engine) as db, db.begin():
        book_execution(db, execution)
        db.get(AccountRow, "demo-account").posted_cash = Decimal("300.00")
    with Session(postgres_engine) as db, db.begin():
        assert rebuild_cash(db, "demo-account") == Decimal("200.00")


def test_restricted_role_cannot_mutate_history(postgres_engine, execution):
    role = "lab_test_" + uuid4().hex
    with postgres_engine.begin() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
        connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        connection.execute(
            text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
        )
        connection.execute(
            text(
                f"GRANT SELECT, INSERT ON ALL TABLES "
                f'IN SCHEMA "{schema}" TO "{role}"'
            )
        )
    try:
        for statement in (
            "DELETE FROM journal_entries",
            "UPDATE journal_entries SET amount = amount",
        ):
            with pytest.raises(DBAPIError, match="permission denied"):
                with postgres_engine.begin() as connection:
                    connection.execute(text(f'SET LOCAL ROLE "{role}"'))
                    assert (
                        connection.scalar(text("SELECT current_user")) == role
                    )
                    connection.execute(text(statement))
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(text(f'DROP OWNED BY "{role}"'))
            connection.execute(text(f'DROP ROLE "{role}"'))


def test_late_acceptance_cannot_regress_fill(postgres_engine, execution):
    with Session(postgres_engine) as db, db.begin():
        book_execution(db, execution)
    with Session(postgres_engine) as db, db.begin():
        apply_partner_result(
            db,
            PartnerResult(
                client_order_id=execution.order_id,
                provider_order_id="provider",
                status="ACCEPTED",
                observed_at=utc_now(),
            ),
        )
    with Session(postgres_engine) as db:
        assert db.get(OrderRow, execution.order_id).business_status == "FILLED"


def test_seeded_duplicate_and_late_event_sequence(postgres_engine, execution):
    from random import Random

    actions = ["execution"] * 12 + ["late_acceptance"] * 8
    Random(42).shuffle(actions)
    for action in actions:
        with Session(postgres_engine) as db, db.begin():
            if action == "execution":
                book_execution(db, execution)
            else:
                apply_partner_result(
                    db,
                    PartnerResult(
                        client_order_id=execution.order_id,
                        provider_order_id="stable-provider-id",
                        status="ACCEPTED",
                        observed_at=utc_now(),
                    ),
                )
    with Session(postgres_engine) as db:
        assert cash_from_ledger(db, "demo-account") == Decimal("200.00")
        assert positions(db, "demo-account")[0]["quantity"] == 8
        assert db.get(OrderRow, execution.order_id).business_status == "FILLED"
        assert (
            db.scalar(select(func.count()).select_from(ProcessedExecution))
            == 1
        )


def test_reset_missing_account_creates_balanced_funding(postgres_engine):
    reset_demo(postgres_engine, app_env="development", confirmed=True)
    with Session(postgres_engine) as db:
        assert db.get(AccountRow, "demo-account").posted_cash == Decimal(
            "1000"
        )
        assert cash_from_ledger(db, "demo-account") == Decimal("1000")
        assert db.scalar(select(func.sum(JournalEntry.amount))) == Decimal("0")
