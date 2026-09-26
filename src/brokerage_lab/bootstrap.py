"""Migrate as owner, then grant the runtime role only its required rights."""

import os

from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import text

from .config import Settings
from .db import make_engine
from .demo import seed_demo


def main():
    engine = make_engine(Settings().database_url.get_secret_value())
    with engine.begin() as connection:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    seed_demo(engine)
    with engine.begin() as connection:
        exists = connection.scalar(
            text("SELECT 1 FROM pg_roles WHERE rolname = 'brokerage_app'")
        )
        cursor = connection.connection.driver_connection.cursor()
        if not exists:
            cursor.execute(
                sql.SQL("CREATE ROLE brokerage_app LOGIN PASSWORD {}").format(
                    sql.Literal(os.environ["APP_DATABASE_PASSWORD"])
                )
            )
        cursor.close()
        connection.execute(
            text("GRANT USAGE ON SCHEMA public TO brokerage_app")
        )
        connection.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                "IN SCHEMA public TO brokerage_app"
            )
        )
        connection.execute(
            text(
                "REVOKE UPDATE, DELETE ON journal_transactions, "
                "journal_entries, "
                "instrument_movements, processed_executions, "
                "break_history "
                "FROM brokerage_app"
            )
        )
        connection.execute(
            text("REVOKE ALL ON alembic_version FROM brokerage_app")
        )
    engine.dispose()


if __name__ == "__main__":
    main()
