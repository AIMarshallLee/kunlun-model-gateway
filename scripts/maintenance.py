#!/usr/bin/env python3
"""Periodic non-business-data maintenance jobs."""

from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy import text

from app.config import Settings
from app.db import build_engine, build_session_factory
from app.services.rate_limit_maintenance import cleanup_rate_limit_counters
from app.services.reconciliation_maintenance import recover_stale_model_reservations


MAINTENANCE_ADVISORY_LOCK_ID = 1_267_428_844


def run_once(
    settings: Settings,
    session_factory,
    *,
    retention_days: int = 7,
) -> dict[str, int] | None:
    with session_factory() as session:
        if session.bind.dialect.name == "postgresql" and not session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
            {"lock_id": MAINTENANCE_ADVISORY_LOCK_ID},
        ):
            session.rollback()
            return None
        counts = cleanup_rate_limit_counters(session, retention_days=retention_days)
        counts["stale_model_reservations"] = recover_stale_model_reservations(
            session,
            lease_seconds=settings.model_reservation_lease_seconds,
        )
        session.commit()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=86_400)
    parser.add_argument("--retention-days", type=int, default=7)
    args = parser.parse_args(argv)
    if args.interval_seconds < 1:
        parser.error("--interval-seconds must be positive")
    settings = Settings.from_env()
    engine = build_engine(settings.database_url)
    session_factory = build_session_factory(engine)
    logging.basicConfig(level=logging.INFO)
    try:
        while True:
            counts = run_once(
                settings,
                session_factory,
                retention_days=args.retention_days,
            )
            if counts is None:
                logging.info("maintenance skipped: another invocation owns the database lock")
                if args.once:
                    return 0
                time.sleep(args.interval_seconds)
                continue
            logging.info("rate-limit retention complete: %s", counts)
            if args.once:
                return 0
            time.sleep(args.interval_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
