"""Ownership failures must not reveal orders or create financial effects."""

from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from brokerage_lab.api import create_app
from brokerage_lab.auth import DemoAuthSettings
from brokerage_lab.db import AccountRow, OrderRow, ReservationRow
from brokerage_lab.demo import DEMO_ACCOUNT_ID, seed_demo
from brokerage_lab.services import AccountNotFoundError, submit_order
from brokerage_lab.unit_of_work import SqlAlchemyUnitOfWork


@pytest.mark.parametrize("key", [None, "invalid-key"])
def test_authentication_required_without_database_access(key):
    engine = MagicMock()
    headers = {} if key is None else {"X-API-Key": key}
    with TestClient(create_app(engine=engine), headers=headers) as client:
        for method, path, body in [
            ("GET", "/demo/account", None),
            ("GET", "/demo/orders/unknown", None),
            ("POST", "/demo/orders", {"quantity": 8}),
        ]:
            result = client.request(method, path, json=body)
            assert result.status_code == 401
            assert result.json() == {"detail": "Invalid or missing API key"}
        assert client.get("/health/live").status_code == 200
        schema = client.get("/openapi.json").json()
        assert schema["paths"]["/demo/account"]["get"]["security"]
    engine.connect.assert_not_called()


def test_missing_auth_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv("DEMO_API_KEY", "")
    monkeypatch.setenv("OTHER_DEMO_API_KEY", "")
    engine = MagicMock()
    with TestClient(create_app(engine=engine)) as client:
        assert client.get("/demo/account").status_code == 503
        assert client.get("/health/live").status_code == 200
    engine.connect.assert_not_called()


def test_duplicate_identity_keys_are_rejected():
    with pytest.raises(ValueError, match="must be distinct"):
        DemoAuthSettings(
            demo_api_key="same-key", other_demo_api_key="same-key"
        ).identities()


@pytest.mark.postgres
def test_foreign_client_cannot_read_or_reserve(postgres_engine):
    seed_demo(postgres_engine)
    with TestClient(create_app(engine=postgres_engine)) as client:
        owner = {"X-API-Key": "test-demo-key"}
        foreign = {"X-API-Key": "test-other-key"}
        created = client.post(
            "/demo/orders",
            json={"quantity": 1},
            headers=(owner | {"Idempotency-Key": str(uuid4())}),
        )
        assert created.status_code == 201
        before = client.get("/demo/account", headers=owner).json()
        for path in ["/demo/account", created.headers["location"]]:
            assert client.get(path, headers=foreign).status_code == 404
        refused = client.post(
            "/demo/orders",
            json={"quantity": 8},
            headers=(foreign | {"Idempotency-Key": str(uuid4())}),
        )
        assert refused.status_code == 404
        assert client.get("/demo/account", headers=owner).json() == before
    # Calling the use case directly cannot bypass the ownership check.
    with pytest.raises(AccountNotFoundError):
        submit_order(
            DEMO_ACCOUNT_ID,
            8,
            SqlAlchemyUnitOfWork(postgres_engine),
            client_id="other-demo-client",
        )
    with Session(postgres_engine) as session:
        assert len(session.scalars(select(OrderRow)).all()) == 1
        reservations = session.scalars(select(ReservationRow)).all()
        assert len(reservations) == 1
        assert reservations[0].amount == Decimal("100.00")
        assert session.get(AccountRow, DEMO_ACCOUNT_ID).posted_cash == Decimal(
            "1000.00"
        )


@pytest.mark.postgres
def test_owner_migration_preserves_cash_and_denies_unassigned(postgres_engine):
    # All operations stay in the fixture's disposable schema.
    config = Config("alembic.ini")
    with postgres_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "6a42afaa2f8b")
        connection.execute(
            text(
                "INSERT INTO accounts (id, posted_cash, currency) "
                "VALUES "
                "('demo-account', 725.00, 'EUR'), "
                "('legacy-account', 90.00, 'EUR')"
            )
        )
        command.upgrade(config, "head")
        command.check(config)
    with Session(postgres_engine) as session:
        demo = session.get(AccountRow, DEMO_ACCOUNT_ID)
        legacy = session.get(AccountRow, "legacy-account")
        assert demo.owner_client_id == "demo-client"
        assert demo.posted_cash == Decimal("725.00")
        assert legacy.owner_client_id == "unassigned"
        assert legacy.posted_cash == Decimal("90.00")
    seed_demo(postgres_engine)
    with TestClient(
        create_app(engine=postgres_engine),
        headers={"X-API-Key": "test-demo-key"},
    ) as client:
        assert client.get("/demo/account").json()["posted_cash"] == "725.00"


@pytest.mark.postgres
def test_access_follows_stored_owner_not_a_hardcoded_identity(postgres_engine):
    seed_demo(postgres_engine)
    with Session(postgres_engine) as session, session.begin():
        session.get(
            AccountRow, DEMO_ACCOUNT_ID
        ).owner_client_id = "other-demo-client"
    # Re-seeding must not grant ownership back to the original demo client.
    seed_demo(postgres_engine)
    with TestClient(create_app(engine=postgres_engine)) as client:
        assert (
            client.get(
                "/demo/account", headers={"X-API-Key": "test-demo-key"}
            ).status_code
            == 404
        )
        other = {"X-API-Key": "test-other-key"}
        assert client.get("/demo/account", headers=other).status_code == 200
        created = client.post(
            "/demo/orders",
            json={"quantity": 1},
            headers=(other | {"Idempotency-Key": str(uuid4())}),
        )
        assert created.status_code == 201
        assert (
            client.get(created.headers["location"], headers=other).status_code
            == 200
        )
