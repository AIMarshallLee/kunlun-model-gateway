"""Persistence models. Monetary fields are integer microUSD; no floating money."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .security import utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AccessSession(Base):
    __tablename__ = "access_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"
    __table_args__ = (UniqueConstraint("token_digest", name="uq_email_verify_token_digest"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_digest: Mapped[str] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (UniqueConstraint("token_digest", name="uq_password_reset_token_digest"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_digest: Mapped[str] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    secret_digest: Mapped[str] = mapped_column(String(64))
    last_four: Mapped[str] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        CheckConstraint("balance_microusd >= 0", name="wallet_balance_nonnegative"),
        CheckConstraint("reserved_microusd >= 0", name="wallet_reserved_nonnegative"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    currency: Mapped[str] = mapped_column(String(16), default="microUSD")
    balance_microusd: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_microusd: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    reference: Mapped[str] = mapped_column(String(80), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("amount_microusd != 0", name="ledger_entry_nonzero"),
        Index("ix_ledger_entries_transaction_user", "transaction_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    account: Mapped[str] = mapped_column(String(48), index=True)
    amount_microusd: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaymentOrder(Base):
    __tablename__ = "payment_orders"
    __table_args__ = (
        CheckConstraint("credit_amount_microusd > 0", name="payment_credit_amount_positive"),
        CheckConstraint(
            "payment_amount_minor IS NULL OR payment_amount_minor > 0",
            name="payment_cash_amount_positive",
        ),
        CheckConstraint("quote_numerator > 0", name="payment_quote_numerator_positive"),
        CheckConstraint("quote_denominator > 0", name="payment_quote_denominator_positive"),
        UniqueConstraint("user_id", "client_idempotency_key", name="uq_payment_user_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="test_hmac")
    # Customer credit and provider cash are intentionally different units.
    # The old API name `amount` always refers to service credit, never cash.
    credit_amount_microusd: Mapped[int] = mapped_column(BigInteger)
    payment_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payment_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    quote_numerator: Mapped[int] = mapped_column(BigInteger, default=1)
    quote_denominator: Mapped[int] = mapped_column(BigInteger, default=1)
    quote_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    client_idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    provider_transaction_id: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    checkout_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    checkout_claim_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    reconciliation_claim_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    risk_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def amount_microusd(self) -> int:
        """Compatibility view: service credit amount, never provider cash."""
        return self.credit_amount_microusd

    @property
    def currency(self) -> str:
        """Compatibility view for service credit; provider cash uses payment_currency."""
        return "microUSD"


class PaymentWebhookEvent(Base):
    __tablename__ = "payment_webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_payment_provider_event"),
        UniqueConstraint("provider", "nonce", name="uq_payment_provider_nonce"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[str] = mapped_column(String(120))
    nonce: Mapped[str] = mapped_column(String(120))
    order_id: Mapped[str] = mapped_column(ForeignKey("payment_orders.id", ondelete="RESTRICT"), index=True)
    raw_digest: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(48), default="unknown")
    status: Mapped[str] = mapped_column(String(24), default="processed")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (
        CheckConstraint("limit_microusd > 0", name="budget_limit_positive"),
        CheckConstraint("reserved_microusd >= 0", name="budget_reserved_nonnegative"),
        CheckConstraint("spent_microusd >= 0", name="budget_spent_nonnegative"),
        CheckConstraint("spent_microusd + reserved_microusd <= limit_microusd", name="budget_within_limit"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    limit_microusd: Mapped[int] = mapped_column(BigInteger)
    reserved_microusd: Mapped[int] = mapped_column(BigInteger, default=0)
    spent_microusd: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelPrice(Base):
    __tablename__ = "model_prices"
    __table_args__ = (
        CheckConstraint("input_microusd_per_million >= 0", name="model_input_price_nonnegative"),
        CheckConstraint("output_microusd_per_million >= 0", name="model_output_price_nonnegative"),
        UniqueConstraint("model", "version", name="uq_model_price_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    model: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    input_microusd_per_million: Mapped[int] = mapped_column(BigInteger)
    output_microusd_per_million: Mapped[int] = mapped_column(BigInteger)
    max_output_tokens: Mapped[int] = mapped_column(BigInteger, default=4096)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelRequest(Base):
    __tablename__ = "model_requests"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_request_user_idempotency"),
        CheckConstraint("reserved_microusd >= 0", name="request_reserved_nonnegative"),
        CheckConstraint("charged_microusd >= 0", name="request_charged_nonnegative"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id", ondelete="RESTRICT"), index=True)
    budget_id: Mapped[str | None] = mapped_column(
        ForeignKey("budgets.id", ondelete="RESTRICT"), nullable=True, index=True,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    requested_model: Mapped[str] = mapped_column(String(120))
    final_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    final_provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="reserved", index=True)
    price_version: Mapped[int] = mapped_column(Integer)
    input_price: Mapped[int] = mapped_column(BigInteger)
    output_price: Mapped[int] = mapped_column(BigInteger)
    reserved_microusd: Mapped[int] = mapped_column(BigInteger)
    charged_microusd: Mapped[int] = mapped_column(BigInteger, default=0)
    upstream_cost_microusd: Mapped[int] = mapped_column(BigInteger, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    usage_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProviderAttempt(Base):
    __tablename__ = "provider_attempts"
    __table_args__ = (UniqueConstraint("request_id", "ordinal", name="uq_request_attempt_ordinal"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("model_requests.id", ondelete="RESTRICT"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(32))
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RateLimitCounter(Base):
    __tablename__ = "rate_limit_counters"
    __table_args__ = (UniqueConstraint("api_key_id", "window_epoch", name="uq_rate_key_window"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"), index=True)
    window_epoch: Mapped[int] = mapped_column(Integer, index=True)
    count: Mapped[int] = mapped_column(Integer, default=1)


class AuthRateLimitCounter(Base):
    __tablename__ = "auth_rate_limit_counters"
    __table_args__ = (UniqueConstraint("subject_digest", "action", "window_epoch", name="uq_auth_subject_window"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subject_digest: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(24), index=True)
    window_epoch: Mapped[int] = mapped_column(Integer, index=True)
    count: Mapped[int] = mapped_column(Integer, default=1)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    topic: Mapped[str] = mapped_column(String(80), index=True)
    reference: Mapped[str] = mapped_column(String(120), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OperatorAction(Base):
    __tablename__ = "operator_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str | None] = mapped_column(ForeignKey("model_requests.id", ondelete="RESTRICT"), nullable=True, index=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(String(500))
    actor: Mapped[str] = mapped_column(String(200), default="legacy-operator")
    scopes: Mapped[str] = mapped_column(String(500), default="")
    token_id: Mapped[str] = mapped_column(String(120), default="legacy")
    operation_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_ip_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    before_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    after_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaymentRefund(Base):
    __tablename__ = "payment_refunds"
    __table_args__ = (
        CheckConstraint("payment_amount_minor > 0", name="refund_cash_amount_positive"),
        CheckConstraint("credit_amount_microusd > 0", name="refund_credit_amount_positive"),
        UniqueConstraint("order_id", name="uq_refund_order"),
        UniqueConstraint("order_id", "idempotency_key", name="uq_refund_order_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("payment_orders.id", ondelete="RESTRICT"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    provider_refund_id: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    payment_amount_minor: Mapped[int] = mapped_column(BigInteger)
    payment_currency: Mapped[str] = mapped_column(String(3))
    credit_amount_microusd: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    risk_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    claim_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SafetyAudit(Base):
    __tablename__ = "safety_audits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id", ondelete="RESTRICT"), index=True)
    request_id: Mapped[str | None] = mapped_column(ForeignKey("model_requests.id", ondelete="RESTRICT"), nullable=True, index=True)
    phase: Mapped[str] = mapped_column(String(16), index=True)
    outcome: Mapped[str] = mapped_column(String(24), index=True)
    reason_code: Mapped[str] = mapped_column(String(64))
    decision_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


Index("ix_ledger_user_created", LedgerEntry.user_id, LedgerEntry.created_at)
Index("ix_model_request_user_created", ModelRequest.user_id, ModelRequest.created_at)
