import hashlib
import json

import pytest
from sqlalchemy import func, select

from app.models import LedgerEntry, LedgerTransaction, OperatorAction, PaymentChargeback, PaymentOrder, PaymentRefund, User, Wallet
from app.services.payment_domain import PaymentDomainError, PaymentDomainService
from tests.test_payment_domain import session


def paid_order(db, owner, provider="approved-test"):
    service = PaymentDomainService(db)
    order = service.create_order(user_id=owner, provider=provider, payment_amount_minor=100,
        payment_currency="USD", credit_amount_microusd=1000, quote_id="test-v1",
        quote_numerator=10, quote_denominator=1, idempotency_key="purchase-1")
    service.apply_webhook(provider=order.provider, event_id="paid-1", raw_digest="a" * 64,
        order_id=order.id, event_type="payment.succeeded", status="paid", payment_amount_minor=100,
        payment_currency="USD", provider_transaction_id="txn-1")
    return order


def dispute(service, order, *, event="debit-1", dispute_id="dispute-1", amount=100, txn="txn-1"):
    return service.apply_webhook(provider=order.provider, event_id=event,
        raw_digest=hashlib.sha256(f"{event}:{dispute_id}:{amount}".encode()).hexdigest(),
        order_id=order.id, event_type="payment.charged_back", status="charged_back",
        payment_amount_minor=amount, payment_currency="USD", provider_transaction_id=txn,
        provider_dispute_id=dispute_id)


def test_chargeback_once_across_duplicate_and_new_event_ids(session):
    db, owner = session
    order = paid_order(db, owner)
    service = PaymentDomainService(db)
    assert dispute(service, order) is False
    for _ in range(10):
        assert dispute(service, order) is True
    assert dispute(service, order, event="debit-new-event") is True
    assert db.get(Wallet, owner).balance_microusd == 0
    assert db.get(User, owner).status == "frozen"
    row = db.scalar(select(PaymentChargeback))
    assert row.status == "recovered" and row.recovered_microusd == 1000
    assert row.outstanding_microusd == 0
    assert db.scalar(select(func.count(LedgerTransaction.id)).where(LedgerTransaction.kind == "payment_chargeback")) == 1
    assert db.scalar(select(func.sum(LedgerEntry.amount_microusd))) == 0
    with pytest.raises(PaymentDomainError):
        service.prepare_refund(order_id=order.id, idempotency_key="must-not-refund")
    db.rollback()


def test_chargeback_preserves_model_holds_and_records_shortfall(session):
    db, owner = session
    order = paid_order(db, owner)
    wallet = db.get(Wallet, owner)
    # Synthetic in-flight state: balance is available, reserved is separate.
    wallet.balance_microusd = 200
    wallet.reserved_microusd = 300
    db.commit()
    dispute(PaymentDomainService(db), order)
    row = db.scalar(select(PaymentChargeback))
    assert (row.status, row.recovered_microusd, row.outstanding_microusd) == ("risk", 200, 800)
    assert (wallet.balance_microusd, wallet.reserved_microusd) == (0, 300)
    assert db.get(User, owner).status == "frozen"


@pytest.mark.parametrize("case", ["partial", "refund_complete", "refund_inflight", "second_dispute"])
def test_ambiguous_chargeback_is_recorded_without_guessing_a_second_debit(session, case):
    db, owner = session
    order = paid_order(db, owner)
    service = PaymentDomainService(db)
    if case == "refund_complete":
        service.apply_refund(order_id=order.id, idempotency_key="refund-1", provider_refund_id="refund-provider-1")
    elif case == "refund_inflight":
        service.prepare_refund(order_id=order.id, idempotency_key="refund-1")
    elif case == "second_dispute":
        dispute(service, order)
    before = db.get(Wallet, owner).balance_microusd
    dispute(service, order, event="new-event", dispute_id="other-dispute", amount=50 if case == "partial" else 100)
    row = db.scalar(select(PaymentChargeback).where(PaymentChargeback.provider_dispute_id == "other-dispute"))
    assert row.status == "pending_reconciliation"
    assert db.get(Wallet, owner).balance_microusd == before
    assert row.recovered_microusd == row.outstanding_microusd == 0
    assert db.get(User, owner).status == "frozen"
    with pytest.raises(PaymentDomainError):
        service.prepare_refund(order_id=order.id, idempotency_key="refund-1")
    db.rollback()


def test_chargeback_requires_consistent_dispute_identity(session):
    db, owner = session
    order = paid_order(db, owner)
    service = PaymentDomainService(db)
    dispute(service, order)
    with pytest.raises(PaymentDomainError):
        dispute(service, order, event="modified", amount=50)
    db.rollback()
    assert db.scalar(select(func.count(PaymentChargeback.id))) == 1


def test_signed_bridge_requires_dispute_id_only_on_chargeback():
    from tests.test_live_payments import bridge, signed_headers
    from app.services.live_payments import PaymentBridgeError
    adapter = bridge(lambda _: None)
    body = dict(merchant_id="merchant-1", event_id="event-1", order_id="order-1", payment_amount_minor=100, currency="USD",
        provider_transaction_id="txn-1", type="payment.charged_back", status="charged_back")
    raw = json.dumps(body).encode()
    with pytest.raises(PaymentBridgeError):
        adapter.verify_webhook(raw, signed_headers(raw))
    body["provider_dispute_id"] = "dispute-1"
    raw = json.dumps(body).encode()
    assert adapter.verify_webhook(raw, signed_headers(raw)).provider_dispute_id == "dispute-1"
    body.update(type="payment.succeeded", status="paid", event_id="wrong-type")
    raw = json.dumps(body).encode()
    with pytest.raises(PaymentBridgeError):
        adapter.verify_webhook(raw, signed_headers(raw, nonce="wrong-type"))


def test_refund_after_chargeback_retains_both_records_without_second_debit(session):
    db, owner = session
    order = paid_order(db, owner)
    service = PaymentDomainService(db)
    dispute(service, order)
    result = service.apply_webhook(provider=order.provider, event_id="refund-late", raw_digest="b" * 64,
        order_id=order.id, event_type="payment.refunded", status="refunded", payment_amount_minor=100,
        payment_currency="USD", provider_transaction_id="txn-1", provider_refund_id="late-refund")
    assert result is False
    assert db.get(Wallet, owner).balance_microusd == 0
    assert db.scalar(select(PaymentChargeback)).status == "pending_reconciliation"
    assert db.scalar(select(PaymentRefund)).risk_reason == "refund_chargeback_overlap"
    assert db.scalar(select(func.count(LedgerTransaction.id))) == 2


def test_debit_before_paid_callback_cannot_create_or_recredit_balance(session):
    db, owner = session
    order = paid_order(db, owner)
    # Independent new order whose paid callback has not been received.
    service = PaymentDomainService(db)
    second = service.create_order(user_id=owner, provider="approved-test", payment_amount_minor=100,
        payment_currency="USD", credit_amount_microusd=1000, quote_id="test-v1", quote_numerator=10,
        quote_denominator=1, idempotency_key="purchase-2")
    dispute(service, second, txn="txn-2")
    assert db.get(Wallet, owner).balance_microusd == 1000
    assert db.get(PaymentOrder, second.id).status == "disputed"
    assert db.get(PaymentOrder, second.id).provider_transaction_id == "txn-2"
    with pytest.raises(PaymentDomainError):
        service.apply_webhook(provider=order.provider, event_id="late-paid", raw_digest="c" * 64,
            order_id=second.id, event_type="payment.succeeded", status="paid", payment_amount_minor=100,
            payment_currency="USD", provider_transaction_id="txn-2")
    db.rollback()


@pytest.mark.parametrize("action", ["recover_available", "write_off"])
def test_risk_disposition_requires_cleared_holds_and_is_idempotent(session, action):
    from app.services.chargebacks import resolve_chargeback_risk
    db, owner = session
    order = paid_order(db, owner)
    wallet = db.get(Wallet, owner)
    wallet.balance_microusd = 200
    wallet.reserved_microusd = 800
    db.commit()
    dispute(PaymentDomainService(db), order)
    row = db.scalar(select(PaymentChargeback))
    with pytest.raises(PaymentDomainError):
        resolve_chargeback_risk(db, row.id, action=action, idempotency_key="resolve-1")
    db.rollback()
    # Emulate release/settlement of the original work; this test focuses on
    # disposition. Real reserve/release concurrency is tested in PostgreSQL.
    wallet.reserved_microusd = 0
    wallet.balance_microusd = 800 if action == "recover_available" else 300
    db.commit()
    resolved, duplicate, recovered, written_off = resolve_chargeback_risk(db, row.id,
        action=action, idempotency_key="resolve-1")
    db.commit()
    assert not duplicate and recovered + written_off == 800
    assert resolved.status == "resolved" and resolved.outstanding_microusd == 0
    assert db.get(User, owner).status == "frozen"
    repeat = resolve_chargeback_risk(db, row.id, action=action, idempotency_key="resolve-1")
    assert repeat[1:] == (True, recovered, written_off)
    with pytest.raises(PaymentDomainError):
        resolve_chargeback_risk(db, row.id, action="write_off" if action == "recover_available" else "recover_available",
            idempotency_key="resolve-1")
    db.rollback()


def test_chargeback_ops_scope_and_atomic_audit(tmp_path):
    from tests.test_live_payment_routes import FakeLiveBridge, OPS_SECRET, _client
    from app.services.ops_tokens import mint_operator_token
    app, client, auth = _client(tmp_path, FakeLiveBridge())
    try:
        with app.state.SessionLocal() as db:
            owner = db.scalar(select(User))
            owner.email_verified_at = owner.created_at
            db.commit()
            order = paid_order(db, owner.id)
            db.get(Wallet, owner.id).balance_microusd = 0
            db.commit()
            dispute(PaymentDomainService(db), order)
            row_id = db.scalar(select(PaymentChargeback.id))
            owner_id = owner.id
        assert client.get("/ops/chargebacks", headers=auth).status_code in (401, 403)
        def ops(scopes):
            return {"X-Kunlun-Ops-Token": mint_operator_token(OPS_SECRET, subject="chargeback-reviewer", scopes=scopes)}
        read = ops({"payments:read"})
        write = ops({"payments:risk:write"})
        assert client.get("/ops/chargebacks", headers=read).json()["pagination"]["total"] == 1
        assert client.get("/ops/chargebacks/absent", headers=read).status_code == 404
        url = f"/ops/chargebacks/{row_id}/risk-disposition"
        payload = {"action": "write_off", "reason": "synthetic risk loss reviewed", "idempotency_key": "ops-1"}
        assert client.post(url, headers=read, json=payload).status_code == 401
        unfreeze = f"/ops/accounts/{owner_id}/status"
        assert client.post(unfreeze, headers=ops({"accounts:write"}), json={"action": "unfreeze", "reason": "synthetic review only"}).status_code == 409
        from sqlalchemy import event
        from sqlalchemy.exc import IntegrityError
        def fail_audit(*_):
            raise IntegrityError("synthetic audit failure", {}, Exception("inert"))
        event.listen(OperatorAction, "before_insert", fail_audit)
        try:
            assert client.post(url, headers=write, json=payload).status_code == 409
        finally:
            event.remove(OperatorAction, "before_insert", fail_audit)
        with app.state.SessionLocal() as db:
            assert db.get(PaymentChargeback, row_id).outstanding_microusd == 1000
            assert db.get(PaymentChargeback, row_id).status == "risk"
            assert db.scalar(select(func.count(LedgerTransaction.id))) == 2
        result = client.post(url, headers=write, json=payload)
        assert result.status_code == 200, result.text
        assert result.json()["written_off_microusd"] == 1000
        assert client.post(url, headers=write, json=payload).json()["duplicate"] is True
        with app.state.SessionLocal() as db:
            assert db.scalar(select(func.count(OperatorAction.id)).where(OperatorAction.target_type == "payment_chargeback")) == 1
            assert db.get(User, owner_id).status == "frozen"
        assert client.post(unfreeze, headers=ops({"accounts:write"}), json={"action": "unfreeze", "reason": "synthetic review only"}).status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_signed_chargeback_webhook_route_rejects_bad_merchant_then_applies_once(tmp_path):
    from tests.test_live_payment_routes import _client
    from tests.test_live_payments import bridge, signed_headers
    adapter = bridge(lambda _: None)
    app, client, _ = _client(tmp_path, adapter)
    try:
        with app.state.SessionLocal() as db:
            owner = db.scalar(select(User.id))
            order = paid_order(db, owner, "wechatpay")
            order_id = order.id
        body = dict(merchant_id="wrong-merchant", event_id="route-cb", order_id=order_id,
            payment_amount_minor=100, currency="USD", provider_transaction_id="txn-1",
            type="payment.charged_back", status="charged_back", provider_dispute_id="route-dispute")
        raw = json.dumps(body).encode()
        assert client.post("/billing/live/webhook", content=raw, headers=signed_headers(raw)).status_code == 401
        body["merchant_id"] = "merchant-1"
        raw = json.dumps(body).encode()
        assert client.post("/billing/live/webhook", content=raw).status_code == 401
        for index in range(10):
            result = client.post("/billing/live/webhook", content=raw, headers=signed_headers(raw))
            assert result.status_code == 200, result.text
            assert result.json()["duplicate"] is (index > 0)
        with app.state.SessionLocal() as db:
            assert db.scalar(select(func.count(PaymentChargeback.id))) == 1
            assert db.get(Wallet, owner).balance_microusd == 0
    finally:
        client.__exit__(None, None, None)
