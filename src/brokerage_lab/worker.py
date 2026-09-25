"""Leased delivery with short claim and result transactions."""

import argparse
import logging
import os
import time
from datetime import timedelta
from uuid import uuid4

import httpx
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import Settings
from .contracts import PartnerOrderCreate, utc_now
from .db import AccountRow, OrderRow, make_engine
from .executions import ExecutionConflict, apply_partner_result, record_event
from .models import OutboxMessage
from .transport import PartnerTransport

logger = logging.getLogger(__name__)


def claim(
    engine,
    *,
    run_id: str | None = None,
    lease_seconds: float = 10,
    max_attempts: int = 5,
) -> dict | None:
    now = utc_now()
    with Session(engine) as db, db.begin():
        stmt = (
            select(OutboxMessage)
            .where(
                OutboxMessage.run_id == run_id,
                or_(
                    (OutboxMessage.status == "PENDING")
                    & (OutboxMessage.available_at <= now),
                    (OutboxMessage.status == "CLAIMED")
                    & (OutboxMessage.lease_until <= now),
                ),
            )
            .order_by(OutboxMessage.created_at)
            .with_for_update(skip_locked=True)
        )
        message = db.scalar(stmt.limit(1))
        if message is None:
            return None
        order = db.get(OrderRow, message.order_id)
        db.scalar(
            select(AccountRow)
            .where(AccountRow.id == order.account_id)
            .with_for_update()
        )
        db.refresh(order)
        if order.business_status in {"FILLED", "REJECTED"}:
            message.status = "DONE"
            return None
        if message.attempts >= max_attempts:
            message.status = "ERROR"
            message.last_error = "Retry budget exhausted; investigate outcome"
            order.communication_status = "UNKNOWN"
            record_event(db, order.id, "RETRY_EXHAUSTED", source="worker")
            return None
        message.status = "CLAIMED"
        message.attempts += 1
        message.lease_token = str(uuid4())
        message.lease_until = now + timedelta(seconds=lease_seconds)
        order.communication_status = "IN_FLIGHT"
        record_event(
            db,
            order.id,
            "CLAIMED",
            {
                "attempt": message.attempts,
                "lease_token": message.lease_token,
            },
            source="worker",
        )
        request = PartnerOrderCreate(
            client_order_id=order.id,
            account_id=order.account_id,
            quantity=order.quantity,
            mode=message.mode,
            run_id=message.run_id,
        )
        return dict(
            id=message.id,
            token=message.lease_token,
            attempt=message.attempts,
            request=request,
        )


def finish(
    engine,
    claimed: dict,
    result=None,
    *,
    error: str | None = None,
    permanent: bool = False,
    retry_delay: float | None = None,
) -> bool:
    with Session(engine) as db, db.begin():
        message = db.scalar(
            select(OutboxMessage)
            .where(OutboxMessage.id == claimed["id"])
            .with_for_update()
        )
        if (
            message is None
            or message.status != "CLAIMED"
            or message.lease_token != claimed["token"]
            or message.lease_until <= utc_now()
        ):
            return False
        if result is not None:
            if result.client_order_id != message.order_id:
                raise ExecutionConflict("Partner returned another order")
            apply_partner_result(db, result)
            message.status = (
                "DONE" if result.status != "ACCEPTED" else "PENDING"
            )
            message.last_error = None
            message.available_at = utc_now() + timedelta(seconds=1)
        else:
            order = db.get(OrderRow, message.order_id)
            db.scalar(
                select(AccountRow)
                .where(AccountRow.id == order.account_id)
                .with_for_update()
            )
            db.refresh(order)
            if order.business_status in {"FILLED", "REJECTED"}:
                message.status = "DONE"
            else:
                order.communication_status = "UNKNOWN"
                message.status = (
                    "ERROR"
                    if permanent or message.attempts >= 5
                    else "PENDING"
                )
                message.last_error = error or "Partner outcome is unknown"
                delay = (
                    retry_delay
                    if retry_delay is not None
                    else min(2**message.attempts, 30)
                )
                message.available_at = utc_now() + timedelta(seconds=delay)
                record_event(
                    db,
                    order.id,
                    "UNKNOWN",
                    {
                        "reason": message.last_error,
                        "attempt": message.attempts,
                    },
                    source="worker",
                )
        message.lease_token = None
        message.lease_until = None
        return True


def process_one(
    engine,
    transport,
    *,
    run_id: str | None = None,
    crash_after_send: bool = False,
    lease_seconds: float = 10,
    retry_delay: float | None = None,
) -> bool:
    claimed = claim(engine, run_id=run_id, lease_seconds=lease_seconds)
    if claimed is None:
        return False
    logger.info(
        "delivery_claimed order_id=%s run_id=%s attempt=%s",
        claimed["request"].client_order_id,
        run_id,
        claimed["attempt"],
    )
    try:
        result = None
        if claimed["attempt"] > 1:
            result = transport.lookup(claimed["request"].client_order_id)
        result = result or transport.submit(claimed["request"])
        if crash_after_send:
            # Exit after remote commit and before local effects.
            os._exit(86)
        finish(engine, claimed, result)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        finish(
            engine,
            claimed,
            error=f"Partner HTTP {code}; outcome unresolved",
            permanent=400 <= code < 500 and code not in {408, 429},
            retry_delay=retry_delay,
        )
    except (httpx.HTTPError, ValidationError, ExecutionConflict, ValueError):
        finish(
            engine,
            claimed,
            error="Partner result unavailable or inconsistent",
            retry_delay=retry_delay,
        )
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--crash-after-send", action="store_true")
    parser.add_argument("--lease-seconds", type=float, default=10)
    args = parser.parse_args()
    settings = Settings()
    if args.crash_after_send and settings.app_env != "development":
        parser.error("Crash injection requires development mode")
    logging.basicConfig(level=logging.INFO)
    engine = make_engine(settings.database_url.get_secret_value())
    transport = PartnerTransport(
        settings.partner_url, settings.partner_api_key.get_secret_value()
    )
    try:
        while True:
            worked = process_one(
                engine,
                transport,
                run_id=args.run_id,
                crash_after_send=args.crash_after_send,
                lease_seconds=args.lease_seconds,
            )
            if args.once:
                break
            if not worked:
                time.sleep(0.5)
    finally:
        transport.close()
        engine.dispose()


if __name__ == "__main__":
    main()
