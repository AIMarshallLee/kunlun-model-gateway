"""One externally scheduled alert digest tick. Preview by default; no migrations."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.db import build_engine, build_session_factory
from app.db_guards import SCHEMA_HEAD, assert_schema_revision
from app.ops_alerts import collect_alerts
from app.services.alert_notifications import delivery_projection, dispatch_digest, next_pending_digest, queue_digest, recipient_digest
from app.models import OutboxEvent
from app.services.identity import SmtpEmailSender
from app.services.platform_credentials import SupabasePlatformVault, platform_contract_errors
from scripts.preflight import _runtime_permission_errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Explicitly queue/send; also requires KUNLUN_ALERT_NOTIFICATIONS_ENABLED=true")
    args = parser.parse_args(argv)
    engine = executor = None
    try:
        if args.send and os.getenv("KUNLUN_ALERT_NOTIFICATIONS_ENABLED") != "true":
            raise ValueError("Notification sending is disabled")
        settings = Settings.from_env()
        if settings.environment != "production" or settings.gateway_mode != "managed_gateway" or settings.vault_backend != "supabase_vault":
            raise ValueError("Requires production safety configuration, managed gateway and Supabase Vault")
        recipient = os.getenv("KUNLUN_ALERT_RECIPIENT", "")
        sender = None
        if args.send:
            recipient_digest(recipient)
            sender = SmtpEmailSender(settings.smtp_url, from_address=settings.email_from, public_base_url=settings.public_base_url)
        engine = build_engine(settings.database_url)
        executor = build_engine(settings.vault_executor_database_url)
        assert_schema_revision(engine, SCHEMA_HEAD)
        if engine.dialect.name != "postgresql" or _runtime_permission_errors(engine, "kunlun_runtime") or platform_contract_errors(engine, executor):
            raise ValueError("Notification database boundary not verified")
        factory = build_session_factory(engine)
        with factory() as db:
            observation = collect_alerts(db, settings, SupabasePlatformVault(executor))
        if not args.send:
            print(json.dumps({"mode": "preview", "active_rules": len(observation["items"]), "writes": 0, "smtp_attempts": 0}))
            return 0
        current_id = queue_digest(factory, observation, recipient)
        event_id = next_pending_digest(factory, recipient) or current_id
        status = dispatch_digest(factory, event_id, recipient, sender) if event_id else "no_active_alerts"
        if status == "not_claimed":
            with factory() as db:
                status = delivery_projection(db.get(OutboxEvent, event_id))["status"]
        print(json.dumps({"notification_id": event_id, "status": status, "inbox_delivery_verified": False}))
        return 0 if status in {"accepted", "no_active_alerts", "sending"} else 1
    except Exception:
        print("Alert notification tick failed; outcome may be unknown. Inspect notification records. No credentials are included.", file=sys.stderr)
        return 1
    finally:
        if executor is not None:
            executor.dispose()
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
