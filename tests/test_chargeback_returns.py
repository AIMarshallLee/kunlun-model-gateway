import hashlib

import pytest
from sqlalchemy import func, select

from app.models import LedgerEntry, LedgerTransaction, PaymentChargeback, PaymentOrder, User, Wallet
from app.services.payment_domain import PaymentDomainError, PaymentDomainService
from app.services.chargebacks import resolve_chargeback_risk
from tests.test_payment_domain import session
from tests.test_chargebacks import paid_order, dispute


def returned(service, order, *, event="return-event", return_id="return-1", amount=100, dispute_id="dispute-1"):
    return service.apply_webhook(provider=order.provider, event_id=event,
        raw_digest=hashlib.sha256(f"{event}:{return_id}:{amount}:{dispute_id}".encode()).hexdigest(),
        order_id=order.id, event_type="payment.chargeback_returned", status="chargeback_returned",
        payment_amount_minor=amount, payment_currency="USD", provider_transaction_id="txn-1",
        provider_dispute_id=dispute_id, provider_return_id=return_id)


@pytest.mark.parametrize("disposition", ["unresolved", "recover_available", "write_off"])
def test_return_restores_only_recovered_credit_and_reverses_risk_or_loss(session, disposition):
    db, owner = session
    order = paid_order(db, owner)
    wallet = db.get(Wallet, owner)
    wallet.balance_microusd = 200
    wallet.reserved_microusd = 300 if disposition == "unresolved" else 0
    db.commit()
    service = PaymentDomainService(db)
    dispute(service, order)
    case = db.scalar(select(PaymentChargeback))
    if disposition != "unresolved":
        wallet.balance_microusd = 800 if disposition == "recover_available" else 0
        db.commit()
        resolve_chargeback_risk(db, case.id, action=disposition, idempotency_key="risk-command")
        db.commit()
    assert returned(service, order) is False
    expected = 1000 if disposition == "recover_available" else 200
    assert wallet.balance_microusd == expected
    assert wallet.reserved_microusd == (300 if disposition == "unresolved" else 0)
    assert case.status == "resolved" and case.outstanding_microusd == 0
    assert case.risk_reason == "chargeback_funds_returned"
    assert db.get(User, owner).status == "frozen"
    for _ in range(10):
        assert returned(service, order) is True
    assert returned(service, order, event="another-event") is True
    assert wallet.balance_microusd == expected
    assert db.scalar(select(func.count(LedgerTransaction.id)).where(LedgerTransaction.kind == "chargeback_return")) == 1
    assert db.scalar(select(func.sum(LedgerEntry.amount_microusd))) == 0


def test_return_before_debit_pairs_without_extra_credit(session):
    db, owner = session
    order = paid_order(db, owner)
    service = PaymentDomainService(db)
    assert returned(service, order) is False
    assert db.get(Wallet, owner).balance_microusd == 1000
    assert db.get(User, owner).status == "frozen"
    with pytest.raises(PaymentDomainError):
        service.prepare_refund(order_id=order.id, idempotency_key="refund-blocked")
    db.rollback()
    dispute(service, order)
    case = db.scalar(select(PaymentChargeback))
    assert case.status == "resolved" and case.risk_reason == "chargeback_funds_returned"
    assert db.get(Wallet, owner).balance_microusd == 1000
    assert db.scalar(select(func.count(LedgerTransaction.id)).where(LedgerTransaction.kind == "chargeback_return")) == 1


@pytest.mark.parametrize("scenario", ["partial", "other_dispute", "refund", "second_return", "bad_projection"])
def test_ambiguous_return_keeps_evidence_without_credit(session, scenario):
    db, owner = session
    order = paid_order(db, owner)
    service = PaymentDomainService(db)
    if scenario == "refund":
        service.apply_refund(order_id=order.id, idempotency_key="ref", provider_refund_id="ref-1")
    dispute(service, order)
    if scenario == "second_return":
        returned(service, order)
    if scenario == "bad_projection":
        db.scalar(select(PaymentChargeback)).recovered_microusd = 900
        db.commit()
    before = db.get(Wallet, owner).balance_microusd
    returned(service, order, event="return-ambiguous", return_id="return-other",
             amount=50 if scenario == "partial" else 100,
             dispute_id="unknown-dispute" if scenario == "other_dispute" else "dispute-1")
    from app.models import PaymentChargebackReturn
    row = db.scalar(select(PaymentChargebackReturn).where(PaymentChargebackReturn.provider_return_id == "return-other"))
    assert row.status == "pending_reconciliation"
    assert db.get(Wallet, owner).balance_microusd == before
    assert db.get(User, owner).status == "frozen"


def test_return_identifier_cannot_change_principal(session):
    db, owner = session
    order = paid_order(db, owner)
    service = PaymentDomainService(db)
    dispute(service, order)
    returned(service, order)
    with pytest.raises(PaymentDomainError):
        returned(service, order, event="conflicting-event", amount=50)
    db.rollback()


def test_signed_return_route_and_scoped_pending_query(tmp_path):
    import json
    from tests.test_live_payment_routes import OPS_SECRET, _client
    from tests.test_live_payments import bridge, signed_headers
    from app.services.ops_tokens import mint_operator_token
    adapter = bridge(lambda _: None)
    app, client, customer = _client(tmp_path, adapter)
    try:
        with app.state.SessionLocal() as db:
            owner = db.scalar(select(User))
            owner.email_verified_at = owner.created_at
            db.commit()
            order = paid_order(db, owner.id, "wechatpay")
            order_id, owner_id = order.id, owner.id
        body = dict(merchant_id="merchant-1", event_id="signed-return", order_id=order_id,
            payment_amount_minor=100, currency="USD", provider_transaction_id="txn-1",
            type="payment.chargeback_returned", status="chargeback_returned",
            provider_dispute_id="dispute-1", provider_return_id="signed-cash-return")
        for change in ({"merchant_id": "wrong"}, {"status": "won"}, {"provider_return_id": ""},
                       {"provider_dispute_id": ""}, {"type": "payment.succeeded", "status": "paid"}):
            raw = json.dumps(body | change).encode()
            assert client.post("/billing/live/webhook", content=raw, headers=signed_headers(raw)).status_code == 401
        raw = json.dumps(body).encode()
        assert client.post("/billing/live/webhook", content=raw).status_code == 401
        for index in range(10):
            response = client.post("/billing/live/webhook", content=raw, headers=signed_headers(raw))
            assert response.status_code == 200, response.text
            assert response.json()["duplicate"] is (index > 0)
        def ops(scopes):
            return {"X-Kunlun-Ops-Token": mint_operator_token(OPS_SECRET, subject="test-return-reviewer", scopes=scopes)}
        url = "/ops/chargeback-returns"
        assert client.get(url, headers=customer).status_code in {401, 403}
        assert client.get(url, headers=ops({"accounts:read"})).status_code in {401, 403}
        report = client.get(url, headers=ops({"payments:read"})).json()
        assert report["pagination"]["total"] == 1
        assert report["items"][0]["status"] == "pending_reconciliation"
        assert report["items"][0]["restored_microusd"] == 0
        assert client.get(url + "?order_id=absent", headers=ops({"payments:read"})).json()["items"] == []
        assert client.get(url + "?limit=201", headers=ops({"payments:read"})).status_code == 422
        response = client.post(f"/ops/accounts/{owner_id}/status", headers=ops({"accounts:write"}),
            json={"action": "unfreeze", "reason": "synthetic account review only"})
        assert response.status_code == 409 and "返还" in response.text
        with app.state.SessionLocal() as db:
            dispute(PaymentDomainService(db), db.get(PaymentOrder, order_id))
        report = client.get(url, headers=ops({"payments:read"})).json()
        assert report["items"][0]["status"] == "applied"
        assert report["items"][0]["restored_microusd"] == 1000
    finally:
        client.__exit__(None, None, None)


def test_two_early_returns_require_reconciliation_even_after_debit(session):
    db, owner = session
    order = paid_order(db, owner)
    service = PaymentDomainService(db)
    returned(service, order)
    returned(service, order, event="second", return_id="second-return")
    dispute(service, order)
    from app.models import PaymentChargebackReturn
    assert all(row.status == "pending_reconciliation" for row in db.scalars(select(PaymentChargebackReturn)))
    assert db.get(Wallet, owner).balance_microusd == 0
    assert order.risk_reason == "chargeback_return_reconciliation"


def test_return_transaction_failure_rolls_back_wallet_and_evidence(session):
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError
    from app.models import PaymentChargebackReturn
    db, owner = session
    order = paid_order(db, owner)
    service = PaymentDomainService(db)
    dispute(service, order)
    def fail_return(_mapper, _connection, row):
        if row.kind == "chargeback_return":
            raise IntegrityError("synthetic failure", {}, Exception("inert"))
    event.listen(LedgerTransaction, "before_insert", fail_return)
    try:
        with pytest.raises(PaymentDomainError):
            returned(service, order)
    finally:
        event.remove(LedgerTransaction, "before_insert", fail_return)
    assert db.get(Wallet, owner).balance_microusd == 0
    assert db.scalar(select(func.count(PaymentChargebackReturn.id))) == 0
    assert db.scalar(select(PaymentChargeback)).status == "recovered"
    assert returned(service, order) is False
