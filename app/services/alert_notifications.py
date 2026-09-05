"""Bounded SMTP digests. SMTP acceptance is not inbox-delivery evidence."""

from datetime import datetime
import hashlib
import json
import re
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..models import OutboxEvent
from ..security import as_utc, utcnow

TOPIC = "ops.alert.digest"


def recipient_digest(recipient):
    if not isinstance(recipient, str) or len(recipient) > 254 or not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", recipient):
        raise ValueError("Configure one plain ASCII operator email address")
    return hashlib.sha256(recipient.encode()).hexdigest()


def safe_rules(items):
    if not isinstance(items, list) or len(items) > 10:
        raise ValueError("Invalid operational summary")
    result = []
    for item in items:
        rule = item.get("rule", item.get("id", ""))
        if (not isinstance(rule, str) or not re.fullmatch(r"[a-z_]{1,40}", rule)
            or item.get("severity") not in {"warning", "critical"}
            or type(item.get("count")) is not int or not 1 <= item["count"] <= 2**63 - 1):
            raise ValueError("Invalid operational summary")
        result.append({"rule": rule, "severity": item["severity"], "count": item["count"]})
    return sorted(result, key=lambda row: row["rule"])


def queue_digest(factory, observation, recipient, *, now=None):
    destination = recipient_digest(recipient)
    rules = safe_rules(observation["items"])
    if not rules:
        return None
    now = as_utc(now or utcnow())
    period = now.strftime("%Y-%m-%dT%H")
    severity = "critical" if any(row["severity"] == "critical" for row in rules) else "warning"
    event_id = str(uuid5(NAMESPACE_URL, f"kunlun/ops-digest/v1/{destination}/{period}/{severity}"))
    observed_at = as_utc(datetime.fromisoformat(observation["observed_at"])).isoformat()
    with factory() as db:
        existing = db.get(OutboxEvent, event_id)
        if existing:
            if existing.topic != TOPIC:
                raise ValueError("Notification identity conflict")
            return event_id
        db.add(OutboxEvent(id=event_id, topic=TOPIC, reference=f"{destination}:{period}:{severity}", status="pending", created_at=now,
            payload_json=json.dumps({"recipient_digest": destination, "observed_at": observed_at, "rules": rules})))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.get(OutboxEvent, event_id)
            if existing is None or existing.topic != TOPIC:
                raise
    return event_id


def next_pending_digest(factory, recipient):
    destination = recipient_digest(recipient)
    with factory() as db:
        return db.scalar(select(OutboxEvent.id).where(OutboxEvent.topic == TOPIC, OutboxEvent.status == "pending",
            OutboxEvent.reference.startswith(destination + ":")).order_by(OutboxEvent.created_at, OutboxEvent.id).limit(1))


def dispatch_digest(factory, event_id, recipient, sender, *, now=None):
    destination = recipient_digest(recipient)
    now = as_utc(now or utcnow())
    with factory() as db:
        row = db.get(OutboxEvent, event_id)
        if row is None or row.topic != TOPIC:
            return "not_found"
        data = json.loads(row.payload_json)
        if data["recipient_digest"] != destination:
            return "recipient_mismatch"
        rules = safe_rules(data["rules"])
        observed_at = as_utc(datetime.fromisoformat(data["observed_at"])).isoformat()
        # Claim and commit BEFORE network I/O. No automatic takeover of sending
        # or unconfirmed records: a stopped worker may already have sent mail.
        data["claim_started_at"] = now.isoformat()
        changed = db.execute(update(OutboxEvent).where(OutboxEvent.id == event_id, OutboxEvent.topic == TOPIC,
            OutboxEvent.status == "pending").values(status="sending", payload_json=json.dumps(data)))
        db.commit()
        if changed.rowcount != 1:
            return "not_claimed"
    try:
        sender.send_operator_alert(recipient, event_id, {"observed_at": observed_at, "rules": rules})
        status = "accepted"
    except Exception:
        status = "unconfirmed"
    with factory() as db:
        db.execute(update(OutboxEvent).where(OutboxEvent.id == event_id, OutboxEvent.topic == TOPIC,
            OutboxEvent.status == "sending").values(status=status))
        db.commit()
    return status


def delivery_projection(row, *, now=None):
    """Metadata-only view. An abandoned claim remains unknown, never resent."""
    data = json.loads(row.payload_json)
    claimed = data.get("claim_started_at")
    state = row.status
    if state == "sending" and claimed and (as_utc(now or utcnow()) - as_utc(datetime.fromisoformat(claimed))).total_seconds() >= 300:
        state = "unconfirmed"
    return {"id": row.id, "period": row.reference.split(":", 1)[1], "status": state, "created_at": as_utc(row.created_at).isoformat(),
            "claim_started_at": claimed, "observed_at": data["observed_at"], "rules": safe_rules(data["rules"])}
