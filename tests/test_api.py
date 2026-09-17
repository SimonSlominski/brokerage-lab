"""HTTP behavior without PostgreSQL, including sanitized failure responses."""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from brokerage_lab.api import create_app


def test_liveness_and_openapi_without_database():
    with TestClient(create_app(engine=MagicMock())) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
        assert "/demo/account" in schema["paths"]
        assert "CashResponse" in schema["components"]["schemas"]


def test_readiness_hides_database_errors():
    engine = MagicMock()
    engine.connect.side_effect = OperationalError("hidden-database-details", {}, Exception("secret"))
    with TestClient(create_app(engine=engine)) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "Database is not ready"}
        assert "secret" not in response.text
