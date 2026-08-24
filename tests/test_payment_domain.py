from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from app.models import (
    ApiKey, EmailVerificationToken, LedgerEntry, LedgerTransaction, ModelPrice,
    PasswordResetToken, PaymentOrder, PaymentRefund, PaymentWebhookEvent, User, Wallet,
)
from app.services.gateway_billing import (
    release_model_request, reserve_model_request, settle_model_request,
)
from app.services.payment_domain import PaymentDomainError, PaymentDomainService
from app.security import token_digest, utcnow


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'domain.sqlite'}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(id=str(uuid.uuid4()), email="domain@example.com", password_hash="x")
        db.add_all([user, Wallet(user_id=user.id)])
        db.commit()
        yield db, user.id


def test_order_keeps_cash_credit_and_quote_snapshot_separate(session):
    db, user_id = session
    order = PaymentDomainService(db).create_order(
        user_id=user_id, provider="wechatpay", payment_amount_minor=1999,
        payment_currency="CNY", credit_amount_microusd=250000,
        quote_id="quote-v3", quote_numerator=250000, quote_denominator=1999,
        idempotency_key="signup-1",
    )
    assert (order.payment_amount_minor, order.payment_currency) == (1999, "CNY")
    assert order.credit_amount_microusd == 250000
    assert (order.quote_id, order.quote_numerator, order.quote_denominator) == ("quote-v3", 250000, 1999)
    assert PaymentDomainService(db).create_order(
        user_id=user_id, provider="wechatpay", payment_amount_minor=1999,
        payment_currency="CNY", credit_amount_microusd=250000,
        quote_id="quote-v3", quote_numerator=250000, quote_denominator=1999,
        idempotency_key="signup-1",
    ).id == order.id
    with pytest.raises(PaymentDomainError, match="报价不一致"):
        PaymentDomainService(db).create_order(
            user_id=user_id, provider="wechatpay", payment_amount_minor=2999,
            payment_currency="CNY", credit_amount_microusd=250000,
            quote_id="quote-v3", quote_numerator=250000, quote_denominator=1999,
            idempotency_key="signup-1",
        )


def test_frozen_account_cannot_create_or_claim_checkout(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(
        user_id=user_id, provider="wechatpay", payment_amount_minor=1999,
        payment_currency="CNY", credit_amount_microusd=250000,
        quote_id="freeze-race", quote_numerator=250000, quote_denominator=1999,
        idempotency_key="freeze-race-before-claim",
    )
    db.get(User, user_id).status = "frozen"
    db.commit()

    with pytest.raises(PaymentDomainError, match="账户不可用") as claim_rejected:
        service.prepare_checkout(order_id=order.id)
    assert claim_rejected.value.status_code == 403
    db.expire_all()
    assert db.get(PaymentOrder, order.id).status == "pending"

    with pytest.raises(PaymentDomainError, match="账户不可用") as create_rejected:
        service.create_order(
            user_id=user_id, provider="wechatpay", payment_amount_minor=1999,
            payment_currency="CNY", credit_amount_microusd=250000,
            quote_id="freeze-race-2", quote_numerator=250000, quote_denominator=1999,
            idempotency_key="freeze-race-new-order",
        )
    assert create_rejected.value.status_code == 403


def test_checkout_claim_is_exclusive_and_stale_claim_requires_reconciliation(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(
        user_id=user_id, provider="wechatpay", payment_amount_minor=1999,
        payment_currency="CNY", credit_amount_microusd=250000,
        quote_id="quote-checkout", quote_numerator=250000, quote_denominator=1999,
        idempotency_key="checkout-lease",
    )
    claimed_order, claimed = service.prepare_checkout(order_id=order.id)
    assert claimed is True
    assert claimed_order.status == "checkout_requesting"
    assert claimed_order.checkout_claim_started_at is not None
    with pytest.raises(PaymentDomainError, match="正在创建") as active:
        service.prepare_checkout(order_id=order.id)
    assert active.value.status_code == 409

    claimed_order.checkout_claim_started_at = utcnow() - timedelta(minutes=10)
    db.commit()
    with pytest.raises(PaymentDomainError, match="人工对账") as stale:
        service.prepare_checkout(order_id=order.id)
    assert stale.value.status_code == 503
    db.expire_all()
    final = db.get(PaymentOrder, order.id)
    assert final.status == "pending_reconciliation"
    assert final.risk_reason == "checkout_claim_expired"


def test_paid_webhook_can_settle_while_checkout_response_is_in_flight(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(
        user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
        payment_currency="CNY", credit_amount_microusd=100000,
        quote_id="checkout-paid-race", quote_numerator=100000, quote_denominator=1000,
        idempotency_key="checkout-paid-race",
    )
    service.prepare_checkout(order_id=order.id)
    assert service.apply_webhook(
        provider="wechatpay", event_id="checkout-paid-race-event", raw_digest="a" * 64,
        order_id=order.id, event_type="payment.succeeded", status="paid",
        payment_amount_minor=1000, payment_currency="CNY",
        provider_transaction_id="checkout-paid-race-txn",
    ) is False
    db.expire_all()
    settled = db.get(PaymentOrder, order.id)
    assert settled.status == "paid"
    assert settled.checkout_claim_started_at is None
    assert db.get(Wallet, user_id).balance_microusd == 100000


def test_pending_webhook_during_checkout_claim_requires_reconciliation(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(
        user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
        payment_currency="CNY", credit_amount_microusd=100000,
        quote_id="checkout-pending-race", quote_numerator=100000, quote_denominator=1000,
        idempotency_key="checkout-pending-race",
    )
    service.prepare_checkout(order_id=order.id)
    service.apply_webhook(
        provider="wechatpay", event_id="checkout-pending-event", raw_digest="b" * 64,
        order_id=order.id, event_type="payment.pending", status="pending",
        payment_amount_minor=1000, payment_currency="CNY",
        provider_transaction_id="checkout-pending-txn",
    )
    db.expire_all()
    pending = db.get(PaymentOrder, order.id)
    assert pending.status == "pending_reconciliation"
    assert pending.risk_reason == "checkout_callback_before_response"
    assert pending.checkout_claim_started_at is None


def test_paid_webhook_is_idempotent_and_credits_once(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
                                payment_currency="CNY", credit_amount_microusd=100000,
                                quote_id="q", quote_numerator=100000, quote_denominator=1000,
                                idempotency_key="o1")
    raw = json.dumps({"event": "paid", "order": order.id}, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    assert service.apply_webhook(provider="wechatpay", event_id="e1", raw_digest=digest, order_id=order.id,
                                 event_type="payment.succeeded", status="paid", payment_amount_minor=1000,
                                 payment_currency="CNY", provider_transaction_id="tx1") is False
    assert service.apply_webhook(provider="wechatpay", event_id="e1", raw_digest=digest, order_id=order.id,
                                 event_type="payment.succeeded", status="paid", payment_amount_minor=1000,
                                 payment_currency="CNY", provider_transaction_id="tx1") is True
    db.expire_all()
    assert db.scalar(select(Wallet.balance_microusd).where(Wallet.user_id == user_id)) == 100000
    assert len(db.scalars(select(LedgerTransaction).where(LedgerTransaction.reference == order.id)).all()) == 1


def test_webhook_rejects_conflict_and_mismatch(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
                                payment_currency="CNY", credit_amount_microusd=100000,
                                quote_id="q", quote_numerator=100000, quote_denominator=1000,
                                idempotency_key="o2")
    with pytest.raises(PaymentDomainError):
        service.apply_webhook(provider="wechatpay", event_id="bad", raw_digest="a" * 64, order_id=order.id,
                              event_type="payment.succeeded", status="paid", payment_amount_minor=999,
                              payment_currency="CNY", provider_transaction_id="tx")
    service.apply_webhook(provider="wechatpay", event_id="e2", raw_digest="b" * 64, order_id=order.id,
                          event_type="payment.succeeded", status="paid", payment_amount_minor=1000,
                          payment_currency="CNY", provider_transaction_id="tx2")
    with pytest.raises(PaymentDomainError):
        service.apply_webhook(provider="wechatpay", event_id="e2", raw_digest="c" * 64, order_id=order.id,
                              event_type="payment.succeeded", status="paid", payment_amount_minor=1000,
                              payment_currency="CNY", provider_transaction_id="tx2")


def test_refund_with_insufficient_balance_freezes_and_uses_risk_entries(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
                                payment_currency="CNY", credit_amount_microusd=100000,
                                quote_id="q", quote_numerator=100000, quote_denominator=1000,
                                idempotency_key="o3")
    service.apply_webhook(provider="wechatpay", event_id="e3", raw_digest="d" * 64, order_id=order.id,
                          event_type="payment.succeeded", status="paid", payment_amount_minor=1000,
                          payment_currency="CNY", provider_transaction_id="tx3")
    db.add_all([
        EmailVerificationToken(
            id=str(uuid.uuid4()), user_id=user_id,
            token_digest=token_digest("verify-before-risk", "pepper"),
            expires_at=utcnow() + timedelta(hours=1),
        ),
        PasswordResetToken(
            id=str(uuid.uuid4()), user_id=user_id,
            token_digest=token_digest("reset-before-risk", "pepper"),
            expires_at=utcnow() + timedelta(hours=1),
        ),
    ])
    db.execute(Wallet.__table__.update().where(Wallet.user_id == user_id).values(balance_microusd=1))
    db.commit()
    refund = service.apply_refund(order_id=order.id, idempotency_key="r1", provider_refund_id="rf1")
    assert refund.status == "risk"
    assert db.get(User, user_id).status == "frozen"
    assert db.scalar(select(EmailVerificationToken.consumed_at)) is not None
    assert db.scalar(select(PasswordResetToken.consumed_at)) is not None
    assert db.get(Wallet, user_id).balance_microusd == 0
    entries = db.scalars(select(LedgerEntry).join(LedgerTransaction).where(LedgerTransaction.reference == order.id)).all()
    risk_refund_entries = [
        e for e in entries
        if db.get(LedgerTransaction, e.transaction_id).kind == "payment_refund_risk"
    ]
    assert {(e.account, e.amount_microusd) for e in risk_refund_entries} == {
        ("customer_available", -1),
        ("platform_risk", -99_999),
        ("platform_clearing", 100_000),
    }


def _risk_refund_with_reserved_model_request(db, user_id, suffix):
    service = PaymentDomainService(db)
    order = service.create_order(
        user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
        payment_currency="CNY", credit_amount_microusd=100000,
        quote_id="q", quote_numerator=100000, quote_denominator=1000,
        idempotency_key=f"risk-reserved-order-{suffix}",
    )
    service.apply_webhook(
        provider="wechatpay", event_id=f"risk-paid-{suffix}", raw_digest="7" * 64,
        order_id=order.id, event_type="payment.succeeded", status="paid",
        payment_amount_minor=1000, payment_currency="CNY",
        provider_transaction_id=f"risk-tx-{suffix}",
    )
    key_id = f"risk-key-{suffix}"
    db.add_all([
        ApiKey(
            id=key_id, user_id=user_id, name="risk-test", secret_digest="d" * 64,
            last_four="test",
        ),
        ModelPrice(
            id=str(uuid.uuid4()), model=f"risk-model-{suffix}", version=1,
            input_microusd_per_million=1_000_000,
            output_microusd_per_million=1_000_000,
            max_output_tokens=100,
        ),
    ])
    db.commit()
    reservation = reserve_model_request(
        db, user_id=user_id, api_key_id=key_id, model=f"risk-model-{suffix}",
        billable_payload={"messages": [{"role": "user", "content": "hello"}]},
        max_output_tokens=10, idempotency_key=f"risk-request-{suffix}",
    )
    refund = service.apply_refund(
        order_id=order.id, idempotency_key=f"risk-refund-{suffix}",
        provider_refund_id=f"risk-provider-refund-{suffix}",
    )
    assert refund.status == "risk"
    assert db.get(Wallet, user_id).reserved_microusd == reservation.amount
    return service, order, refund, reservation


def test_refund_risk_waits_for_reserved_release_then_recovers_credit(session):
    db, user_id = session
    service, _order, refund, reservation = _risk_refund_with_reserved_model_request(
        db, user_id, "release",
    )
    with pytest.raises(PaymentDomainError, match="在途预授权"):
        service.resolve_refund_risk(
            refund_id=refund.id, action="recover_available", idempotency_key="recover-after-release",
        )
    release_model_request(db, reservation.request_id, "operator confirmed no charge")
    resolved, duplicate, outstanding, recovered, written_off = service.resolve_refund_risk(
        refund_id=refund.id, action="recover_available", idempotency_key="recover-after-release",
    )
    db.commit()
    assert duplicate is False
    assert resolved.status == "resolved"
    assert outstanding == recovered == reservation.amount
    assert written_off == 0
    assert db.get(Wallet, user_id).balance_microusd == 0
    again = service.resolve_refund_risk(
        refund_id=refund.id, action="recover_available", idempotency_key="recover-after-release",
    )
    assert again[1] is True


def test_refund_risk_waits_for_reserved_settlement_then_writes_off_residual(session):
    db, user_id = session
    service, _order, refund, reservation = _risk_refund_with_reserved_model_request(
        db, user_id, "settle",
    )
    settle_model_request(
        db,
        request_id=reservation.request_id,
        response={
            "model": "risk-model-settle",
            "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
        provider="provider-a",
        fallback_count=0,
        upstream_input_price=1_000_000,
        upstream_output_price=1_000_000,
    )
    resolved, duplicate, outstanding, recovered, written_off = service.resolve_refund_risk(
        refund_id=refund.id, action="write_off", idempotency_key="writeoff-after-settle",
    )
    db.commit()
    assert duplicate is False
    assert resolved.status == "resolved"
    assert outstanding == reservation.amount
    assert recovered + written_off == outstanding
    assert recovered > 0 and written_off > 0
    assert db.get(Wallet, user_id).balance_microusd == 0
    entries = db.scalars(select(LedgerEntry).join(LedgerTransaction).where(
        LedgerTransaction.kind == "refund_risk_write_off",
    )).all()
    assert sum(entry.amount_microusd for entry in entries) == 0
    assert any(entry.account == "platform_loss" for entry in entries)


def test_full_refund_cannot_be_applied_again_with_new_key(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
                                payment_currency="CNY", credit_amount_microusd=100000,
                                quote_id="q", quote_numerator=100000, quote_denominator=1000,
                                idempotency_key="o4")
    service.apply_webhook(provider="wechatpay", event_id="e4", raw_digest="e" * 64, order_id=order.id,
                          event_type="payment.succeeded", status="paid", payment_amount_minor=1000,
                          payment_currency="CNY", provider_transaction_id="tx4")
    service.apply_refund(order_id=order.id, idempotency_key="r4", provider_refund_id="rf4")
    with pytest.raises(PaymentDomainError):
        service.apply_refund(order_id=order.id, idempotency_key="r4b", provider_refund_id="rf4b")


def test_refunds_on_distinct_orders_may_reuse_client_idempotency_key(session):
    db, user_id = session
    service = PaymentDomainService(db)
    orders = []
    for suffix in ("first", "second"):
        order = service.create_order(
            user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
            payment_currency="CNY", credit_amount_microusd=100000,
            quote_id=f"refund-cross-order-{suffix}", quote_numerator=100000,
            quote_denominator=1000, idempotency_key=f"refund-cross-order-{suffix}",
        )
        service.apply_webhook(
            provider="wechatpay", event_id=f"refund-cross-order-paid-{suffix}",
            raw_digest=("f" if suffix == "first" else "e") * 64,
            order_id=order.id, event_type="payment.succeeded", status="paid",
            payment_amount_minor=1000, payment_currency="CNY",
            provider_transaction_id=f"refund-cross-order-tx-{suffix}",
        )
        orders.append(order)

    refunds = [
        service.apply_refund(
            order_id=order.id, idempotency_key="same-client-refund-key",
            provider_refund_id=f"refund-cross-order-provider-{index}",
        )
        for index, order in enumerate(orders, start=1)
    ]

    assert [refund.status for refund in refunds] == ["refunded", "refunded"]
    transactions = db.scalars(select(LedgerTransaction).where(
        LedgerTransaction.kind == "payment_refund",
        LedgerTransaction.reference.in_([order.id for order in orders]),
    )).all()
    assert len(transactions) == 2
    assert len({transaction.idempotency_key for transaction in transactions}) == 2
    assert all(
        transaction.idempotency_key == f"refund:{refund.id}"
        for transaction, refund in zip(transactions, refunds)
    )


def test_risk_refunds_on_distinct_orders_namespace_ledger_keys_by_refund(session):
    db, user_id = session
    service = PaymentDomainService(db)
    orders = []
    for suffix in ("first", "second"):
        order = service.create_order(
            user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
            payment_currency="CNY", credit_amount_microusd=100000,
            quote_id=f"refund-risk-cross-order-{suffix}", quote_numerator=100000,
            quote_denominator=1000, idempotency_key=f"refund-risk-cross-order-{suffix}",
        )
        service.apply_webhook(
            provider="wechatpay", event_id=f"refund-risk-cross-order-paid-{suffix}",
            raw_digest=("d" if suffix == "first" else "c") * 64,
            order_id=order.id, event_type="payment.succeeded", status="paid",
            payment_amount_minor=1000, payment_currency="CNY",
            provider_transaction_id=f"refund-risk-cross-order-tx-{suffix}",
        )
        orders.append(order)
    db.execute(Wallet.__table__.update().where(Wallet.user_id == user_id).values(
        balance_microusd=1,
    ))
    db.commit()

    refunds = [
        service.apply_refund(
            order_id=order.id, idempotency_key="same-client-risk-key",
            provider_refund_id=f"refund-cross-order-risk-provider-{index}",
        )
        for index, order in enumerate(orders, start=1)
    ]

    assert [refund.status for refund in refunds] == ["risk", "risk"]
    transactions = db.scalars(select(LedgerTransaction).where(
        LedgerTransaction.kind == "payment_refund_risk",
        LedgerTransaction.reference.in_([order.id for order in orders]),
    )).all()
    assert len(transactions) == 2
    assert len({transaction.idempotency_key for transaction in transactions}) == 2
    assert all(
        transaction.idempotency_key == f"refund-risk:{refund.id}"
        for transaction, refund in zip(transactions, refunds)
    )


def test_terminal_refund_replay_must_keep_provider_refund_identity(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
                                 payment_currency="CNY", credit_amount_microusd=100000,
                                 quote_id="q", quote_numerator=100000, quote_denominator=1000,
                                 idempotency_key="o-terminal-refund")
    service.apply_webhook(provider="wechatpay", event_id="paid-terminal", raw_digest="5" * 64,
                          order_id=order.id, event_type="payment.succeeded", status="paid",
                          payment_amount_minor=1000, payment_currency="CNY",
                          provider_transaction_id="tx-terminal")
    service.apply_refund(
        order_id=order.id, idempotency_key="refund-terminal", provider_refund_id="rf-terminal",
    )
    assert service.apply_refund(
        order_id=order.id, idempotency_key="refund-terminal", provider_refund_id="rf-terminal",
    ).status == "refunded"
    with pytest.raises(PaymentDomainError, match="退款标识"):
        service.apply_refund(
            order_id=order.id,
            idempotency_key="refund-terminal",
            provider_refund_id="rf-conflict",
        )


def test_database_allows_only_one_full_refund_record_per_order(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
                                 payment_currency="CNY", credit_amount_microusd=100000,
                                 quote_id="q", quote_numerator=100000, quote_denominator=1000,
                                 idempotency_key="o-one-refund")
    order.status = "paid"
    db.commit()
    first = PaymentRefund(
        id=str(uuid.uuid4()), order_id=order.id, user_id=user_id,
        provider_refund_id="rf-one", idempotency_key="refund-one",
        payment_amount_minor=1000, payment_currency="CNY",
        credit_amount_microusd=100000, status="requesting",
    )
    second = PaymentRefund(
        id=str(uuid.uuid4()), order_id=order.id, user_id=user_id,
        provider_refund_id="rf-two", idempotency_key="refund-two",
        payment_amount_minor=1000, payment_currency="CNY",
        credit_amount_microusd=100000, status="requesting",
    )
    db.add(first)
    db.commit()
    db.add(second)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    with pytest.raises(PaymentDomainError, match="并发冲突"):
        service.apply_refund(
            order_id=order.id,
            idempotency_key="refund-three",
            provider_refund_id="rf-three",
        )


def test_refund_is_reserved_before_external_call_and_retry_uses_same_command(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
                                 payment_currency="CNY", credit_amount_microusd=100000,
                                 quote_id="q", quote_numerator=100000, quote_denominator=1000,
                                 idempotency_key="o-refund-reserve")
    service.apply_webhook(provider="wechatpay", event_id="paid-reserve", raw_digest="6" * 64,
                          order_id=order.id, event_type="payment.succeeded", status="paid",
                          payment_amount_minor=1000, payment_currency="CNY",
                          provider_transaction_id="tx-reserve")
    refund, claimed = service.prepare_refund(order_id=order.id, idempotency_key="refund-command")
    assert claimed is True
    assert refund.status == "requesting"
    db.expire_all()
    assert db.get(PaymentOrder, order.id).status == "refunding"
    with pytest.raises(PaymentDomainError, match="正在处理"):
        service.prepare_refund(order_id=order.id, idempotency_key="refund-command")
    with pytest.raises(PaymentDomainError, match="不可退款"):
        service.prepare_refund(order_id=order.id, idempotency_key="another-command")

    refund.status = "pending_reconciliation"
    db.commit()
    retried, claimed = service.prepare_refund(order_id=order.id, idempotency_key="refund-command")
    assert claimed is True and retried.id == refund.id
    completed = service.apply_refund(
        order_id=order.id, idempotency_key="refund-command", provider_refund_id="rf-reserve",
    )
    assert completed.status == "refunded"
    db.expire_all()
    assert db.get(Wallet, user_id).balance_microusd == 0


def test_refund_webhook_completes_crashed_reserved_command(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
                                 payment_currency="CNY", credit_amount_microusd=100000,
                                 quote_id="q", quote_numerator=100000, quote_denominator=1000,
                                 idempotency_key="o-crash-refund")
    service.apply_webhook(provider="wechatpay", event_id="paid-crash", raw_digest="7" * 64,
                          order_id=order.id, event_type="payment.succeeded", status="paid",
                          payment_amount_minor=1000, payment_currency="CNY",
                          provider_transaction_id="tx-crash")
    reserved, claimed = service.prepare_refund(order_id=order.id, idempotency_key="refund-crash")
    assert claimed is True and reserved.status == "requesting"
    # Simulate the process dying after the provider accepted the refund but
    # before the HTTP response was persisted. The authenticated provider
    # webhook must finish the exact reserved command.
    assert service.apply_webhook(
        provider="wechatpay", event_id="refund-crash-event", raw_digest="8" * 64,
        order_id=order.id, event_type="payment.refunded", status="refunded",
        payment_amount_minor=1000, payment_currency="CNY",
        provider_transaction_id="tx-crash", provider_refund_id="rf-crash",
    ) is False
    db.expire_all()
    assert db.get(PaymentOrder, order.id).status == "refunded"
    assert db.get(PaymentRefund, reserved.id).status == "refunded"
    assert db.get(Wallet, user_id).balance_microusd == 0
    final, claimed = service.prepare_refund(order_id=order.id, idempotency_key="refund-crash")
    assert claimed is False and final.provider_refund_id == "rf-crash"


def test_stale_requesting_refund_can_be_atomically_reclaimed(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
                                 payment_currency="CNY", credit_amount_microusd=100000,
                                 quote_id="q", quote_numerator=100000, quote_denominator=1000,
                                 idempotency_key="o-stale-refund")
    service.apply_webhook(provider="wechatpay", event_id="paid-stale", raw_digest="9" * 64,
                          order_id=order.id, event_type="payment.succeeded", status="paid",
                          payment_amount_minor=1000, payment_currency="CNY",
                          provider_transaction_id="tx-stale")
    reserved, _ = service.prepare_refund(order_id=order.id, idempotency_key="refund-stale")
    created_at = reserved.created_at
    reserved.claim_started_at = utcnow() - timedelta(minutes=10)
    db.commit()
    reclaimed, claimed = service.prepare_refund(order_id=order.id, idempotency_key="refund-stale")
    assert claimed is True and reclaimed.status == "retrying"
    assert reclaimed.created_at == created_at
    with pytest.raises(PaymentDomainError, match="正在处理"):
        service.prepare_refund(order_id=order.id, idempotency_key="refund-stale")


def test_pending_and_failed_webhooks_are_persisted_without_credit(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
                                 payment_currency="CNY", credit_amount_microusd=100000,
                                 quote_id="q", quote_numerator=100000, quote_denominator=1000,
                                 idempotency_key="o5")
    assert service.apply_webhook(
        provider="wechatpay", event_id="pending-5", nonce="nonce-pending-5",
        raw_digest="f" * 64, order_id=order.id, event_type="payment.pending",
        status="pending", payment_amount_minor=1000, payment_currency="CNY",
        provider_transaction_id="tx5",
    ) is False
    assert db.get(Wallet, user_id).balance_microusd == 0
    assert service.apply_webhook(
        provider="wechatpay", event_id="failed-5", nonce="nonce-failed-5",
        raw_digest="1" * 64, order_id=order.id, event_type="payment.failed",
        status="failed", payment_amount_minor=1000, payment_currency="CNY",
        provider_transaction_id="tx5",
    ) is False
    db.expire_all()
    assert db.get(PaymentOrder, order.id).status == "failed"
    assert db.get(Wallet, user_id).balance_microusd == 0
    assert len(db.scalars(select(PaymentWebhookEvent).where(
        PaymentWebhookEvent.order_id == order.id,
    )).all()) == 2


def test_closed_webhook_terminates_order_without_credit(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(
        user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
        payment_currency="CNY", credit_amount_microusd=100000,
        quote_id="q", quote_numerator=100000, quote_denominator=1000,
        idempotency_key="o-closed",
    )
    assert service.apply_webhook(
        provider="wechatpay", event_id="closed-1", nonce="nonce-closed-1",
        raw_digest="c" * 64, order_id=order.id, event_type="payment.closed",
        status="closed", payment_amount_minor=1000, payment_currency="CNY",
        provider_transaction_id="tx-closed",
    ) is False
    db.expire_all()
    assert db.get(PaymentOrder, order.id).status == "closed"
    assert db.get(Wallet, user_id).balance_microusd == 0


def test_provider_refund_webhook_reverses_credit_and_is_idempotent(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
                                 payment_currency="CNY", credit_amount_microusd=100000,
                                 quote_id="q", quote_numerator=100000, quote_denominator=1000,
                                 idempotency_key="o6")
    service.apply_webhook(provider="wechatpay", event_id="paid-6", nonce="nonce-paid-6",
                          raw_digest="2" * 64, order_id=order.id,
                          event_type="payment.succeeded", status="paid",
                          payment_amount_minor=1000, payment_currency="CNY",
                          provider_transaction_id="tx6")
    assert service.apply_webhook(
        provider="wechatpay", event_id="refund-6", nonce="nonce-refund-6",
        raw_digest="3" * 64, order_id=order.id, event_type="payment.refunded",
        status="refunded", payment_amount_minor=1000, payment_currency="CNY",
        provider_transaction_id="tx6", provider_refund_id="rf6",
    ) is False
    assert service.apply_webhook(
        provider="wechatpay", event_id="refund-6", nonce="nonce-refund-6",
        raw_digest="3" * 64, order_id=order.id, event_type="payment.refunded",
        status="refunded", payment_amount_minor=1000, payment_currency="CNY",
        provider_transaction_id="tx6", provider_refund_id="rf6",
    ) is True
    db.expire_all()
    assert db.get(PaymentOrder, order.id).status == "refunded"
    assert db.get(Wallet, user_id).balance_microusd == 0
    refunds = db.scalars(select(PaymentRefund).where(PaymentRefund.order_id == order.id)).all()
    assert len(refunds) == 1 and refunds[0].provider_refund_id == "rf6"


def test_refund_webhook_rejects_missing_or_mismatched_refund_identifier(session):
    db, user_id = session
    service = PaymentDomainService(db)
    order = service.create_order(user_id=user_id, provider="wechatpay", payment_amount_minor=1000,
                                 payment_currency="CNY", credit_amount_microusd=100000,
                                 quote_id="q", quote_numerator=100000, quote_denominator=1000,
                                 idempotency_key="o7")
    service.apply_webhook(provider="wechatpay", event_id="paid-7", raw_digest="4" * 64,
                          order_id=order.id, event_type="payment.succeeded", status="paid",
                          payment_amount_minor=1000, payment_currency="CNY",
                          provider_transaction_id="tx7")
    with pytest.raises(PaymentDomainError, match="退款标识"):
        service.apply_webhook(provider="wechatpay", event_id="refund-7", raw_digest="5" * 64,
                              order_id=order.id, event_type="payment.refunded", status="refunded",
                              payment_amount_minor=1000, payment_currency="CNY",
                              provider_transaction_id="tx7")
