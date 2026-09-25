from alembic import context

from brokerage_lab.config import Settings
from brokerage_lab.db import make_engine
from brokerage_lab.partner_models import PartnerBase


def run(connection):
    context.configure(
        connection=connection, target_metadata=PartnerBase.metadata
    )
    with context.begin_transaction():
        context.run_migrations()


if context.config.attributes.get("connection") is not None:
    run(context.config.attributes["connection"])
else:
    engine = make_engine(Settings().database_url.get_secret_value())
    try:
        with engine.connect() as connection:
            run(connection)
    finally:
        engine.dispose()
