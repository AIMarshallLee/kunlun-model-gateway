"""Durable payment state transitions.

This module intentionally contains no network/provider calls.  An adapter may
create a provider order or verify a callback outside the database transaction;
the normalized result is then handed to this service for an atomic transition.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    LedgerEntry, LedgerTransaction, OutboxEvent, PaymentChargeback, PaymentOrder, PaymentRefund,
    PaymentWebhookEvent, User, Wallet,
)
from ..security import as_utc, utcnow
from .identity import apply_user_freeze
from .ledger import (
    CUSTOMER_AVAILABLE, PLATFORM_CLEARING, PLATFORM_LOSS, PLATFORM_RISK,
    post_transaction,
)


class PaymentDomainError(RuntimeError):
    """A safe, client-facing domain rejection."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        self.status_code = status_code
        super().__init__(message)


REFUND_CLAIM_LEASE = timedelta(minutes=5)
CHECKOUT_CLAIM_LEASE = timedelta(minutes=5)
OPEN_CHECKOUT_STATUSES = ("pending", "checkout_requesting", "pending_reconciliation")


class PaymentDomainService:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _same_order_quote(
        order: PaymentOrder, *, provider: str, payment_amount_minor: int,
        payment_currency: str, credit_amount_microusd: int, quote_id: str,
        quote_numerator: int, quote_denominator: int,
    ) -> bool:
        return (
            order.provider,
            order.payment_amount_minor,
            order.payment_currency,
            order.credit_amount_microusd,
            order.quote_id,
            order.quote_numerator,
            order.quote_denominator,
        ) == (
            provider,
            payment_amount_minor,
            payment_currency,
            credit_amount_microusd,
            quote_id,
            quote_numerator,
            quote_denominator,
        )

    def create_order(
        self, *, user_id: str, provider: str, payment_amount_minor: int,
        payment_currency: str, credit_amount_microusd: int, quote_id: str,
        quote_numerator: int, quote_denominator: int, idempotency_key: str,
        max_open_orders: int | None = None,
    ) -> PaymentOrder:
        if payment_amount_minor <= 0 or credit_amount_microusd <= 0:
            raise PaymentDomainError("支付报价金额必须为正数", 422)
        if not payment_currency or len(payment_currency) != 3 or not payment_currency.isupper():
            raise PaymentDomainError("支付币种无效", 422)
        if not provider or not quote_id or quote_numerator <= 0 or quote_denominator <= 0:
            raise PaymentDomainError("支付报价快照不完整", 422)
        # Account freeze, checkout and model reservation all use the User row
        # as their first serialization point. Recheck status inside the money
        # transaction instead of trusting the earlier HTTP authentication.
        owner = self.session.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner is None:
            self.session.rollback()
            raise PaymentDomainError("支付账户不存在", 404)
        if owner.status != "active":
            self.session.rollback()
            raise PaymentDomainError("支付账户不可用", 403)
        existing = self.session.scalar(select(PaymentOrder).where(
            PaymentOrder.user_id == user_id,
            PaymentOrder.client_idempotency_key == idempotency_key,
        ))
        if existing is not None:
            if not self._same_order_quote(
                existing, provider=provider, payment_amount_minor=payment_amount_minor,
                payment_currency=payment_currency,
                credit_amount_microusd=credit_amount_microusd, quote_id=quote_id,
                quote_numerator=quote_numerator, quote_denominator=quote_denominator,
            ):
                raise PaymentDomainError("相同幂等键的支付报价不一致", 409)
            return existing
        if max_open_orders is not None:
            if max_open_orders < 1:
                raise PaymentDomainError("未结支付订单上限无效", 500)
            # The User lock above serializes distinct idempotency keys per
            # account so concurrent requests cannot all pass this count.
            existing = self.session.scalar(select(PaymentOrder).where(
                PaymentOrder.user_id == user_id,
                PaymentOrder.client_idempotency_key == idempotency_key,
            ))
            if existing is not None:
                if not self._same_order_quote(
                    existing, provider=provider, payment_amount_minor=payment_amount_minor,
                    payment_currency=payment_currency,
                    credit_amount_microusd=credit_amount_microusd, quote_id=quote_id,
                    quote_numerator=quote_numerator, quote_denominator=quote_denominator,
                ):
                    self.session.rollback()
                    raise PaymentDomainError("相同幂等键的支付报价不一致", 409)
                self.session.rollback()
                return existing
            open_count = int(self.session.scalar(
                select(func.count(PaymentOrder.id)).where(
                    PaymentOrder.user_id == user_id,
                    PaymentOrder.status.in_(OPEN_CHECKOUT_STATUSES),
                )
            ) or 0)
            if open_count >= max_open_orders:
                self.session.rollback()
                raise PaymentDomainError("未结支付订单过多，请先完成或等待对账", 409)
        order = PaymentOrder(
            id=str(uuid.uuid4()), user_id=user_id, provider=provider,
            payment_amount_minor=payment_amount_minor, payment_currency=payment_currency,
            credit_amount_microusd=credit_amount_microusd, quote_id=quote_id,
            quote_numerator=quote_numerator, quote_denominator=quote_denominator,
            client_idempotency_key=idempotency_key, status="pending",
        )
        self.session.add(order)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            existing = self.session.scalar(select(PaymentOrder).where(
                PaymentOrder.user_id == user_id,
                PaymentOrder.client_idempotency_key == idempotency_key,
            ))
            if existing is not None:
                if not self._same_order_quote(
                    existing, provider=provider, payment_amount_minor=payment_amount_minor,
                    payment_currency=payment_currency,
                    credit_amount_microusd=credit_amount_microusd, quote_id=quote_id,
                    quote_numerator=quote_numerator, quote_denominator=quote_denominator,
                ):
                    raise PaymentDomainError("相同幂等键的支付报价不一致", 409) from exc
                return existing
            raise PaymentDomainError("支付订单创建并发冲突", 409) from exc
        return order

    def prepare_checkout(
        self, *, order_id: str, now: datetime | None = None,
        lease: timedelta = CHECKOUT_CLAIM_LEASE,
    ) -> tuple[PaymentOrder, bool]:
        """Atomically grant one caller permission to create a checkout.

        A stale claim is deliberately not retried: the provider may already
        have created a billable payment intent. Operators must first reconcile
        it by merchant order ID.
        """
        claim_now = as_utc(now or utcnow())
        order_user_id = self.session.scalar(select(PaymentOrder.user_id).where(
            PaymentOrder.id == order_id,
        ))
        if order_user_id is None:
            raise PaymentDomainError("支付订单不存在", 404)
        owner = self.session.scalar(
            select(User)
            .where(User.id == order_user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner is None or owner.status != "active":
            self.session.rollback()
            raise PaymentDomainError("支付账户不可用", 403)
        order = self.session.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if order is None:
            raise PaymentDomainError("支付订单不存在", 404)
        if order.checkout_url and order.provider_transaction_id:
            self.session.rollback()
            return order, False
        if order.status == "checkout_requesting":
            started_at = order.checkout_claim_started_at
            if started_at is not None and as_utc(started_at) > claim_now - lease:
                self.session.rollback()
                raise PaymentDomainError("支付收银台正在创建，不得并发重试", 409)
            changed = self.session.execute(update(PaymentOrder).where(
                PaymentOrder.id == order.id,
                PaymentOrder.status == "checkout_requesting",
                PaymentOrder.checkout_claim_started_at == started_at,
            ).values(
                status="pending_reconciliation",
                risk_reason="checkout_claim_expired",
            ))
            if changed.rowcount != 1:
                self.session.rollback()
                raise PaymentDomainError("支付订单已被并发处理", 409)
            self.session.commit()
            raise PaymentDomainError("支付创建状态不确定，已转人工对账", 503)
        if (
            order.status != "pending"
            or order.provider_transaction_id is not None
            or order.checkout_url is not None
        ):
            self.session.rollback()
            raise PaymentDomainError("订单正在对账或已结束", 409)
        changed = self.session.execute(update(PaymentOrder).where(
            PaymentOrder.id == order.id,
            PaymentOrder.status == "pending",
            PaymentOrder.provider_transaction_id.is_(None),
            PaymentOrder.checkout_url.is_(None),
        ).values(
            status="checkout_requesting",
            checkout_claim_started_at=claim_now,
            risk_reason=None,
        ))
        if changed.rowcount != 1:
            self.session.rollback()
            raise PaymentDomainError("支付订单已被并发处理", 409)
        self.session.commit()
        self.session.refresh(order)
        return order, True

    def apply_webhook(
        self, *, provider: str, event_id: str, raw_digest: str, order_id: str,
        event_type: str, status: str, payment_amount_minor: int,
        payment_currency: str, provider_transaction_id: str, nonce: str | None = None,
        provider_refund_id: str | None = None,
        provider_dispute_id: str | None = None,
    ) -> bool:
        """Persist an already-authenticated event and apply it exactly once."""
        nonce = nonce or event_id
        if not event_id or not nonce or not raw_digest or not provider_transaction_id:
            raise PaymentDomainError("支付事件标识不完整", 422)
        existing = self.session.scalar(select(PaymentWebhookEvent).where(
            PaymentWebhookEvent.provider == provider, PaymentWebhookEvent.event_id == event_id,
        ))
        if existing is not None:
            if existing.raw_digest != raw_digest or existing.nonce != nonce:
                raise PaymentDomainError("同一支付事件正文不一致", 409)
            self.session.rollback()
            return True
        replay = self.session.scalar(select(PaymentWebhookEvent).where(
            PaymentWebhookEvent.provider == provider,
            PaymentWebhookEvent.nonce == nonce,
        ))
        if replay is not None:
            raise PaymentDomainError("支付回调 nonce 已被使用", 409)
        # Cash reversal and model admission share User -> Order -> Wallet.
        # Acquire User before Order on both reversal webhook paths.
        if event_type in {"payment.refunded", "payment.charged_back"}:
            owner_id = self.session.scalar(select(PaymentOrder.user_id).where(PaymentOrder.id == order_id))
            self.session.scalar(select(User).where(User.id == owner_id).with_for_update())
        order = self.session.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if order is None or order.provider != provider:
            raise PaymentDomainError("支付订单不存在", 404)
        chargeback = event_type == "payment.charged_back"
        if (type(payment_amount_minor) is not int or payment_amount_minor <= 0 or
                (not chargeback and payment_amount_minor != order.payment_amount_minor) or
                payment_currency != order.payment_currency):
            raise PaymentDomainError("支付金额或币种与订单报价不一致", 409)
        if (
            order.provider_transaction_id is not None
            and order.provider_transaction_id != provider_transaction_id
        ):
            raise PaymentDomainError("支付交易号与已创建订单不一致", 409)
        valid_event = {
            "payment.pending": "pending",
            "payment.failed": "failed",
            "payment.closed": "closed",
            "payment.succeeded": "paid",
            "topup.succeeded": "paid",
            "payment.refunded": "refunded",
            "payment.charged_back": "charged_back",
        }
        if valid_event.get(event_type) != status:
            raise PaymentDomainError("支付事件类型与状态不一致", 409)
        if status == "refunded" and not provider_refund_id:
            raise PaymentDomainError("退款事件缺少供应商退款标识", 422)
        if status != "refunded" and provider_refund_id is not None:
            raise PaymentDomainError("非退款事件不得携带退款标识", 409)
        if chargeback and not provider_dispute_id:
            raise PaymentDomainError("拒付事件缺少供应商争议标识", 422)
        if not chargeback and provider_dispute_id is not None:
            raise PaymentDomainError("非拒付事件不得携带争议标识", 409)
        event = PaymentWebhookEvent(
            id=str(uuid.uuid4()), provider=provider, event_id=event_id,
            nonce=nonce, order_id=order.id, raw_digest=raw_digest, event_type=event_type,
            status="processed",
        )
        self.session.add(event)
        if chargeback:
            from .chargebacks import record_chargeback
            try:
                order.provider_transaction_id = order.provider_transaction_id or provider_transaction_id
                duplicate = record_chargeback(self.session, order, provider_dispute_id=provider_dispute_id,
                                               payment_amount_minor=payment_amount_minor)
                event.status = "processed_duplicate" if duplicate else "processed"
                self.session.commit()
            except IntegrityError as exc:
                self.session.rollback()
                raise PaymentDomainError("拒付事件并发冲突，请用原事件重试", 409) from exc
            return duplicate
        if status == "refunded":
            if order.status in {"charged_back", "disputed"}:
                from .chargebacks import record_refund_overlap
                try:
                    duplicate = record_refund_overlap(self.session, order, provider_refund_id=provider_refund_id)
                    event.status = "pending_reconciliation"
                    self.session.commit()
                except IntegrityError as exc:
                    self.session.rollback()
                    raise PaymentDomainError("退款与拒付重叠事件并发冲突", 409) from exc
                return duplicate
            if order.status == "refunded":
                existing_refund = self.session.scalar(select(PaymentRefund).where(
                    PaymentRefund.provider_refund_id == provider_refund_id,
                ))
                if existing_refund is None or existing_refund.order_id != order.id:
                    raise PaymentDomainError("退款标识与已退款订单不一致", 409)
                event.status = "processed_duplicate"
                try:
                    self.session.commit()
                except IntegrityError as exc:
                    self.session.rollback()
                    raise PaymentDomainError("退款事件并发冲突", 409) from exc
                return True
            refund_idempotency_key = f"webhook:{provider}:{event_id}"
            if order.status == "refunding":
                active_refunds = self.session.scalars(select(PaymentRefund).where(
                    PaymentRefund.order_id == order.id,
                    PaymentRefund.status.in_(("requesting", "retrying", "pending_reconciliation")),
                )).all()
                if len(active_refunds) != 1:
                    raise PaymentDomainError("退款命令状态异常，需要人工核对", 409)
                active_refund = active_refunds[0]
                if active_refund.provider_refund_id not in {None, provider_refund_id}:
                    raise PaymentDomainError("供应商退款标识不一致", 409)
                refund_idempotency_key = active_refund.idempotency_key
            elif order.status != "paid":
                raise PaymentDomainError("支付订单状态不允许退款", 409)
            # apply_refund commits the pending webhook event and the ledger
            # reversal in the same database transaction.
            self.apply_refund(
                order_id=order.id,
                idempotency_key=refund_idempotency_key,
                provider_refund_id=provider_refund_id,
            )
            # apply_refund normally commits the ledger transition. A terminal
            # replay can return without doing so, and this explicit commit
            # preserves the authenticated webhook event in that race.
            self.session.commit()
            return False
        if status in {"pending", "failed", "closed"}:
            if order.status not in {
                "pending", "pending_reconciliation", "checkout_requesting",
            }:
                raise PaymentDomainError("支付订单状态不允许更新", 409)
            order.provider_transaction_id = order.provider_transaction_id or provider_transaction_id
            order.reconciliation_claim_started_at = None
            if status in {"failed", "closed"}:
                order.status = status
                order.checkout_claim_started_at = None
            elif order.status == "checkout_requesting":
                # A callback can beat the checkout HTTP response. We now know
                # a provider-side intent exists, but do not yet have a trusted
                # checkout URL, so retrying creation is unsafe.
                order.status = "pending_reconciliation"
                order.risk_reason = "checkout_callback_before_response"
                order.checkout_claim_started_at = None
            try:
                self.session.commit()
            except IntegrityError as exc:
                self.session.rollback()
                raise PaymentDomainError("支付事件并发冲突", 409) from exc
            return False
        if order.status not in {
            "pending", "pending_reconciliation", "checkout_requesting",
        }:
            if order.provider_transaction_id == provider_transaction_id and order.status == "paid":
                self.session.rollback()
                return True
            raise PaymentDomainError("支付订单状态或交易号不一致", 409)
        changed = self.session.execute(update(PaymentOrder).where(
            PaymentOrder.id == order.id,
            PaymentOrder.status.in_(("pending", "pending_reconciliation", "checkout_requesting")),
        ).values(
            status="paid",
            provider_transaction_id=provider_transaction_id,
            paid_at=utcnow(),
            checkout_claim_started_at=None,
            reconciliation_claim_started_at=None,
            risk_reason=None,
        ))
        if changed.rowcount != 1:
            self.session.rollback()
            raise PaymentDomainError("支付订单已被并发处理", 409)
        wallet = self.session.scalar(
            select(Wallet)
            .where(Wallet.user_id == order.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if wallet is None:
            raise PaymentDomainError("用户钱包不存在", 500)
        wallet.balance_microusd += order.credit_amount_microusd
        wallet.updated_at = utcnow()
        post_transaction(self.session, user_id=order.user_id, kind="payment_credit",
                          reference=order.id, idempotency_key=f"payment:{provider}:{event_id}",
                          entries=[(CUSTOMER_AVAILABLE, order.credit_amount_microusd),
                                   (PLATFORM_CLEARING, -order.credit_amount_microusd)])
        self.session.add(OutboxEvent(
            id=str(uuid.uuid4()), topic="wallet.credited", reference=order.id,
            payload_json=json.dumps({"order_id": order.id, "credit_amount_microusd": order.credit_amount_microusd}, separators=(",", ":")),
        ))
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise PaymentDomainError("支付事件并发冲突", 409) from exc
        return False

    def prepare_refund(
        self, *, order_id: str, idempotency_key: str,
        now: datetime | None = None, lease: timedelta = REFUND_CLAIM_LEASE,
    ) -> tuple[PaymentRefund, bool]:
        """Reserve one refund command before any external side effect.

        ``claimed`` is true only for the caller allowed to contact the payment
        provider. A timed-out command can be claimed again with the exact same
        idempotency key. ``claim_started_at`` advances on every successful
        claim while ``created_at`` remains immutable audit history.
        """
        if not idempotency_key:
            raise PaymentDomainError("退款幂等键不能为空", 422)
        # Serialize initial/retry refund claims with chargeback admission.
        self.session.scalar(select(PaymentOrder).where(PaymentOrder.id == order_id).with_for_update())
        if self.session.scalar(select(PaymentChargeback.id).where(PaymentChargeback.order_id == order_id).limit(1)):
            raise PaymentDomainError("订单存在拒付记录，禁止自动发起或重试退款", 409)
        existing = self.session.scalar(select(PaymentRefund).where(
            PaymentRefund.order_id == order_id, PaymentRefund.idempotency_key == idempotency_key,
        ))
        if existing is not None:
            if existing.status in {"refunded", "risk"}:
                self.session.rollback()
                return existing, False
            claim_now = as_utc(now or utcnow())
            if existing.status in {"requesting", "retrying"}:
                if as_utc(existing.claim_started_at) > claim_now - lease:
                    self.session.rollback()
                    raise PaymentDomainError("退款正在处理，不得并发重试", 409)
                next_status = "retrying" if existing.status == "requesting" else "requesting"
                changed = self.session.execute(update(PaymentRefund).where(
                    PaymentRefund.id == existing.id,
                    PaymentRefund.status == existing.status,
                    PaymentRefund.claim_started_at == existing.claim_started_at,
                ).values(status=next_status, claim_started_at=claim_now, risk_reason=None))
                if changed.rowcount != 1:
                    self.session.rollback()
                    raise PaymentDomainError("退款命令已被并发处理", 409)
                self.session.commit()
                self.session.refresh(existing)
                return existing, True
            if existing.status != "pending_reconciliation":
                self.session.rollback()
                raise PaymentDomainError("退款状态需要人工核对", 409)
            changed = self.session.execute(update(PaymentRefund).where(
                PaymentRefund.id == existing.id,
                PaymentRefund.status == "pending_reconciliation",
            ).values(status="requesting", claim_started_at=claim_now, risk_reason=None))
            if changed.rowcount != 1:
                self.session.rollback()
                raise PaymentDomainError("退款命令已被并发处理", 409)
            self.session.commit()
            self.session.refresh(existing)
            return existing, True

        order = self.session.scalar(select(PaymentOrder).where(
            PaymentOrder.id == order_id,
        ).with_for_update())
        if order is None or order.status != "paid":
            raise PaymentDomainError("订单当前不可退款", 409)
        if order.payment_amount_minor is None or order.payment_currency is None:
            raise PaymentDomainError("订单缺少现金报价快照", 409)
        refund = PaymentRefund(
            id=str(uuid.uuid4()), order_id=order.id, user_id=order.user_id,
            provider_refund_id=None, idempotency_key=idempotency_key,
            payment_amount_minor=order.payment_amount_minor, payment_currency=order.payment_currency,
            credit_amount_microusd=order.credit_amount_microusd, status="requesting",
        )
        self.session.add(refund)
        changed = self.session.execute(update(PaymentOrder).where(
            PaymentOrder.id == order.id,
            PaymentOrder.status == "paid",
        ).values(status="refunding"))
        if changed.rowcount != 1:
            self.session.rollback()
            raise PaymentDomainError("退款命令已被并发处理", 409)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise PaymentDomainError("退款命令并发冲突", 409) from exc
        return refund, True

    def apply_refund(self, *, order_id: str, idempotency_key: str, provider_refund_id: str) -> PaymentRefund:
        order_owner_id = self.session.scalar(select(PaymentOrder.user_id).where(
            PaymentOrder.id == order_id,
        ))
        if order_owner_id is None:
            raise PaymentDomainError("订单当前不可退款", 409)
        # Lock order is User -> PaymentOrder -> Wallet whenever this method is
        # the transaction entrypoint. The webhook path may already own the
        # order lock, but still acquires User before Wallet, preventing the
        # User/Wallet inversion with model reservations.
        self.session.scalar(
            select(User)
            .where(User.id == order_owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        order = self.session.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        existing = self.session.scalar(select(PaymentRefund).where(
            PaymentRefund.order_id == order_id, PaymentRefund.idempotency_key == idempotency_key,
        ))
        if existing is not None and existing.status in {"refunded", "risk"}:
            if existing.provider_refund_id != provider_refund_id:
                raise PaymentDomainError("供应商退款标识与已完成退款不一致", 409)
            return existing
        if order is None or order.status not in {"paid", "refunding", "refunded"}:
            raise PaymentDomainError("订单当前不可退款", 409)
        if order.status == "refunded":
            raise PaymentDomainError("订单已完成退款", 409)
        if order.payment_amount_minor is None or order.payment_currency is None:
            raise PaymentDomainError("订单缺少现金报价快照", 409)
        if existing is None:
            refund = PaymentRefund(
                id=str(uuid.uuid4()), order_id=order.id, user_id=order.user_id,
                provider_refund_id=provider_refund_id, idempotency_key=idempotency_key,
                payment_amount_minor=order.payment_amount_minor, payment_currency=order.payment_currency,
                credit_amount_microusd=order.credit_amount_microusd, status="requesting",
            )
            self.session.add(refund)
            # Force the per-order uniqueness check before any later query can
            # trigger an implicit autoflush. This turns a concurrent second
            # full-refund command into the stable domain-level 409 below.
            try:
                self.session.flush()
            except IntegrityError as exc:
                self.session.rollback()
                raise PaymentDomainError("退款并发冲突", 409) from exc
        else:
            refund = existing
            if refund.status not in {"requesting", "retrying", "pending_reconciliation"}:
                raise PaymentDomainError("退款状态不允许完成", 409)
            if refund.provider_refund_id not in {None, provider_refund_id}:
                raise PaymentDomainError("供应商退款标识不一致", 409)
            refund.provider_refund_id = provider_refund_id
        wallet = self.session.scalar(
            select(Wallet)
            .where(Wallet.user_id == order.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if wallet is None:
            raise PaymentDomainError("用户钱包不存在", 500)
        now = utcnow()
        if wallet.balance_microusd >= order.credit_amount_microusd:
            wallet.balance_microusd -= order.credit_amount_microusd
            wallet.updated_at = now
            refund.status = "refunded"
            post_transaction(self.session, user_id=order.user_id, kind="payment_refund",
                              # The client key is scoped to one refund command
                              # (and therefore one order), while the ledger
                              # key is global. Namespace it with the
                              # immutable refund id so two orders may safely
                              # reuse the same client idempotency key.
                              reference=order.id, idempotency_key=f"refund:{refund.id}",
                              entries=[(CUSTOMER_AVAILABLE, -order.credit_amount_microusd),
                                       (PLATFORM_CLEARING, order.credit_amount_microusd)])
        else:
            recovered = max(0, min(wallet.balance_microusd, order.credit_amount_microusd))
            shortfall = order.credit_amount_microusd - recovered
            wallet.balance_microusd -= recovered
            wallet.updated_at = now
            apply_user_freeze(self.session, order.user_id, now=now)
            order.risk_reason = "refund_balance_insufficient"
            refund.status = "risk"
            refund.risk_reason = "refund_balance_insufficient"
            post_transaction(self.session, user_id=order.user_id, kind="payment_refund_risk",
                              reference=order.id, idempotency_key=f"refund-risk:{refund.id}",
                              entries=[(CUSTOMER_AVAILABLE, -recovered),
                                       (PLATFORM_RISK, -shortfall),
                                       (PLATFORM_CLEARING, order.credit_amount_microusd)])
        order.status = "refunded"
        order.refunded_at = now
        order.reconciliation_claim_started_at = None
        refund.completed_at = now
        self.session.add(OutboxEvent(
            id=str(uuid.uuid4()), topic="payment.refund.completed", reference=order.id,
            payload_json=json.dumps({"order_id": order.id, "status": refund.status}, separators=(",", ":")),
        ))
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise PaymentDomainError("退款并发冲突", 409) from exc
        return refund

    def resolve_refund_risk(
        self, *, refund_id: str, action: str, idempotency_key: str,
    ) -> tuple[PaymentRefund, bool, int, int, int]:
        """Resolve a cash-refund shortfall after every model hold is cleared.

        This method deliberately does not commit. The ops route adds its
        OperatorAction and commits the wallet, immutable ledger, refund state
        and audit record as one transaction.
        """
        if action not in {"recover_available", "write_off"}:
            raise PaymentDomainError("退款风险处置动作无效", 422)
        refund = self.session.scalar(
            select(PaymentRefund)
            .where(PaymentRefund.id == refund_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if refund is None:
            raise PaymentDomainError("退款风险记录不存在", 404)
        disposition_key = f"refund-risk-disposition:{refund.id}:{idempotency_key}"
        expected_kind = (
            "refund_risk_recover"
            if action == "recover_available"
            else "refund_risk_write_off"
        )
        existing_tx = self.session.scalar(select(LedgerTransaction).where(
            LedgerTransaction.idempotency_key == disposition_key,
        ))
        if existing_tx is not None:
            if existing_tx.kind != expected_kind:
                raise PaymentDomainError("同一幂等键已用于不同风险处置动作", 409)
            entries = self.session.scalars(select(LedgerEntry).where(
                LedgerEntry.transaction_id == existing_tx.id,
            )).all()
            recovered = -sum(
                entry.amount_microusd
                for entry in entries if entry.account == CUSTOMER_AVAILABLE
            )
            written_off = -sum(
                entry.amount_microusd
                for entry in entries if entry.account == PLATFORM_LOSS
            )
            return refund, True, recovered + written_off, recovered, written_off
        if refund.status != "risk":
            raise PaymentDomainError("退款记录不处于待处置风险状态", 409)
        order = self.session.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == refund.order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if order is None or order.status != "refunded":
            raise PaymentDomainError("退款订单状态异常", 409)
        risk_net = int(self.session.scalar(
            select(func.coalesce(func.sum(LedgerEntry.amount_microusd), 0))
            .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
            .where(
                LedgerTransaction.reference == refund.order_id,
                LedgerTransaction.kind.in_((
                    "payment_refund_risk",
                    "refund_risk_recover",
                    "refund_risk_write_off",
                )),
                LedgerEntry.account == PLATFORM_RISK,
            )
        ) or 0)
        outstanding = -risk_net
        if outstanding <= 0:
            raise PaymentDomainError("退款风险差额已结清或账本状态异常", 409)
        wallet = self.session.scalar(
            select(Wallet)
            .where(Wallet.user_id == refund.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if wallet is None:
            raise PaymentDomainError("用户钱包不存在", 500)
        if wallet.reserved_microusd != 0:
            raise PaymentDomainError("仍有在途预授权，必须先完成结算或释放", 409)
        if action == "recover_available" and wallet.balance_microusd < outstanding:
            raise PaymentDomainError("当前可用额度不足以全额追回风险差额", 409)
        recovered = (
            outstanding
            if action == "recover_available"
            else min(wallet.balance_microusd, outstanding)
        )
        written_off = outstanding - recovered
        wallet.balance_microusd -= recovered
        wallet.updated_at = utcnow()
        entries = [(PLATFORM_RISK, outstanding)]
        if recovered:
            entries.append((CUSTOMER_AVAILABLE, -recovered))
        if written_off:
            entries.append((PLATFORM_LOSS, -written_off))
        post_transaction(
            self.session,
            user_id=refund.user_id,
            kind=expected_kind,
            reference=refund.order_id,
            idempotency_key=disposition_key,
            entries=entries,
        )
        refund.status = "resolved"
        refund.risk_reason = (
            "resolved_recovered" if written_off == 0 else "resolved_written_off"
        )
        if order.risk_reason == "refund_balance_insufficient":
            order.risk_reason = None
        self.session.add(OutboxEvent(
            id=str(uuid.uuid4()),
            topic="payment.refund.risk_resolved",
            reference=refund.id,
            payload_json=json.dumps({
                "refund_id": refund.id,
                "recovered_microusd": recovered,
                "written_off_microusd": written_off,
            }, separators=(",", ":")),
        ))
        return refund, False, outstanding, recovered, written_off
