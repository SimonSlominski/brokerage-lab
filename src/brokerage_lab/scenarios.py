"""Repeatable failures with durable rows and real partner HTTP."""

import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .contracts import PartnerOrderCreate, PartnerReport, ReportItem, utc_now
from .db import AccountRow, OrderRow, ReservationRow
from .executions import book_execution
from .ledger import fund_account
from .models import (
    InstrumentMovement,
    OutboxMessage,
    ProcessedExecution,
    ReconciliationBreak,
    ScenarioRun,
)
from .operations import partner_transport
from .reconciliation import reconcile
from .services import create_order
from .unit_of_work import SqlAlchemyUnitOfWork
from .worker import process_one

logger = logging.getLogger(__name__)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def snapshot(engine, account_id: str) -> dict:
    with Session(engine) as db:
        account = db.get(AccountRow, account_id)
        orders = db.scalars(
            select(OrderRow).where(OrderRow.account_id == account_id)
        ).all()
        reserved = db.scalar(
            select(func.coalesce(func.sum(ReservationRow.amount), 0)).where(
                ReservationRow.account_id == account_id
            )
        )
        booked = db.scalar(
            select(func.count())
            .select_from(ProcessedExecution)
            .where(ProcessedExecution.order_id.in_([row.id for row in orders]))
        )
        movements = db.scalar(
            select(func.count())
            .select_from(InstrumentMovement)
            .where(InstrumentMovement.account_id == account_id)
        )
        return dict(
            account_id=account_id,
            posted_cash=str(account.posted_cash),
            reserved=str(Decimal(reserved).quantize(Decimal("0.01"))),
            available=str(account.posted_cash - reserved),
            order_count=len(orders),
            execution_count=booked,
            movement_count=movements,
            orders=[
                dict(
                    id=row.id,
                    business=row.business_status,
                    communication=row.communication_status,
                )
                for row in orders
            ],
        )


def execute_scenario(engine, scenario: str, seed: int = 42) -> dict:
    run_id = str(uuid4())
    account_id = f"lab-{run_id}"
    with Session(engine) as db, db.begin():
        db.add(ScenarioRun(id=run_id, scenario=scenario, seed=seed))
        fund_account(db, account_id, "demo-client")
    transport = partner_transport()
    evidence = {"source": "local PostgreSQL and partner HTTP", "seed": seed}
    error = None

    def submit(key: str = "intent", mode: str = "normal"):
        return create_order(
            account_id,
            8,
            SqlAlchemyUnitOfWork(engine),
            client_id="demo-client",
            key=f"{run_id}:{key}",
            run_id=run_id,
            mode=mode,
        )

    try:
        if scenario == "retry_storm":
            barrier = Barrier(20)

            def retry(_):
                barrier.wait(timeout=15)
                return submit()["order_id"]

            with ThreadPoolExecutor(max_workers=20) as pool:
                ids = list(pool.map(retry, range(20)))
            before = snapshot(engine, account_id)
            require(
                len(set(ids)) == 1 and before["order_count"] == 1,
                "Retries created more than one order",
            )
            require(before["reserved"] == "800.00", "Unexpected reservation")
            evidence.update(
                requests=20,
                distinct_order_ids=len(set(ids)),
                before_delivery=before,
            )
            process_one(engine, transport, run_id=run_id)
        elif scenario == "lost_response":
            order = submit(mode="lost_response")
            process_one(engine, transport, run_id=run_id, retry_delay=0)
            unknown = snapshot(engine, account_id)
            require(
                unknown["orders"][0]["communication"] == "UNKNOWN",
                "Lost response did not produce UNKNOWN",
            )
            require(unknown["reserved"] == "800.00", "Unknown released cash")
            evidence["unknown_snapshot"] = unknown
            remote = transport.lookup(order["order_id"])
            evidence["provider_order_id"] = remote.provider_order_id
            process_one(engine, transport, run_id=run_id)
        elif scenario == "worker_crash":
            order = submit()
            environment = os.environ.copy()
            environment["DATABASE_URL"] = engine.url.render_as_string(
                hide_password=False
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "brokerage_lab.worker",
                    "--once",
                    "--run-id",
                    run_id,
                    "--crash-after-send",
                    "--lease-seconds",
                    "0.5",
                ],
                env=environment,
                capture_output=True,
                timeout=20,
            )
            require(
                result.returncode == 86, "Worker did not exit at fault point"
            )
            remote_before = transport.lookup(order["order_id"])
            evidence["process_exit_code"] = result.returncode
            evidence["after_crash"] = snapshot(engine, account_id)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with Session(engine) as db:
                    lease = db.scalar(
                        select(OutboxMessage.lease_until).where(
                            OutboxMessage.order_id == order["order_id"]
                        )
                    )
                if lease <= utc_now():
                    break
                time.sleep(0.05)
            process_one(engine, transport, run_id=run_id)
            remote_after = transport.lookup(order["order_id"])
            require(
                remote_before.provider_order_id
                == remote_after.provider_order_id,
                "Recovery created a second partner order",
            )
            require(
                not process_one(engine, transport, run_id=run_id),
                "Acknowledged message was claimed again",
            )
            evidence["provider_order_id"] = remote_after.provider_order_id
        elif scenario == "duplicate_execution":
            order = submit()
            result = transport.submit(
                PartnerOrderCreate(
                    client_order_id=order["order_id"],
                    account_id=account_id,
                    quantity=8,
                    run_id=run_id,
                )
            )
            barrier = Barrier(10)

            def deliver(_):
                barrier.wait(timeout=15)
                with Session(engine) as db, db.begin():
                    return book_execution(db, result.execution)

            with ThreadPoolExecutor(max_workers=10) as pool:
                journals = list(pool.map(deliver, range(10)))
            require(len(set(journals)) == 1, "Duplicate accounting effect")
            evidence.update(
                deliveries=10, journal_transactions=len(set(journals))
            )
            process_one(engine, transport, run_id=run_id)
        elif scenario == "amount_mismatch":
            # Deliberate external evidence fixtures, not executable BUY prices.
            now = utc_now()
            local = ReportItem(
                order_id=f"fixture-{run_id}",
                execution_id="fixture-execution",
                status="FILLED",
                amount="950.00",
                executed_at=now,
            )
            remote = ReportItem(
                order_id=local.order_id,
                execution_id=local.execution_id,
                status="FILLED",
                amount="975.00",
                executed_at=now,
            )
            report = PartnerReport(
                report_id=f"fixture-{run_id}", cutoff=now, items=[remote]
            )
            report = transport.store_report_fixture(report)
            reconciliation_id = reconcile(
                engine, report, local_fixture=[local]
            )
            with Session(engine) as db:
                cases = db.scalars(
                    select(ReconciliationBreak).where(
                        ReconciliationBreak.run_id == reconciliation_id
                    )
                ).all()
                require(
                    len(cases) == 1
                    and cases[0].category == "AMOUNT_MISMATCH"
                    and cases[0].status == "OPEN",
                    "Expected one open amount break",
                )
            evidence.update(
                source=(
                    "controlled local fixture and persisted partner "
                    "report fetched over HTTP"
                ),
                local_amount="950.00",
                partner_amount="975.00",
                reconciliation_run_id=reconciliation_id,
            )
        else:
            raise ValueError("Unknown scenario")
        evidence["final"] = snapshot(engine, account_id)
        final = evidence["final"]
        if scenario != "amount_mismatch":
            require(
                final["posted_cash"] == "200.00"
                and final["reserved"] == "0.00"
                and final["execution_count"] == 1
                and final["movement_count"] == 1,
                "Final cash or accounting invariant failed",
            )
            report = transport.report(run_id=run_id)
            require(
                len(report.items) == 1, "Partner report contains extra orders"
            )
            evidence["partner_report"] = report.model_dump(mode="json")
        else:
            require(
                final["posted_cash"] == "1000.00"
                and final["movement_count"] == 0,
                "Reconciliation changed money without evidence",
            )
    except Exception as exc:
        logger.exception(
            "scenario_failed run_id=%s scenario=%s", run_id, scenario
        )
        error = f"{type(exc).__name__}: scenario failed; inspect local logs"
    finally:
        transport.close()
    with Session(engine) as db, db.begin():
        run = db.get(ScenarioRun, run_id)
        run.status = "FAILED" if error else "PASSED"
        run.evidence = evidence
        run.error = error
    return dict(
        run_id=run_id,
        status="FAILED" if error else "PASSED",
        evidence=evidence,
        error=error,
    )
