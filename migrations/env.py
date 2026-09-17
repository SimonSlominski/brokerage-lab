"""Alembic uses the same local configuration as the application."""

from alembic import context

from brokerage_lab.config import Settings
from brokerage_lab.db import Base, make_engine

config = context.config


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=Settings().database_url.get_secret_value(),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    run(config.attributes["connection"])
else:
    engine = make_engine(Settings().database_url.get_secret_value())
    try:
        with engine.connect() as connection:
            run(connection)
    finally:
        engine.dispose()
