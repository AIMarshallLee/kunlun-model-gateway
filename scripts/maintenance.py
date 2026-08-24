#!/usr/bin/env python3
"""Periodic non-business-data maintenance jobs."""

from __future__ import annotations

import argparse
import logging
import time

from app.config import Settings
from app.db import build_engine, build_session_factory
from app.services.rate_limit_maintenance import cleanup_rate_limit_counters
from app.services.reconciliation_maintenance import recover_stale_model_reservations


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
            with session_factory() as session:
                counts = cleanup_rate_limit_counters(session, retention_days=args.retention_days)
                counts["stale_model_reservations"] = recover_stale_model_reservations(
                    session,
                    lease_seconds=settings.model_reservation_lease_seconds,
                )
                session.commit()
            logging.info("rate-limit retention complete: %s", counts)
            if args.once:
                return 0
            time.sleep(args.interval_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
