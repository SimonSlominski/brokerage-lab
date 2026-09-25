"""Order use cases sharing an explicit local transaction."""

from uuid import uuid4

from sqlalchemy import select, text

from .contracts import fingerprint
from .domain import Account, DomainError, Order
from .models import IdempotencyRecord, OrderEvent, OutboxMessage
from .schemas import OrderResponse
from .unit_of_work import UnitOfWork


class AccountNotFoundError(Exception):
    """The requested account does not exist within the caller's scope."""


class OrderNotFoundError(Exception):
    """No order exists within the requested account."""


class IdempotencyConflict(DomainError):
    """The key was already bound to another request."""


def get_account(
    account_id: str, uow: UnitOfWork, *, client_id: str
) -> Account:
    with uow:
        account = uow.accounts.get(account_id, owner_client_id=client_id)
        if account is None:
            raise AccountNotFoundError("Account not found")
        return account


def create_order(
    account_id: str,
    quantity: int,
    uow: UnitOfWork,
    *,
    client_id: str,
    key: str,
    run_id: str | None = None,
    mode: str = "normal",
) -> dict:
    if not key.strip() or len(key) > 128:
        raise DomainError("Idempotency key must contain 1 to 128 characters")
    order = Order.demo_buy(str(uuid4()), account_id, quantity)
    payload_hash = fingerprint(
        dict(
            schema_version=1,
            operation="create_order",
            account_id=account_id,
            instrument=order.instrument,
            side=order.side,
            currency="EUR",
            quantity=order.quantity,
        )
    )
    with uow:
        # Serialize this client/key across accounts; uniqueness is also in SQL.
        # Hash collisions only serialize unrelated work; they never merge keys.
        lock_hash = fingerprint(dict(client_id=client_id, key=key))[:15]
        uow.session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": int(lock_hash, 16)},
        )
        account = uow.accounts.get_for_update(
            account_id, owner_client_id=client_id
        )
        if account is None:
            raise AccountNotFoundError("Account not found")
        record = uow.session.get(IdempotencyRecord, (client_id, key))
        if record:
            if record.fingerprint != payload_hash:
                raise IdempotencyConflict("Idempotency key payload conflict")
            return record.response
        reserved_account = account.reserve(order)
        uow.orders.add(order)
        uow.accounts.add_reservation(
            account.id, reserved_account.reservations[-1]
        )
        response = OrderResponse.from_order(order).model_dump(mode="json")
        uow.session.add(
            IdempotencyRecord(
                client_id=client_id,
                key=key,
                fingerprint=payload_hash,
                account_id=account_id,
                order_id=order.id,
                response=response,
                status_code=201,
            )
        )
        uow.session.add(
            OutboxMessage(
                id=str(uuid4()),
                order_id=order.id,
                run_id=run_id,
                mode=mode,
            )
        )
        uow.session.add(
            OrderEvent(
                id=str(uuid4()),
                order_id=order.id,
                kind="RESERVED",
                source="local",
                detail={"amount": str(order.reservation_amount.amount)},
            )
        )
        uow.commit()
        return response


def submit_order(
    account_id: str,
    quantity: int,
    uow: UnitOfWork,
    *,
    client_id: str,
    key: str | None = None,
) -> Order:
    # Internal convenience for tests; HTTP callers must supply a stable key.
    response = create_order(
        account_id,
        quantity,
        uow,
        client_id=client_id,
        key=key or str(uuid4()),
    )
    return Order.demo_buy(
        response["order_id"], account_id, response["quantity"]
    )


def get_order(
    account_id: str, order_id: str, uow: UnitOfWork, *, client_id: str
) -> Order:
    with uow:
        if uow.accounts.get(account_id, owner_client_id=client_id) is None:
            raise OrderNotFoundError("Order not found")
        order = uow.orders.get(order_id)
        if order is None or order.account_id != account_id:
            raise OrderNotFoundError("Order not found")
        return order


def timeline(db, order_id: str) -> list[dict]:
    stmt = (
        select(OrderEvent)
        .where(OrderEvent.order_id == order_id)
        .order_by(OrderEvent.observed_at, OrderEvent.id)
    )
    return [
        dict(
            kind=event.kind,
            source=event.source,
            detail=event.detail,
            observed_at=event.observed_at.isoformat(),
        )
        for event in db.scalars(stmt)
    ]
