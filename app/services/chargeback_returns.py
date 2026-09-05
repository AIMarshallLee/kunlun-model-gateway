"""Full confirmed funds returns only; never infer money movement from 'won'."""

import json
from uuid import uuid4

from sqlalchemy import func, select

from ..models import LedgerEntry, LedgerTransaction, OutboxEvent, PaymentChargeback, PaymentChargebackReturn, PaymentRefund, Wallet
from ..security import utcnow
from .identity import apply_user_freeze
from .ledger import CUSTOMER_AVAILABLE, PLATFORM_CLEARING, PLATFORM_LOSS, PLATFORM_RISK, post_transaction
from .payment_domain import PaymentDomainError


def apply_matching_return(db, order, row):
    """Caller owns User -> Order locks and transaction. Ambiguity stays pending."""
    cases = db.scalars(select(PaymentChargeback).where(PaymentChargeback.order_id == order.id)
                       .with_for_update().execution_options(populate_existing=True)).all()
    case = next((item for item in cases if item.provider_dispute_id == row.provider_dispute_id), None)
    row.chargeback_id = case.id if case else None
    others = db.scalar(select(PaymentChargebackReturn.id).where(
        PaymentChargebackReturn.order_id == order.id, PaymentChargebackReturn.id != row.id).limit(1))
    refund = db.scalar(select(PaymentRefund.id).where(PaymentRefund.order_id == order.id).limit(1))
    if (len(cases) != 1 or case is None or others or refund or order.status != "charged_back"
            or case.status not in {"risk", "recovered", "resolved"}
            or case.risk_reason == "chargeback_funds_returned"
            or row.payment_amount_minor != case.payment_amount_minor
            or row.payment_amount_minor != order.payment_amount_minor):
        return False
    recovered, outstanding, loss = case.recovered_microusd, case.outstanding_microusd, case.written_off_microusd
    expected = {CUSTOMER_AVAILABLE: -recovered, PLATFORM_RISK: -outstanding,
                PLATFORM_LOSS: -loss, PLATFORM_CLEARING: case.credit_amount_microusd}
    actual = dict(db.execute(select(LedgerEntry.account, func.sum(LedgerEntry.amount_microusd))
        .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
        .where(LedgerTransaction.reference == case.id).group_by(LedgerEntry.account)).all())
    if (recovered + outstanding + loss != case.credit_amount_microusd
            or {key: value for key, value in actual.items() if value} != {key: value for key, value in expected.items() if value}):
        row.risk_reason = "chargeback_return_ledger_mismatch"
        return False
    wallet = db.scalar(select(Wallet).where(Wallet.user_id == order.user_id).with_for_update()
                       .execution_options(populate_existing=True))
    if wallet is None:
        raise PaymentDomainError("用户钱包不存在", 500)
    # Spent credits are not new entitlement. Restore only what was recovered;
    # pending model reservations remain untouched.
    wallet.balance_microusd += recovered
    wallet.updated_at = utcnow()
    post_transaction(db, user_id=order.user_id, kind="chargeback_return", reference=case.id,
        idempotency_key=f"chargeback-return:{row.id}", entries=[(account, -amount) for account, amount in expected.items()])
    row.restored_microusd, row.canceled_risk_microusd, row.reversed_loss_microusd = recovered, outstanding, loss
    row.status, row.risk_reason, row.applied_at = "applied", None, utcnow()
    case.outstanding_microusd = 0
    case.status, case.risk_reason, case.resolved_at = "resolved", "chargeback_funds_returned", utcnow()
    order.risk_reason = None
    # Keep the disputed order terminal; no automatic refund/new charge cycle.
    # User stays frozen and revoked credentials are never revived.
    db.add(OutboxEvent(id=str(uuid4()), topic="payment.chargeback.return_applied", reference=row.id,
        payload_json=json.dumps({"return_id": row.id, "chargeback_id": case.id})))
    return True


def record_chargeback_return(db, order, *, provider_dispute_id, provider_return_id, payment_amount_minor):
    existing = db.scalar(select(PaymentChargebackReturn).where(
        PaymentChargebackReturn.provider == order.provider,
        PaymentChargebackReturn.provider_return_id == provider_return_id))
    if existing:
        if (existing.order_id != order.id or existing.provider_dispute_id != provider_dispute_id
                or existing.payment_amount_minor != payment_amount_minor):
            raise PaymentDomainError("返还标识对应的订单、争议或金额不一致", 409)
        return True, existing
    row = PaymentChargebackReturn(id=str(uuid4()), order_id=order.id, user_id=order.user_id,
        provider=order.provider, provider_dispute_id=provider_dispute_id, provider_return_id=provider_return_id,
        payment_amount_minor=payment_amount_minor, payment_currency=order.payment_currency,
        status="pending_reconciliation", risk_reason="chargeback_return_requires_matching_debit")
    db.add(row)
    db.flush()
    apply_user_freeze(db, order.user_id)
    if not apply_matching_return(db, order, row):
        order.risk_reason = "chargeback_return_reconciliation"
        if order.status not in {"paid", "charged_back", "refunded", "refunding", "disputed"}:
            order.status = "disputed"
        db.add(OutboxEvent(id=str(uuid4()), topic="payment.chargeback.return_pending", reference=row.id,
            payload_json=json.dumps({"return_id": row.id, "status": row.status})))
    return False, row


def match_early_return(db, order):
    pending = db.scalars(select(PaymentChargebackReturn).where(
        PaymentChargebackReturn.order_id == order.id,
        PaymentChargebackReturn.status == "pending_reconciliation")).all()
    if len(pending) == 1:
        apply_matching_return(db, order, pending[0])
    if any(row.status == "pending_reconciliation" for row in pending):
        order.risk_reason = "chargeback_return_reconciliation"
