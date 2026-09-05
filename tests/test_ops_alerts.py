from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models import ModelRequest, OperatorAction, PaymentOrder, PaymentRefund, PlatformDailyBudget, Wallet
from app.security import utcnow
from tests.test_managed_gateway import managed, ready_call
from tests.test_ops_console import operator


def alerts(client):
    result = client.get("/ops/alerts", headers=operator("alerts:read"))
    assert result.status_code == 200
    assert "no-store" in result.headers["cache-control"]
    return {row["id"]: row for row in result.json()["items"]}


def ack(client, alert, **overrides):
    body = {"expected_revision": alert["revision"], "operation_id": "alert-receipt-1", "reason": "Operator accepted synthetic incident handoff"}
    body.update(overrides)
    return client.post(f"/ops/alerts/{alert['id']}/ack", headers=operator("alerts:write"), json=body)


def test_alerts_require_separate_scope_and_only_expose_metadata(managed):
    client, auth, *_ = managed
    assert client.get("/ops/alerts", headers=auth).status_code == 401
    assert client.get("/ops/alerts", headers=operator("console:read")).status_code == 401
    current = alerts(client)
    assert current["supply_unavailable"]["severity"] == "critical"
    assert not any(secret in str(current) for secret in ("managed@example.com", "secret", "password", "hello"))


def test_healthy_injected_supply_has_no_alert_and_does_not_call_model(managed):
    client, _, _ = ready_call(managed)
    assert alerts(client) == {}
    assert managed[-1] == []


def test_chargeback_risk_enters_existing_alerts_without_becoming_resolved_on_ack(managed):
    from app.models import PaymentChargeback, User
    from app.services.payment_domain import PaymentDomainService
    from tests.test_chargebacks import paid_order, dispute
    from app.services.alert_notifications import safe_rules
    client, _, _ = ready_call(managed)
    with client.app.state.SessionLocal() as db:
        owner = db.scalar(select(User.id))
        order = paid_order(db, owner)
        # Partial cash debit cannot be mapped to credits automatically.
        dispute(PaymentDomainService(db), order, amount=50)
    current = alerts(client)["chargeback_risk"]
    assert current["severity"] == "critical" and current["count"] == 1
    assert current["destination"] == "chargebacks"
    assert safe_rules([current])[0]["rule"] == "chargeback_risk"
    assert ack(client, current).status_code == 201
    with client.app.state.SessionLocal() as db:
        assert db.scalar(select(PaymentChargeback.status)) == "pending_reconciliation"


def test_early_chargeback_return_uses_payment_risk_alert_until_matched(managed):
    from app.models import User
    from app.services.payment_domain import PaymentDomainService
    from tests.test_chargebacks import paid_order, dispute
    from tests.test_chargeback_returns import returned
    client, _, _ = ready_call(managed)
    with client.app.state.SessionLocal() as db:
        order = paid_order(db, db.scalar(select(User.id)))
        order_id = order.id
        returned(PaymentDomainService(db), order)
    current = alerts(client)["payment_risk"]
    assert current["count"] == 1 and current["destination"] == "orders"
    assert ack(client, current).status_code == 201
    assert "payment_risk" in alerts(client)
    with client.app.state.SessionLocal() as db:
        dispute(PaymentDomainService(db), db.get(PaymentOrder, order_id))
    assert "payment_risk" not in alerts(client)


def test_ack_is_a_receipt_not_resolution_or_wallet_release(managed):
    client, headers, payload = ready_call(managed)
    client.app.state.test_upstream = lambda request: httpx.Response(500, json={"error": "never log this private upstream detail"})
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 502
    current = alerts(client)["model_reconciliation"]
    with client.app.state.SessionLocal() as db:
        wallet = db.scalar(select(Wallet))
        before = wallet.balance_microusd, wallet.reserved_microusd
    assert ack(client, current).status_code == 201
    assert ack(client, current).status_code == 409
    observed = alerts(client)["model_reconciliation"]
    assert observed["status"] == "attention" and observed["acknowledgement"]["actor"] == "inert-operator"
    with client.app.state.SessionLocal() as db:
        wallet = db.scalar(select(Wallet))
        assert before == (wallet.balance_microusd, wallet.reserved_microusd)
        assert db.scalar(select(ModelRequest.status)) == "pending_reconciliation"
        assert len(db.scalars(select(OperatorAction).where(OperatorAction.action == "alert_ack")).all()) == 1
    detail = client.get("/ops/alerts/model_reconciliation", headers=operator("alerts:read")).json()
    assert detail["alert"]["revision"] == current["revision"]


def test_budget_receipt_becomes_stale_when_usage_changes_and_resolved_is_not_ackable(managed):
    client, *_ = managed
    with client.app.state.SessionLocal() as db:
        db.add(PlatformDailyBudget(period=utcnow().date().isoformat(), limit_microusd=1000000, spent_microusd=800000, reserved_microusd=0))
        db.commit()
    warning = alerts(client)["platform_budget"]
    assert warning["severity"] == "warning"
    assert ack(client, warning).status_code == 201
    with client.app.state.SessionLocal() as db:
        db.scalar(select(PlatformDailyBudget)).spent_microusd = 1000000
        db.commit()
    critical = alerts(client)["platform_budget"]
    assert critical["severity"] == "critical" and critical["acknowledgement"] is None
    assert ack(client, warning, operation_id="stale-receipt").status_code == 409
    with client.app.state.SessionLocal() as db:
        db.scalar(select(PlatformDailyBudget)).spent_microusd = 0
        db.commit()
    assert ack(client, critical, operation_id="resolved-receipt").status_code == 409


def test_stale_reservations_are_reported_without_mutation(managed):
    client, headers, payload = ready_call(managed)
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 200
    with client.app.state.SessionLocal() as db:
        row = db.scalar(select(ModelRequest)); row.status = "reserved"
        row.created_at = utcnow() - timedelta(seconds=client.app.state.settings.model_reservation_lease_seconds + 10)
        db.commit()
    assert alerts(client)["stale_reservations"]["count"] == 1
    with client.app.state.SessionLocal() as db:
        assert db.scalar(select(ModelRequest.status)) == "reserved"


@pytest.mark.parametrize("overrides", [{"reason": " " * 16}, {"expected_revision": "invalid"}, {"operation_id": "bad key"}])
def test_invalid_receipts_are_rejected(managed, overrides):
    client, *_ = managed
    assert ack(client, alerts(client)["supply_unavailable"], **overrides).status_code == 422


def test_vault_failure_and_price_drift_are_reported_without_probe(managed, monkeypatch):
    from app.services.credentials import SecretUnavailable
    client, _, _ = ready_call(managed)
    client.app.state.settings.providers[0]["pricing"]["test-model"]["input_microusd_per_million"] = 2000000
    assert alerts(client)["price_below_supply"]["count"] == 1
    def unavailable():
        raise SecretUnavailable("private connection string must not leak")
    monkeypatch.setattr(client.app.state.platform_vault, "list", unavailable)
    result = alerts(client)
    assert result["supply_observation_failed"]["evidence"] == {"state": "unknown"}
    assert "supply_unavailable" not in result and "private connection" not in str(result)
    assert managed[-1] == []


def test_payment_and_refund_lease_alerts_match_real_states(managed):
    from uuid import uuid4
    client, _, _ = ready_call(managed)
    with client.app.state.SessionLocal() as db:
        order = db.scalar(select(PaymentOrder)); order_id = order.id
        order.status = "checkout_requesting"; order.checkout_claim_started_at = utcnow()
        refund = PaymentRefund(id=str(uuid4()), order_id=order.id, user_id=order.user_id,
            idempotency_key="synthetic-refund", payment_amount_minor=1, credit_amount_microusd=100,
            payment_currency="USD", status="requesting", claim_started_at=utcnow())
        db.add(refund); db.commit()
    assert alerts(client) == {}
    with client.app.state.SessionLocal() as db:
        db.get(PaymentOrder, order_id).checkout_claim_started_at = utcnow() - timedelta(minutes=6)
        db.scalar(select(PaymentRefund)).claim_started_at = utcnow() - timedelta(minutes=6)
        db.commit()
    current = alerts(client)
    assert current["payment_reconciliation"]["count"] == current["refund_reconciliation"]["count"] == 1
    with client.app.state.SessionLocal() as db:
        order = db.get(PaymentOrder, order_id); order.status = "refunded"; order.risk_reason = "refund_balance_insufficient"
        db.scalar(select(PaymentRefund)).status = "risk"; db.commit()
    current = alerts(client)
    assert current["payment_risk"]["count"] == current["refund_risk"]["count"] == 1
    assert current["refund_risk"]["severity"] == "critical"


def test_alert_scope_does_not_grant_other_operations_or_writes(managed):
    client, *_ = managed
    current = alerts(client)["supply_unavailable"]
    assert client.get("/ops/accounts", headers=operator("alerts:read")).status_code == 401
    assert client.post("/ops/alerts/supply_unavailable/ack", headers=operator("alerts:read"), json={
        "expected_revision": current["revision"], "operation_id": "no-write", "reason": "read-only scope is not a write scope",
    }).status_code == 401
    assert client.get("/ops/alerts/missing", headers=operator("alerts:read")).status_code == 404
    client.app.state.settings.gateway_mode = "byok"
    assert client.get("/ops/alerts", headers=operator("alerts:read")).status_code == 404


def test_database_failure_returns_unknown_not_an_empty_healthy_list(managed, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError
    client, *_ = managed
    def failed(*args, **kwargs):
        raise SQLAlchemyError("do not echo a private database connection")
    monkeypatch.setattr(client.app.state.SessionLocal.class_, "execute", failed)
    result = client.get("/ops/alerts", headers=operator("alerts:read"))
    assert result.status_code == 503 and "unknown" in result.text
    assert "private database" not in result.text
