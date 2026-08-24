"""Append-only, double-entry wallet journal helpers."""

from __future__ import annotations

from collections.abc import Iterable
import uuid

from sqlalchemy.orm import Session

from ..models import LedgerEntry, LedgerTransaction


CUSTOMER_AVAILABLE = "customer_available"
CUSTOMER_RESERVED = "customer_reserved"
PLATFORM_CLEARING = "platform_clearing"
PLATFORM_REVENUE = "platform_revenue"
PLATFORM_RISK = "platform_risk"
PLATFORM_LOSS = "platform_loss"
PLATFORM_PROVIDER_EXPENSE = "platform_provider_expense"
PLATFORM_PROVIDER_PAYABLE = "platform_provider_payable"


def post_transaction(
    session: Session,
    *,
    user_id: str,
    kind: str,
    reference: str,
    idempotency_key: str,
    entries: Iterable[tuple[str, int]],
) -> LedgerTransaction:
    normalized = [(account, int(amount)) for account, amount in entries if int(amount) != 0]
    if len(normalized) < 2 or sum(amount for _, amount in normalized) != 0:
        raise RuntimeError("账本分录不平衡")
    transaction = LedgerTransaction(
        id=str(uuid.uuid4()),
        user_id=user_id,
        kind=kind,
        reference=reference,
        idempotency_key=idempotency_key,
    )
    session.add(transaction)
    # The models intentionally have no mutable ORM relationship. Flush the
    # immutable journal header first so foreign-key ordering is explicit.
    session.flush()
    for account, amount in normalized:
        session.add(LedgerEntry(
            id=str(uuid.uuid4()),
            transaction_id=transaction.id,
            user_id=user_id,
            account=account,
            amount_microusd=amount,
        ))
    return transaction
