"""Opt-in PostgreSQL tests use disposable schemas, never the application schema."""

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
    parser.addoption("--postgres", action="store_true", help="Run PostgreSQL integration tests")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--postgres"):
        for item in items:
            if item.get_closest_marker("postgres"):
                item.add_marker(pytest.mark.skip(reason="Use --postgres to run PostgreSQL integration tests"))


@pytest.fixture
def postgres_engine():
    url = os.getenv("TEST_DATABASE_URL") or Settings().database_url.get_secret_value()
    parsed = make_url(url)
    if parsed.drivername != "postgresql+psycopg":
        pytest.fail("Integration tests require postgresql+psycopg")
    schema = "test_brokerage_" + uuid4().hex
    admin = create_engine(parsed, connect_args={"connect_timeout": 3})
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(parsed, connect_args={"connect_timeout": 3, "options": f"-csearch_path={schema}"})
    try:
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
