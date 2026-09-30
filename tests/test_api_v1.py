"""Versioned conventions without changing the existing demo contract."""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from brokerage_lab.api import create_app
from brokerage_lab.api_v1 import position_page
from brokerage_lab.contracts import utc_now
from brokerage_lab.models import IdempotencyRecord

V1_HEADERS = {
    "Authorization": "Bearer test-demo-key",
    "LMG-Data-Privacy-Access-Principal": "backend-demo-client",
    "LMG-Data-Privacy-Access-Justification": "app_usage-order-entry",
}


def test_v1_requires_bearer_and_audit_headers_before_database():
    engine = MagicMock()
    with TestClient(create_app(engine=engine)) as client:
        assert (
            client.get(
                "/v1/demo/account", headers={"X-API-Key": "test-demo-key"}
            ).status_code
            == 401
        )
        response = client.get("/v1/demo/account")
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.json() == {"message": "Invalid or missing API key"}
        for missing in (
            "LMG-Data-Privacy-Access-Principal",
            "LMG-Data-Privacy-Access-Justification",
        ):
            response = client.get(
                "/v1/demo/account",
                headers={
                    key: value
                    for key, value in V1_HEADERS.items()
                    if key != missing
                },
            )
            assert response.status_code == 422
        for bad in ("", "x" * 256, "backend\nforged-log"):
            assert (
                client.get(
                    "/v1/demo/account",
                    headers=V1_HEADERS
                    | {
                        "LMG-Data-Privacy-Access-Principal": bad,
                    },
                ).status_code
                == 422
            )
        assert (
            "BrokerageBearer"
            in client.get("/openapi.json").json()["components"][
                "securitySchemes"
            ]
        )
        schema = client.get("/openapi.json").json()
        assert (
            "message"
            in schema["components"]["schemas"]["ApiError"]["properties"]
        )
        parameters = schema["paths"]["/v1/demo/account"]["get"]["parameters"]
        assert len(parameters) == 2
        assert all(param["required"] for param in parameters)
    engine.connect.assert_not_called()


@pytest.mark.postgres
def test_v1_and_legacy_replay_same_order_without_contract_regression(client):
    key = {"Idempotency-Key": "cross-version-intent"}
    old = client.post("/demo/orders", json={"quantity": 8}, headers=key)
    new = client.post(
        "/v1/demo/orders",
        json={"quantity": "8.00000"},
        headers=V1_HEADERS | key,
    )
    assert old.status_code == new.status_code == 201
    assert old.json()["quantity"] == 8
    assert old.json()["unit_price"] == "100.00"
    assert old.json()["side"] == "BUY"
    assert new.json()["quantity"] == "8.00000"
    assert new.json()["unit_price"] == "100.0000"
    assert new.json()["side"] == "buy"
    assert old.json()["order_id"] == new.json()["order_id"]
    assert new.headers["location"].startswith("/v1/orders/")
    assert (
        client.get("/demo/account").json()["active_reservations"] == "800.00"
    )
    events = client.get(
        new.headers["location"] + "/timeline", headers=V1_HEADERS
    ).json()
    assert events["pagination"]["next_cursor"] is None
    for event in events["data"]:
        assert event["observed_at"].endswith("Z")
        assert len(event["observed_at"].split(".")[1]) == 4


@pytest.mark.postgres
def test_v1_key_remains_required_bounded_and_permanent(
    client, postgres_engine
):
    for key in (None, "", "k" * 101):
        headers = dict(V1_HEADERS)
        if key is not None:
            headers["Idempotency-Key"] = key
        response = client.post(
            "/v1/demo/orders", json={"quantity": "1"}, headers=headers
        )
        assert response.status_code == 422
        assert "message" in response.json()
    headers = V1_HEADERS | {"Idempotency-Key": "k" * 100}
    first = client.post(
        "/v1/demo/orders", json={"quantity": "1"}, headers=headers
    )
    assert first.status_code == 201
    with Session(postgres_engine) as db, db.begin():
        db.get(IdempotencyRecord, ("demo-client", "k" * 100)).created_at = (
            utc_now() - timedelta(days=2)
        )
    again = client.post(
        "/v1/demo/orders", json={"quantity": "1.00000"}, headers=headers
    )
    assert again.json() == first.json()
    conflict = client.post(
        "/v1/demo/orders", json={"quantity": "2"}, headers=headers
    )
    assert conflict.status_code == 422
    assert "message" in conflict.json()


@pytest.mark.postgres
def test_v1_principal_cannot_impersonate_owner(client, caplog):
    with caplog.at_level("INFO", logger="brokerage_lab.auth_v1"):
        response = client.get(
            "/v1/accounts/other-demo-account/cash",
            headers=V1_HEADERS
            | {
                "LMG-Data-Privacy-Access-Principal": "backend-other-client",
            },
        )
    assert response.status_code == 404
    assert "client_id=demo-client" in caplog.text
    assert "test-demo-key" not in caplog.text


def test_position_cursor_is_stable_scoped_and_exact():
    rows = [
        {"instrument": "C", "quantity": 9999999999999999},
        {"instrument": "A", "quantity": 1},
        {"instrument": "B", "quantity": 2},
    ]
    one = position_page(rows, "account", None, 2)
    assert [row["instrument"] for row in one["data"]] == ["A", "B"]
    cursor = one["pagination"]["next_cursor"]
    two = position_page(rows, "account", cursor, 2)
    assert two["data"][0]["quantity"] == "9999999999999999.00000"
    assert two["pagination"]["next_cursor"] is None
    with pytest.raises(HTTPException):
        position_page(rows, "other-account", cursor, 2)
    with pytest.raises(HTTPException):
        position_page(rows, "account", "invalid", 2)


@pytest.mark.postgres
def test_v1_lists_do_not_change_legacy_shape(client):
    assert client.get("/accounts/demo-account/positions").json() == []
    response = client.get(
        "/v1/accounts/demo-account/positions", headers=V1_HEADERS
    )
    assert response.json() == {"data": [], "pagination": {"next_cursor": None}}
    for limit in (0, 101):
        assert (
            client.get(
                "/v1/accounts/demo-account/positions",
                params={"limit": limit},
                headers=V1_HEADERS,
            ).status_code
            == 422
        )
