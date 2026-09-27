"""Operator access and honest scenario failure reporting."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from brokerage_lab.api import create_app
from brokerage_lab.models import ScenarioRun


@pytest.mark.parametrize("path", ["/lab", "/metrics"])
def test_operator_credentials_required(path):
    with TestClient(create_app(engine=MagicMock())) as client:
        assert client.get(path).status_code == 401
        assert (
            client.get(
                path, headers={"X-API-Key": "test-demo-key"}
            ).status_code
            == 401
        )


def test_fault_endpoint_disabled_outside_development(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with TestClient(create_app(engine=MagicMock())) as client:
        response = client.post(
            "/lab/runs",
            json={"scenario": "lost_response"},
            headers={"X-Operator-Key": "test-operator-key"},
        )
        assert response.status_code == 403


@pytest.mark.postgres
def test_failed_scenario_does_not_claim_success(
    client, postgres_engine, monkeypatch
):
    from brokerage_lab import scenarios

    monkeypatch.setenv("APP_ENV", "development")

    def broken(*args, **kwargs):
        raise RuntimeError("Deliberate verification failure")

    monkeypatch.setattr(scenarios, "create_order", broken)
    result = client.post(
        "/lab/runs",
        json={"scenario": "retry_storm"},
        headers={"X-Operator-Key": "test-operator-key"},
    )
    assert result.status_code == 201
    assert result.json()["status"] == "FAILED"
    with Session(postgres_engine) as db:
        assert db.scalar(select(ScenarioRun.status)) == "FAILED"
    panel = client.get("/lab", headers={"X-Operator-Key": "test-operator-key"})
    assert panel.status_code == 200
    assert "FAILED" in panel.text


@pytest.mark.parametrize("path", ["/lab/runs", "/lab/runs/stream"])
def test_scenario_submission_requires_operator(path):
    with TestClient(create_app(engine=MagicMock())) as client:
        assert (
            client.post(path, json={"scenario": "lost_response"}).status_code
            == 401
        )


def test_stream_disabled_outside_development(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with TestClient(create_app(engine=MagicMock())) as client:
        response = client.post(
            "/lab/runs/stream",
            json={"scenario": "lost_response"},
            headers={"X-Operator-Key": "test-operator-key"},
        )
        assert response.status_code == 403


@pytest.mark.postgres
@pytest.mark.parametrize(
    "scenario",
    [
        "lost_response",
        "retry_storm",
        "duplicate_execution",
        "amount_mismatch",
    ],
)
def test_stream_checkpoints_match_persisted_account(
    client, postgres_engine, partner_http, monkeypatch, scenario
):
    import json

    from brokerage_lab import scenarios

    monkeypatch.setattr(scenarios, "partner_transport", lambda: partner_http)
    response = client.post(
        "/lab/runs/stream",
        json={"scenario": scenario},
        headers={"X-Operator-Key": "test-operator-key"},
    )
    assert response.status_code == 200
    messages = [json.loads(line) for line in response.text.splitlines()]
    steps = [item["step"] for item in messages if item["type"] == "step"]
    final = messages[-1]["result"]
    assert final["status"] == "PASSED"
    assert steps == final["evidence"]["steps"]
    assert steps[0]["snapshot"]["posted_cash"] == "1000.00"
    assert steps[0]["snapshot"]["share_quantity"] == 0
    assert steps[-1]["snapshot"] == final["evidence"]["final"]
    assert steps[-1]["snapshot"]["share_quantity"] == (
        0 if scenario == "amount_mismatch" else 8
    )
    if scenario == "lost_response":
        fault = next(step for step in steps if step["fault"] == "response")
        assert fault["snapshot"]["reserved"] == "800.00"
        assert fault["snapshot"]["share_quantity"] == 0
    with Session(postgres_engine) as db:
        row = db.get(ScenarioRun, final["run_id"])
        assert row.evidence["steps"] == steps


@pytest.mark.postgres
def test_stream_failure_preserves_observed_checkpoints(
    client, postgres_engine, monkeypatch
):
    import json

    from brokerage_lab import scenarios

    monkeypatch.setenv("APP_ENV", "development")

    def broken(*args, **kwargs):
        raise RuntimeError("Injected failure")

    monkeypatch.setattr(scenarios, "create_order", broken)
    response = client.post(
        "/lab/runs/stream",
        json={"scenario": "lost_response"},
        headers={"X-Operator-Key": "test-operator-key"},
    )
    messages = [json.loads(line) for line in response.text.splitlines()]
    result = messages[-1]["result"]
    assert result["status"] == "FAILED"
    assert len(result["evidence"]["steps"]) == 1
    assert "final" not in result["evidence"]


@pytest.mark.postgres
def test_panel_embedded_evidence_cannot_inject_script(postgres_engine):
    from brokerage_lab.panel import render_panel

    with Session(postgres_engine) as db, db.begin():
        db.add(
            ScenarioRun(
                id="html-test",
                scenario="lost_response",
                seed=42,
                status="FAILED",
                evidence={"detail": "</script><img src=x>"},
            )
        )
    html = render_panel(postgres_engine)
    assert "</script><img src=x>" not in html
    assert "\\u003c/script>" in html
