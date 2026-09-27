"""Real HTTP and independent persistence around worker failures."""

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from brokerage_lab.contracts import PartnerOrderCreate, utc_now
from brokerage_lab.db import OrderRow, ReservationRow
from brokerage_lab.demo import seed_demo
from brokerage_lab.models import OutboxMessage
from brokerage_lab.partner import create_app as create_partner_app
from brokerage_lab.services import create_order
from brokerage_lab.unit_of_work import SqlAlchemyUnitOfWork
from brokerage_lab.worker import claim, finish, process_one

pytestmark = pytest.mark.postgres


def create_work(engine, mode="normal"):
    seed_demo(engine)
    return create_order(
        "demo-account",
        8,
        SqlAlchemyUnitOfWork(engine),
        client_id="demo-client",
        key="delivery",
        mode=mode,
    )


def test_lost_response_retains_cash_and_lookup_recovers(
    postgres_engine, partner_http
):
    order = create_work(postgres_engine, "lost_response")
    assert process_one(postgres_engine, partner_http, retry_delay=0)
    with Session(postgres_engine) as db:
        row = db.get(OrderRow, order["order_id"])
        assert row.communication_status == "UNKNOWN"
        assert row.business_status == "PENDING"
        assert db.get(ReservationRow, row.id).amount == Decimal("800.00")
    assert partner_http.lookup(order["order_id"]).status == "FILLED"
    assert process_one(postgres_engine, partner_http)
    with Session(postgres_engine) as db:
        assert db.get(OrderRow, order["order_id"]).business_status == "FILLED"
        assert db.get(ReservationRow, order["order_id"]) is None
        assert db.scalar(select(OutboxMessage.status)) == "DONE"
    assert not process_one(postgres_engine, partner_http)


def test_expired_worker_cannot_ack_new_lease(postgres_engine, partner_http):
    order = create_work(postgres_engine)
    old = claim(postgres_engine)
    assert claim(postgres_engine) is None
    result = partner_http.submit(old["request"])
    with Session(postgres_engine) as db, db.begin():
        db.get(OutboxMessage, old["id"]).lease_until = utc_now() - timedelta(
            seconds=1
        )
    new = claim(postgres_engine)
    assert old["token"] != new["token"]
    assert not finish(postgres_engine, old, result)
    assert finish(postgres_engine, new, result)
    with Session(postgres_engine) as db:
        assert db.get(OrderRow, order["order_id"]).business_status == "FILLED"


def test_http_does_not_hold_local_database_connection(
    postgres_engine, partner_http
):
    create_work(postgres_engine)
    original = partner_http.submit

    def submit(obj_in):
        assert postgres_engine.pool.checkedout() == 0
        return original(obj_in)

    partner_http.submit = submit
    assert process_one(postgres_engine, partner_http)


def test_rejection_releases_reservation_without_purchase(
    postgres_engine, partner_http
):
    order = create_work(postgres_engine, "reject")
    process_one(postgres_engine, partner_http)
    from brokerage_lab.models import ProcessedExecution

    with Session(postgres_engine) as db:
        assert (
            db.get(OrderRow, order["order_id"]).business_status == "REJECTED"
        )
        assert db.get(ReservationRow, order["order_id"]) is None
        assert (
            db.scalar(select(func.count()).select_from(ProcessedExecution))
            == 0
        )


def test_retry_exhaustion_does_not_invent_rejection(
    postgres_engine, partner_http
):
    order = create_work(postgres_engine, "timeout_before")
    for _ in range(5):
        process_one(postgres_engine, partner_http, retry_delay=0)
    with Session(postgres_engine) as db:
        assert db.scalar(select(OutboxMessage.status)) == "ERROR"
        row = db.get(OrderRow, order["order_id"])
        assert row.communication_status == "UNKNOWN"
        assert row.business_status == "PENDING"
        assert db.get(ReservationRow, row.id).amount == Decimal("800.00")
    assert not process_one(postgres_engine, partner_http)


def test_partner_deduplicates_after_api_restart(partner_engine):
    obj_in = PartnerOrderCreate(
        client_order_id="stable", account_id="account", quantity=8
    )
    with TestClient(
        create_partner_app(partner_engine, "test-key"),
        headers={"X-Partner-Key": "test-key"},
    ) as client:
        first = client.post("/orders", json=obj_in.model_dump(mode="json"))
        assert first.status_code == 200
    with TestClient(
        create_partner_app(partner_engine, "test-key"),
        headers={"X-Partner-Key": "test-key"},
    ) as client:
        replay = client.post("/orders", json=obj_in.model_dump(mode="json"))
        assert replay.json() == first.json()
        changed = obj_in.model_dump(mode="json") | {"quantity": 7}
        assert client.post("/orders", json=changed).status_code == 422
        assert len(client.get("/reports").json()["items"]) == 1


def test_real_process_crash_recovers_once(
    postgres_engine, partner_http, monkeypatch
):
    import os
    import subprocess
    import sys
    import time
    from uuid import uuid4

    from brokerage_lab.models import ProcessedExecution

    seed_demo(postgres_engine)
    run_id = str(uuid4())
    order = create_order(
        "demo-account",
        8,
        SqlAlchemyUnitOfWork(postgres_engine),
        client_id="demo-client",
        key="process-crash",
        run_id=run_id,
    )
    with postgres_engine.connect() as connection:
        schema = connection.scalar(text("SELECT current_schema()"))
    url = postgres_engine.url.update_query_dict(
        {"options": f"-csearch_path={schema}"}
    )
    environment = os.environ.copy() | {
        "DATABASE_URL": url.render_as_string(hide_password=False),
        "PARTNER_URL": str(partner_http.client.base_url),
        "PARTNER_API_KEY": "test-partner-key",
        "APP_ENV": "development",
    }
    command = [
        sys.executable,
        "-m",
        "brokerage_lab.worker",
        "--once",
        "--run-id",
        run_id,
        "--lease-seconds",
        "0.5",
    ]
    crashed = subprocess.run(
        command + ["--crash-after-send"],
        env=environment,
        capture_output=True,
        timeout=20,
    )
    assert crashed.returncode == 86
    remote = partner_http.lookup(order["order_id"])
    with Session(postgres_engine) as db:
        assert db.get(ReservationRow, order["order_id"]).amount == 800
        assert db.scalar(select(OutboxMessage.status)) == "CLAIMED"
        lease = db.scalar(select(OutboxMessage.lease_until))
    deadline = time.monotonic() + 5
    while utc_now() <= lease and time.monotonic() < deadline:
        time.sleep(0.02)
    for _ in range(2):
        recovered = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            timeout=20,
        )
        assert recovered.returncode == 0
    assert partner_http.lookup(order["order_id"]).provider_order_id == (
        remote.provider_order_id
    )
    assert len(partner_http.report().items) == 1
    with Session(postgres_engine) as db:
        assert db.scalar(select(OutboxMessage.status)) == "DONE"
        assert (
            db.scalar(select(func.count()).select_from(ProcessedExecution))
            == 1
        )
