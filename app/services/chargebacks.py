"""Confirmed cash-debit events only. No provider calls and no implicit commits."""

import json
from uuid import uuid4

from sqlalchemy import func, select

from ..models import LedgerEntry, LedgerTransaction, OutboxEvent, PaymentChargeback, PaymentOrder, PaymentRefund, User, Wallet
from ..security import utcnow
from .identity import apply_user_freeze
from .ledger import CUSTOMER_AVAILABLE, PLATFORM_CLEARING, PLATFORM_LOSS, PLATFORM_RISK, post_transaction
from .payment_domain import PaymentDomainError


def record_chargeback(db, order, *, provider_dispute_id, payment_amount_minor):
    """Caller holds User then Order and commits event, freeze and ledger together."""
    existing = db.scalar(select(PaymentChargeback).where(
        PaymentChargeback.provider == order.provider,
        PaymentChargeback.provider_dispute_id == provider_dispute_id))
    if existing:
        if existing.order_id != order.id or existing.payment_amount_minor != payment_amount_minor:
            raise PaymentDomainError("拒付标识对应的订单或金额不一致", 409)
        return True
    previous = db.scalar(select(PaymentChargeback.id).where(PaymentChargeback.order_id == order.id).limit(1))
    refund = db.scalar(select(PaymentRefund.id).where(PaymentRefund.order_id == order.id).limit(1))
    row = PaymentChargeback(id=str(uuid4()), order_id=order.id, user_id=order.user_id,
        provider=order.provider, provider_dispute_id=provider_dispute_id,
        payment_amount_minor=payment_amount_minor, payment_currency=order.payment_currency,
        credit_amount_microusd=order.credit_amount_microusd, recovered_microusd=0,
        outstanding_microusd=0, written_off_microusd=0, status="pending_reconciliation")
    db.add(row)
    # Flush uniqueness before any wallet mutation, including cross-order races.
    db.flush()
    now = utcnow()
    apply_user_freeze(db, order.user_id, now=now)
    if (previous or refund or order.status != "paid" or order.paid_at is None
            or payment_amount_minor != order.payment_amount_minor):
        row.risk_reason = "chargeback_overlap_or_amount_review"
        order.risk_reason = "chargeback_reconciliation"
        # Do not overwrite a previously completed refund/chargeback. Unknown
        # paid/checkout states are stopped so late paid events cannot credit.
        if order.status not in {"refunded", "charged_back", "refunding"}:
            order.status = "disputed"
    else:
        wallet = db.scalar(select(Wallet).where(Wallet.user_id == order.user_id)
            .with_for_update().execution_options(populate_existing=True))
        if wallet is None:
            raise PaymentDomainError("用户钱包不存在", 500)
        row.recovered_microusd = min(wallet.balance_microusd, order.credit_amount_microusd)
        row.outstanding_microusd = order.credit_amount_microusd - row.recovered_microusd
        wallet.balance_microusd -= row.recovered_microusd
        wallet.updated_at = now
        # reserved_microusd belongs to already admitted model work; leave it
        # unchanged. Later release/settlement never cancels the risk record.
        row.status = "risk" if row.outstanding_microusd else "recovered"
        row.risk_reason = "chargeback_balance_insufficient" if row.outstanding_microusd else None
        order.status = "charged_back"
        order.risk_reason = row.risk_reason
        post_transaction(db, user_id=order.user_id, kind="payment_chargeback", reference=row.id,
            idempotency_key=f"chargeback:{row.id}", entries=[
                (CUSTOMER_AVAILABLE, -row.recovered_microusd), (PLATFORM_RISK, -row.outstanding_microusd),
                (PLATFORM_CLEARING, order.credit_amount_microusd)])
    order.checkout_claim_started_at = None
    order.reconciliation_claim_started_at = None
    db.add(OutboxEvent(id=str(uuid4()), topic="payment.chargeback.recorded", reference=row.id,
        payload_json=json.dumps({"chargeback_id": row.id, "status": row.status})))
    return False


def record_refund_overlap(db, order, *, provider_refund_id):
    """Confirmed refund after chargeback: retain evidence, never debit twice."""
    cases = db.scalars(select(PaymentChargeback).where(PaymentChargeback.order_id == order.id)).all()
    if not cases:
        raise PaymentDomainError("争议订单缺少拒付记录", 409)
    refund = db.scalar(select(PaymentRefund).where(PaymentRefund.order_id == order.id))
    if refund is not None:
        if refund.provider_refund_id != provider_refund_id:
            raise PaymentDomainError("退款标识与已记录的重叠事件不一致", 409)
        return True
    db.add(PaymentRefund(id=str(uuid4()), order_id=order.id, user_id=order.user_id,
        provider_refund_id=provider_refund_id, idempotency_key=f"chargeback-overlap:{order.id}",
        payment_amount_minor=order.payment_amount_minor, payment_currency=order.payment_currency,
        credit_amount_microusd=order.credit_amount_microusd, status="pending_reconciliation",
        risk_reason="refund_chargeback_overlap"))
    for case in cases:
        case.status = "pending_reconciliation"
        case.risk_reason = "refund_chargeback_overlap"
    order.risk_reason = "chargeback_reconciliation"
    apply_user_freeze(db, order.user_id)
    db.add(OutboxEvent(id=str(uuid4()), topic="payment.chargeback.overlap", reference=order.id,
        payload_json=json.dumps({"order_id": order.id, "state": "pending_reconciliation"})))
    return False


def resolve_chargeback_risk(db, chargeback_id, *, action, idempotency_key):
    if action not in {"recover_available", "write_off"}:
        raise PaymentDomainError("拒付风险处置动作无效", 422)
    identity = db.execute(select(PaymentChargeback.user_id, PaymentChargeback.order_id)
                          .where(PaymentChargeback.id == chargeback_id)).one_or_none()
    if identity is None:
        raise PaymentDomainError("拒付记录不存在", 404)
    owner, order_id = identity
    db.scalar(select(User).where(User.id == owner).with_for_update())
    order = db.scalar(select(PaymentOrder).where(PaymentOrder.id == order_id).with_for_update()
                      .execution_options(populate_existing=True))
    row = db.scalar(select(PaymentChargeback).where(PaymentChargeback.id == chargeback_id)
        .with_for_update().execution_options(populate_existing=True))
    command = f"chargeback-risk:{row.id}:{idempotency_key}"
    kind = "chargeback_risk_recover" if action == "recover_available" else "chargeback_risk_write_off"
    previous = db.scalar(select(LedgerTransaction).where(LedgerTransaction.idempotency_key == command))
    if previous:
        if previous.kind != kind:
            raise PaymentDomainError("幂等键已用于其他拒付处置动作", 409)
        entries = db.scalars(select(LedgerEntry).where(LedgerEntry.transaction_id == previous.id)).all()
        return row, True, -sum(e.amount_microusd for e in entries if e.account == CUSTOMER_AVAILABLE), -sum(e.amount_microusd for e in entries if e.account == PLATFORM_LOSS)
    if row.status != "risk" or row.outstanding_microusd <= 0:
        raise PaymentDomainError("仅可处置已确认的拒付差额；待对账不能直接核销", 409)
    risk_net = db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_microusd), 0))
        .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
        .where(LedgerTransaction.reference == row.id, LedgerEntry.account == PLATFORM_RISK))
    if -risk_net != row.outstanding_microusd:
        raise PaymentDomainError("拒付差额与账本不一致，停止处置", 409)
    wallet = db.scalar(select(Wallet).where(Wallet.user_id == owner).with_for_update()
        .execution_options(populate_existing=True))
    if wallet is None or wallet.reserved_microusd != 0:
        raise PaymentDomainError("仍有在途预授权或钱包异常，先完成对账", 409)
    outstanding = row.outstanding_microusd
    if action == "recover_available" and wallet.balance_microusd < outstanding:
        raise PaymentDomainError("可用额度不足以全额追回拒付差额", 409)
    recovered = min(wallet.balance_microusd, outstanding)
    written_off = outstanding - recovered
    wallet.balance_microusd -= recovered
    wallet.updated_at = utcnow()
    post_transaction(db, user_id=owner, kind=kind, reference=row.id, idempotency_key=command,
        entries=[(PLATFORM_RISK, outstanding), (CUSTOMER_AVAILABLE, -recovered), (PLATFORM_LOSS, -written_off)])
    row.recovered_microusd += recovered
    row.written_off_microusd += written_off
    row.outstanding_microusd = 0
    row.status = "resolved"
    row.resolved_at = utcnow()
    row.risk_reason = "resolved_written_off" if written_off else "resolved_recovered"
    if order.risk_reason == "chargeback_balance_insufficient":
        order.risk_reason = None
    # User remains frozen. The separate audited unfreeze route checks other
    # cases too and does not revive previously revoked keys or sessions.
    return row, False, recovered, written_off
