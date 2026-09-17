"""Seed and explicitly reset only the known local demo account."""

import argparse
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from brokerage_lab.config import Settings
from brokerage_lab.db import (
    AccountRow,
    InstrumentRow,
    OrderRow,
    ReservationRow,
    make_engine,
)
from brokerage_lab.domain import DEMO_INSTRUMENT, DEMO_UNIT_PRICE

DEMO_ACCOUNT_ID = "demo-account"


def seed_demo(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        if session.get(InstrumentRow, DEMO_INSTRUMENT) is None:
            session.add(InstrumentRow(id=DEMO_INSTRUMENT, unit_price=DEMO_UNIT_PRICE, currency="EUR"))
        if session.get(AccountRow, DEMO_ACCOUNT_ID) is None:
            session.add(AccountRow(id=DEMO_ACCOUNT_ID, posted_cash=Decimal("1000.00"), currency="EUR"))


def reset_demo(engine: Engine, *, app_env: str, confirmed: bool) -> None:
    if app_env != "development" or not confirmed:
        raise ValueError("Demo reset requires development mode and explicit confirmation")
    with Session(engine) as session, session.begin():
        account = session.scalar(select(AccountRow).where(AccountRow.id == DEMO_ACCOUNT_ID).with_for_update())
        if account is None:
            session.add(AccountRow(id=DEMO_ACCOUNT_ID, posted_cash=Decimal("1000.00"), currency="EUR"))
        else:
            session.execute(delete(ReservationRow).where(ReservationRow.account_id == DEMO_ACCOUNT_ID))
            session.execute(delete(OrderRow).where(OrderRow.account_id == DEMO_ACCOUNT_ID))
            account.posted_cash = Decimal("1000.00")
        if session.get(InstrumentRow, DEMO_INSTRUMENT) is None:
            session.add(InstrumentRow(id=DEMO_INSTRUMENT, unit_price=DEMO_UNIT_PRICE, currency="EUR"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage local demo data")
    parser.add_argument("action", choices=["seed", "reset"])
    parser.add_argument("--confirm", action="store_true", help="Confirm resetting only the demo account")
    args = parser.parse_args()
    settings = Settings()
    if settings.app_env != "development":
        parser.error("Demo commands require development mode")
    engine = make_engine(settings.database_url.get_secret_value())
    try:
        if args.action == "seed":
            seed_demo(engine)
        else:
            if not args.confirm:
                parser.error("Reset requires --confirm")
            reset_demo(engine, app_env=settings.app_env, confirmed=args.confirm)
    finally:
        engine.dispose()
    print("Demo data is ready")


if __name__ == "__main__":
    main()
