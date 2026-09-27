"""PostgreSQL tests use disposable schemas, never application tables."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from brokerage_lab.config import Settings


def pytest_addoption(parser):
    parser.addoption(
        "--postgres",
        action="store_true",
        help="Run PostgreSQL integration tests",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--postgres"):
        for item in items:
            if item.get_closest_marker("postgres"):
                item.add_marker(
                    pytest.mark.skip(
                        reason="Use --postgres for database tests"
                    )
                )


@pytest.fixture
def postgres_engine():
    url = (
        os.getenv("TEST_DATABASE_URL")
        or Settings().database_url.get_secret_value()
    )
    parsed = make_url(url)
    if parsed.drivername != "postgresql+psycopg":
        pytest.fail("Integration tests require postgresql+psycopg")
    schema = "test_brokerage_" + uuid4().hex
    admin = create_engine(parsed, connect_args={"connect_timeout": 3})
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        parsed,
        connect_args={
            "connect_timeout": 3,
            "options": f"-csearch_path={schema}",
        },
    )
    try:
        config = Config(
            str(Path(__file__).resolve().parents[1] / "alembic.ini")
        )
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture(autouse=True)
def demo_auth_environment(monkeypatch):
    monkeypatch.setenv("DEMO_API_KEY", "test-demo-key")
    monkeypatch.setenv("OTHER_DEMO_API_KEY", "test-other-key")
    monkeypatch.setenv("OPERATOR_API_KEY", "test-operator-key")


@pytest.fixture
def db(postgres_engine):
    from sqlalchemy.orm import Session

    with Session(postgres_engine) as db:
        yield db
        db.rollback()


@pytest.fixture
def client(postgres_engine):
    from fastapi.testclient import TestClient

    from brokerage_lab.api import create_app
    from brokerage_lab.demo import seed_demo

    seed_demo(postgres_engine)
    with TestClient(
        create_app(engine=postgres_engine),
        headers={"X-API-Key": "test-demo-key"},
    ) as client:
        yield client


@pytest.fixture
def partner_engine(postgres_engine):
    from sqlalchemy.engine import make_url

    schema = "test_partner_" + uuid4().hex
    url = make_url(postgres_engine.url)
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        url, connect_args={"options": f"-csearch_path={schema}"}
    )
    try:
        config = Config("partner-alembic.ini")
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def partner_http(partner_engine, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    import socket
    import threading
    import time

    import uvicorn

    from brokerage_lab.partner import create_app
    from brokerage_lab.transport import PartnerTransport

    socket_ = socket.socket()
    socket_.bind(("127.0.0.1", 0))
    port = socket_.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(engine=partner_engine, token="test-partner-key"),
            log_level="error",
            lifespan="on",
        )
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [socket_]}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    transport = PartnerTransport(
        f"http://127.0.0.1:{port}", "test-partner-key", timeout=0.15
    )
    try:
        yield transport
    finally:
        transport.close()
        server.should_exit = True
        thread.join(timeout=5)
        socket_.close()
