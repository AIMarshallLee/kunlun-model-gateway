"""Test-payment order and webhook processing.

This adapter deliberately cannot be enabled as live payments. Real providers need
their official SDK, certificate validation, timestamp checks and reconciliation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import OutboxEvent, PaymentOrder, PaymentWebhookEvent, Wallet
from ..security import utcnow
from .ledger import CUSTOMER_AVAILABLE, PLATFORM_CLEARING, post_transaction


class PaymentError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        self.status_code = status_code
        super().__init__(message)


def create_test_order(session: Session, user_id: str, amount: int) -> PaymentOrder:
    if amount < 100 or amount > 100_000_000:
        raise PaymentError("测试充值金额超出允许范围", 422)
    order = PaymentOrder(
        id=str(uuid.uuid4()),
        user_id=user_id,
        credit_amount_microusd=amount,
        payment_amount_minor=None,
        payment_currency=None,
        provider="test_hmac",
    )
    session.add(order)
    session.commit()
    return order


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def process_test_webhook(
    session: Session,
    *,
    raw_body: bytes,
    signature: str,
    secret: str,
) -> bool:
    if not verify_signature(raw_body, signature, secret):
        raise PaymentError("支付回调签名无效", 401)
    try:
        event = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PaymentError("支付回调不是有效 JSON", 400) from exc
    event_id = str(event.get("id") or "")
    order_id = str(event.get("order_id") or "")
    event_type = str(event.get("type") or "")
    amount = event.get("amount")
    if not event_id or not order_id or event_type != "topup.succeeded" or not isinstance(amount, int):
        raise PaymentError("支付回调字段不完整", 422)
    raw_digest = hashlib.sha256(raw_body).hexdigest()
    existing = session.scalar(select(PaymentWebhookEvent).where(
        PaymentWebhookEvent.provider == "test_hmac",
        PaymentWebhookEvent.event_id == event_id,
    ))
    if existing is not None:
        if existing.raw_digest != raw_digest:
            raise PaymentError("同一事件编号的回调正文不一致", 409)
        return True
    order = session.scalar(select(PaymentOrder).where(PaymentOrder.id == order_id))
    if order is None or order.provider != "test_hmac":
        raise PaymentError("充值订单不存在", 404)
    if amount != order.amount_microusd:
        raise PaymentError("支付金额与订单不一致", 409)
    if order.status != "pending":
        session.rollback()
        concurrent = session.scalar(select(PaymentWebhookEvent).where(
            PaymentWebhookEvent.provider == "test_hmac",
            PaymentWebhookEvent.event_id == event_id,
        ))
        if concurrent is not None and concurrent.raw_digest == raw_digest:
            return True
        raise PaymentError("充值订单状态不允许入账", 409)
    try:
        session.add(PaymentWebhookEvent(
            id=str(uuid.uuid4()),
            provider="test_hmac",
            event_id=event_id,
            nonce=event_id,
            order_id=order.id,
            raw_digest=raw_digest,
            event_type=event_type,
        ))
        result = session.execute(update(PaymentOrder).where(
            PaymentOrder.id == order.id,
            PaymentOrder.status == "pending",
        ).values(
            status="paid",
            provider_transaction_id=event_id,
            paid_at=utcnow(),
        ))
        if result.rowcount != 1:
            session.rollback()
            concurrent = session.scalar(select(PaymentWebhookEvent).where(
                PaymentWebhookEvent.provider == "test_hmac",
                PaymentWebhookEvent.event_id == event_id,
            ))
            if concurrent is not None and concurrent.raw_digest == raw_digest:
                return True
            raise PaymentError("充值订单已被并发处理", 409)
        session.execute(update(Wallet).where(Wallet.user_id == order.user_id).values(
            balance_microusd=Wallet.balance_microusd + amount,
            updated_at=utcnow(),
        ))
        post_transaction(
            session,
            user_id=order.user_id,
            kind="credit",
            reference=order.id,
            idempotency_key=f"payment:test_hmac:{event_id}",
            entries=[
                (CUSTOMER_AVAILABLE, amount),
                (PLATFORM_CLEARING, -amount),
            ],
        )
        session.add(OutboxEvent(
            id=str(uuid.uuid4()),
            topic="wallet.credited",
            reference=order.id,
            payload_json=json.dumps({"order_id": order.id, "amount_microusd": amount}, separators=(",", ":")),
        ))
        session.commit()
    except IntegrityError:
        session.rollback()
        duplicate = session.scalar(select(PaymentWebhookEvent).where(
            PaymentWebhookEvent.provider == "test_hmac",
            PaymentWebhookEvent.event_id == event_id,
        ))
        if duplicate is not None and duplicate.raw_digest == raw_digest:
            return True
        raise PaymentError("支付回调并发冲突", 409)
    return False
