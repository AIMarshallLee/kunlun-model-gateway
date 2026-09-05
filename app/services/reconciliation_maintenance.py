"""Fail-closed recovery for durable external-operation leases."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from ..models import ModelRequest
from ..security import as_utc, utcnow


def recover_stale_model_reservations(
    session: Session,
    *,
    lease_seconds: int = 300,
    now: datetime | None = None,
) -> int:
    """Move abandoned reservations into manual reconciliation without release.

    A crash can occur before an attempt row is committed even when an upstream
    accepted the request. Consequently the sweeper never refunds or settles a
    hold automatically; it only makes the item discoverable to an operator.
    The caller owns the transaction.
    """

    if not 60 <= lease_seconds <= 86_400:
        raise ValueError("lease_seconds must be between 60 and 86400")
    recovered_at = as_utc(now or utcnow())
    cutoff = recovered_at - timedelta(seconds=lease_seconds)
    result = session.execute(
        update(ModelRequest)
        .where(
            ModelRequest.status == "reserved",
            ModelRequest.created_at <= cutoff,
        )
        .values(
            status="pending_reconciliation",
            cost_state="pending_reconciliation",
            failure_category="reservation_lease_expired",
            completed_at=recovered_at,
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)
