"""Comparison expectations are fixtures independent of the implementation."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from brokerage_lab.contracts import (
    Execution,
    PartnerOrderCreate,
    PartnerReport,
    PartnerResult,
    ReportItem,
    utc_now,
)
from brokerage_lab.db import AccountRow
from brokerage_lab.demo import seed_demo
from brokerage_lab.domain import DomainError
from brokerage_lab.executions import apply_partner_result, book_execution
from brokerage_lab.models import ReconciliationBreak
from brokerage_lab.reconciliation import (
    compare_records,
    reconcile,
    recover_break,
)
from brokerage_lab.services import create_order
from brokerage_lab.unit_of_work import SqlAlchemyUnitOfWork


def report_item(order_id="order", amount="950.00", **changes):
    values = dict(
        order_id=order_id,
        execution_id="exec",
        status="FILLED",
        amount=amount,
        executed_at=utc_now() - timedelta(seconds=1),
    )
    return ReportItem(**(values | changes))


@pytest.mark.parametrize(
    "category",
    [
        "MISSING_LOCAL",
        "MISSING_PROVIDER",
        "DUPLICATE",
        "AMOUNT_MISMATCH",
        "STATUS_MISMATCH",
    ],
)
def test_each_category(category):
    item = report_item()
    local, remote = [item], [item]
    if category == "MISSING_LOCAL":
        local = []
    elif category == "MISSING_PROVIDER":
        remote = []
    elif category == "DUPLICATE":
        remote = [item, item]
    elif category == "AMOUNT_MISMATCH":
        remote = [report_item(amount="975.00")]
    elif category == "STATUS_MISMATCH":
        remote = [report_item(status="ACCEPTED")]
    report = PartnerReport(report_id="fixture", cutoff=utc_now(), items=remote)
    findings = compare_records(local, report)
    assert [finding["category"] for finding in findings] == [category]


def test_after_cutoff_does_not_create_false_breaks():
    cutoff = utc_now()
    late = report_item(executed_at=cutoff + timedelta(seconds=10))
    report = PartnerReport(report_id="cutoff", cutoff=cutoff, items=[])
    assert compare_records([late], report) == []


@pytest.mark.postgres
def test_mismatch_leaves_cash_unchanged(postgres_engine):
    seed_demo(postgres_engine)
    report = PartnerReport(
        report_id="mismatch",
        cutoff=utc_now(),
        items=[report_item(amount="975.00")],
    )
    run_id = reconcile(postgres_engine, report, local_fixture=[report_item()])
    with Session(postgres_engine) as db:
        case = db.scalar(
            select(ReconciliationBreak).where(
                ReconciliationBreak.run_id == run_id
            )
        )
        assert case.category == "AMOUNT_MISMATCH"
        assert case.status == "OPEN"
        assert case.evidence["local"]["amount"] == "950.00"
        assert case.evidence["partner"]["amount"] == "975.00"
        assert db.get(AccountRow, "demo-account").posted_cash == 1000


@pytest.mark.postgres
@pytest.mark.parametrize("invalid_status", ["ACCEPTED", "REJECTED"])
def test_missing_execution_recovered_once(
    postgres_engine, partner_http, invalid_status
):
    seed_demo(postgres_engine)
    order = create_order(
        "demo-account",
        8,
        SqlAlchemyUnitOfWork(postgres_engine),
        client_id="demo-client",
        key="missing",
    )
    partner_http.submit(
        PartnerOrderCreate(
            client_order_id=order["order_id"],
            account_id="demo-account",
            quantity=8,
        )
    )
    run_id = reconcile(postgres_engine, partner_http.report())
    with Session(postgres_engine) as db:
        case = db.scalar(
            select(ReconciliationBreak).where(
                ReconciliationBreak.run_id == run_id
            )
        )
        assert case.category == "MISSING_LOCAL"
        break_id = case.id
    contradictory = PartnerResult.model_validate(
        partner_http.lookup(order["order_id"]).model_dump()
        | {"status": invalid_status}
    )
    transport = SimpleNamespace(lookup=lambda _: contradictory)
    with pytest.raises(DomainError, match="No confirmed execution"):
        recover_break(postgres_engine, transport, break_id, "operator")
    with Session(postgres_engine) as db:
        assert db.get(AccountRow, "demo-account").posted_cash == 1000
        assert db.get(ReconciliationBreak, break_id).status == "OPEN"
    first = recover_break(postgres_engine, partner_http, break_id, "operator")
    second = recover_break(postgres_engine, partner_http, break_id, "operator")
    assert first == second
    next_run = reconcile(postgres_engine, partner_http.report())
    with Session(postgres_engine) as db:
        assert db.get(ReconciliationBreak, break_id).status == "RESOLVED"
        assert (
            db.scalars(
                select(ReconciliationBreak).where(
                    ReconciliationBreak.run_id == next_run
                )
            ).all()
            == []
        )
        assert db.get(AccountRow, "demo-account").posted_cash == 200


@pytest.mark.postgres
def test_confirmed_rejection_is_not_missing_execution(
    postgres_engine, partner_http
):
    from brokerage_lab.worker import process_one

    seed_demo(postgres_engine)
    create_order(
        "demo-account",
        8,
        SqlAlchemyUnitOfWork(postgres_engine),
        client_id="demo-client",
        key="rejected-report",
        mode="reject",
    )
    process_one(postgres_engine, partner_http)
    run_id = reconcile(postgres_engine, partner_http.report())
    with Session(postgres_engine) as db:
        assert (
            db.scalars(
                select(ReconciliationBreak).where(
                    ReconciliationBreak.run_id == run_id
                )
            ).all()
            == []
        )


@pytest.mark.postgres
def test_cutoff_uses_acceptance_before_later_fill(postgres_engine):
    seed_demo(postgres_engine)
    order = create_order(
        "demo-account",
        8,
        SqlAlchemyUnitOfWork(postgres_engine),
        client_id="demo-client",
        key="historical-cutoff",
    )
    accepted_at = utc_now() - timedelta(seconds=10)
    cutoff = accepted_at + timedelta(seconds=1)
    with Session(postgres_engine) as db, db.begin():
        apply_partner_result(
            db,
            PartnerResult(
                client_order_id=order["order_id"],
                provider_order_id="provider-order",
                status="ACCEPTED",
                observed_at=accepted_at,
            ),
        )
    with Session(postgres_engine) as db, db.begin():
        book_execution(
            db,
            Execution(
                execution_id="late-fill",
                order_id=order["order_id"],
                account_id="demo-account",
                quantity=8,
                amount="800.00",
                executed_at=utc_now(),
            ),
        )
    report = PartnerReport(
        report_id="historical-report",
        cutoff=cutoff,
        items=[
            ReportItem(
                order_id=order["order_id"],
                status="ACCEPTED",
                amount="0.00",
                executed_at=accepted_at,
            )
        ],
    )
    run_id = reconcile(postgres_engine, report)
    with Session(postgres_engine) as db:
        assert (
            list(
                db.scalars(
                    select(ReconciliationBreak).where(
                        ReconciliationBreak.run_id == run_id
                    )
                )
            )
            == []
        )
