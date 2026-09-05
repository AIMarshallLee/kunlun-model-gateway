from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json

import pytest
from sqlalchemy import select

from app.models import OutboxEvent
from app.security import utcnow
from tests.test_managed_gateway import managed


def observation(severity="warning"):
    return {"observed_at": utcnow().isoformat(), "items": [{"id": "model_reconciliation", "severity": severity,
        "count": 2, "evidence": {"private": "must not enter email"}, "acknowledgement": {"actor": "private-operator"}}]}


def test_digest_deduplicates_and_only_preserves_safe_summary(managed):
    from app.services.alert_notifications import queue_digest
    client, *_ = managed
    factory = client.app.state.SessionLocal
    now = utcnow().replace(minute=0, second=0, microsecond=0)
    first = queue_digest(factory, observation(), "ops@example.invalid", now=now)
    assert queue_digest(factory, observation(), "ops@example.invalid", now=now + timedelta(minutes=1)) == first
    assert queue_digest(factory, observation("critical"), "ops@example.invalid", now=now) != first
    assert queue_digest(factory, observation(), "ops@example.invalid", now=now + timedelta(hours=1)) != first
    with factory() as db:
        record = db.get(OutboxEvent, first)
        assert record.status == "pending"
        assert not any(private in record.payload_json for private in ("must not", "private-operator", "ops@example.invalid"))
        assert json.loads(record.payload_json)["rules"][0] == {"rule": "model_reconciliation", "severity": "warning", "count": 2}


def test_empty_observation_does_not_queue_and_invalid_recipient_rejected(managed):
    from app.services.alert_notifications import queue_digest
    factory = managed[0].app.state.SessionLocal
    assert queue_digest(factory, {"items": []}, "ops@example.invalid") is None
    for recipient in ("a@b.com\nBcc: victim@b.com", "a@b.com,b@c.com", ""):
        with pytest.raises(ValueError):
            queue_digest(factory, observation(), recipient)


def test_concurrent_delivery_only_sends_once_and_commits_before_smtp(managed):
    from app.services.alert_notifications import dispatch_digest, queue_digest
    factory = managed[0].app.state.SessionLocal
    event_id = queue_digest(factory, observation(), "ops@example.invalid")
    sent = []
    class Sender:
        def send_operator_alert(self, recipient, notification_id, summary):
            with factory() as db:
                assert db.get(OutboxEvent, event_id).status == "sending"
            sent.append((recipient, notification_id, summary))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: dispatch_digest(factory, event_id, "ops@example.invalid", Sender()), range(4)))
    assert len(sent) == 1 and results.count("accepted") == 1
    with factory() as db:
        assert db.get(OutboxEvent, event_id).status == "accepted"


def test_uncertain_delivery_is_not_automatically_retried_or_redirected(managed):
    from app.services.alert_notifications import dispatch_digest, queue_digest
    factory = managed[0].app.state.SessionLocal
    event_id = queue_digest(factory, observation(), "ops@example.invalid")
    attempts = []
    class Sender:
        def send_operator_alert(self, *args):
            attempts.append(args)
            raise RuntimeError("SMTP secret/account details must be discarded")
    assert dispatch_digest(factory, event_id, "other@example.invalid", Sender()) == "recipient_mismatch"
    assert attempts == []
    assert dispatch_digest(factory, event_id, "ops@example.invalid", Sender()) == "unconfirmed"
    assert dispatch_digest(factory, event_id, "ops@example.invalid", Sender()) == "not_claimed"
    assert len(attempts) == 1
    with factory() as db:
        assert "SMTP secret" not in db.get(OutboxEvent, event_id).payload_json


def test_restart_can_find_old_unclaimed_mail_but_does_not_take_over_sending(managed):
    from app.services.alert_notifications import delivery_projection, next_pending_digest, queue_digest
    factory = managed[0].app.state.SessionLocal
    event_id = queue_digest(factory, observation(), "ops@example.invalid", now=utcnow() - timedelta(hours=2))
    assert next_pending_digest(factory, "other@example.invalid") is None
    assert next_pending_digest(factory, "ops@example.invalid") == event_id
    with factory() as db:
        row = db.get(OutboxEvent, event_id); row.status = "sending"
        data = json.loads(row.payload_json); data["claim_started_at"] = (utcnow() - timedelta(minutes=6)).isoformat()
        row.payload_json = json.dumps(data); db.commit()
        assert delivery_projection(row)["status"] == "unconfirmed"
    assert next_pending_digest(factory, "ops@example.invalid") is None


def test_operator_status_is_scoped_and_does_not_leak_recipient(managed):
    from app.services.alert_notifications import queue_digest
    from tests.test_ops_console import operator
    client, auth, *_ = managed
    queue_digest(client.app.state.SessionLocal, observation(), "ops@example.invalid")
    assert client.get("/ops/notifications", headers=auth).status_code == 401
    result = client.get("/ops/notifications", headers=operator("alerts:read"))
    assert result.status_code == 200 and result.json()["pagination"]["total"] == 1
    assert "recipient_digest" not in result.text and "ops@example.invalid" not in result.text


def test_smtp_digest_is_plain_metadata_with_stable_message_id_and_tls():
    from uuid import uuid4
    from app.services.identity import IdentityError, SmtpEmailSender
    calls = []
    class SMTP:
        refused = False
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ehlo(self): calls.append("ehlo")
        def starttls(self, **kwargs): calls.append("tls")
        def login(self, *args): calls.append("login")
        def send_message(self, message):
            calls.append(message)
            return {"ops@example.invalid": "refused"} if self.refused else {}
    sender = SmtpEmailSender("smtp://fake:inert@smtp.example.invalid", from_address="sender@example.invalid",
        public_base_url="https://gateway.example.invalid", smtp_factory=SMTP)
    event_id = str(uuid4())
    sender.send_operator_alert("ops@example.invalid", event_id,
        {"observed_at": utcnow().isoformat(), "rules": [{"rule": "model_reconciliation", "severity": "warning", "count": 2}]})
    message = calls[-1]
    assert calls[:4] == ["ehlo", "tls", "ehlo", "login"]
    assert event_id in message["Message-ID"] and "/ops/console" in message.get_content()
    assert "token=" not in message.get_content()
    SMTP.refused = True
    with pytest.raises(IdentityError, match="邮件发送失败"):
        sender.send_verification("ops@example.invalid", "inert-token")


def test_send_switch_is_checked_before_reading_credentials(monkeypatch, capsys):
    from scripts import alert_notifications
    monkeypatch.delenv("KUNLUN_ALERT_NOTIFICATIONS_ENABLED", raising=False)
    monkeypatch.setattr(alert_notifications.Settings, "from_env", lambda: pytest.fail("must not access configuration"))
    assert alert_notifications.main(["--send"]) == 1
    assert "failed" in capsys.readouterr().err


def test_tick_preview_writes_nothing_and_send_reports_acceptance_not_delivery(managed, monkeypatch, capsys):
    from types import SimpleNamespace
    from scripts import alert_notifications as tick
    from tests.test_managed_gateway import ready_call
    client, _, _ = ready_call(managed)
    settings = client.app.state.settings
    settings.vault_backend = "supabase_vault"
    settings.validate()  # use the actual supported backend value, not a made-up stub enum
    settings.environment = "production"
    monkeypatch.setattr(tick.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(tick, "build_engine", lambda *args: SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), dispose=lambda: None))
    monkeypatch.setattr(tick, "build_session_factory", lambda *args: client.app.state.SessionLocal)
    monkeypatch.setattr(tick, "assert_schema_revision", lambda *args: None)
    monkeypatch.setattr(tick, "_runtime_permission_errors", lambda *args: [])
    monkeypatch.setattr(tick, "platform_contract_errors", lambda *args: [])
    monkeypatch.setattr(tick, "SupabasePlatformVault", lambda *args: client.app.state.platform_vault)
    monkeypatch.setattr(tick, "collect_alerts", lambda *args: observation())
    attempts = []
    class Sender:
        def __init__(self, *args, **kwargs): pass
        def send_operator_alert(self, *args): attempts.append(args)
    monkeypatch.setattr(tick, "SmtpEmailSender", Sender)
    assert tick.main([]) == 0
    assert json.loads(capsys.readouterr().out)["smtp_attempts"] == 0
    with client.app.state.SessionLocal() as db:
        assert db.scalar(select(OutboxEvent).where(OutboxEvent.topic == "ops.alert.digest")) is None
    monkeypatch.setenv("KUNLUN_ALERT_NOTIFICATIONS_ENABLED", "true")
    monkeypatch.setenv("KUNLUN_ALERT_RECIPIENT", "ops@example.invalid")
    assert tick.main(["--send"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "accepted" and result["inbox_delivery_verified"] is False
    assert tick.main(["--send"]) == 0 and len(attempts) == 1
    capsys.readouterr()
    with client.app.state.SessionLocal() as db:
        db.get(OutboxEvent, result["notification_id"]).status = "unconfirmed"; db.commit()
    assert tick.main(["--send"]) == 1 and len(attempts) == 1
    settings.environment = "test"
    assert tick.main([]) == 1 and len(attempts) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "unconfirmed"
    settings.environment = "production"
    monkeypatch.setattr(tick, "_runtime_permission_errors", lambda *args: ["unsafe role"])
    assert tick.main(["--send"]) == 1 and len(attempts) == 1
