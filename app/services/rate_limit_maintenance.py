"""Retention maintenance for database-backed rate-limit windows."""

from __future__ import annotations

import time

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..models import AuthRateLimitCounter, RateLimitCounter


def cleanup_rate_limit_counters(
    session: Session,
    *,
    retention_days: int = 7,
    now_epoch: int | None = None,
) -> dict[str, int]:
    """Delete windows older than ``retention_days`` from both counter tables.

    ``window_epoch`` is a Unix epoch in minutes, matching the request path.
    The comparison is strict so a window exactly on the retention boundary is
    retained.  The caller owns the transaction and must commit on success.
    """

    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    current = int(time.time() if now_epoch is None else now_epoch)
    cutoff_window = current // 60 - retention_days * 24 * 60
    counts: dict[str, int] = {}
    for model in (RateLimitCounter, AuthRateLimitCounter):
        result = session.execute(delete(model).where(model.window_epoch < cutoff_window))
        counts[model.__tablename__] = int(result.rowcount or 0)
    return counts
