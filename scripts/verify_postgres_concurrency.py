"""Destructive-to-test-data PostgreSQL concurrency verification.

Run only against an isolated validation database. The script creates one
ephemeral account and keeps its audit rows so database invariants remain
observable after completion.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
from threading import Barrier
import uuid

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db_guards import SCHEMA_HEAD, assert_schema_revision
from app.models import LedgerEntry, LedgerTransaction, PaymentOrder, PaymentRefund, User, Wallet
from app.security import utcnow
from app.services.ledger import CUSTOMER_AVAILABLE, PLATFORM_CLEARING, post_transaction
from app.services.payment_domain import PaymentDomainError, PaymentDomainService


ISOLATED_DATABASE_CONFIRMATION = "YES_ISOLATED_TEST_DATABASE"


def _require_isolated_database_confirmation() -> bool:
    """Return whether the caller explicitly acknowledged destructive test data."""
    return os.getenv("KUNLUN_CONFIRM_TEST_DATABASE") == ISOLATED_DATABASE_CONFIRMATION


def _database_url() -> str:
    url = os.getenv("KUNLUN_TEST_POSTGRES_URL", "")
    if not url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise RuntimeError("KUNLUN_TEST_POSTGRES_URL 必须指向隔离的 PostgreSQL 验证库")
    return url


def main() -> int:
    if not _require_isolated_database_confirmation():
        print(
            "拒绝执行：该并发验收会写入并保留测试数据。请仅对可丢弃的隔离库设置 "
            "KUNLUN_CONFIRM_TEST_DATABASE=YES_ISOLATED_TEST_DATABASE。",
            file=sys.stderr,
        )
        return 2
    engine = create_engine(_database_url(), future=True, pool_size=6, max_overflow=0)
    if engine.dialect.name != "postgresql":
        raise RuntimeError("该验收只能在 PostgreSQL 上运行")
    assert_schema_revision(engine, SCHEMA_HEAD)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    run_id = uuid.uuid4().hex
    user_id = str(uuid.uuid4())
    order_ids: list[str] = []

    with session_factory() as session:
        session.add(User(
            id=user_id,
            email=f"pg-concurrency-{run_id}@example.invalid",
            password_hash="test-only",
            status="active",
            email_verified_at=utcnow(),
        ))
        session.add(Wallet(user_id=user_id, balance_microusd=150_000))
        session.flush()
        post_transaction(
            session,
            user_id=user_id,
            kind="postgres_concurrency_seed",
            reference=run_id,
            idempotency_key=f"pg-seed:{run_id}",
            entries=[(CUSTOMER_AVAILABLE, 150_000), (PLATFORM_CLEARING, -150_000)],
        )
        for index in range(2):
            order = PaymentOrder(
                id=str(uuid.uuid4()),
                user_id=user_id,
                provider="wechatpay",
                credit_amount_microusd=100_000,
                payment_amount_minor=1_000,
                payment_currency="CNY",
                quote_numerator=100_000,
                quote_denominator=1_000,
                quote_id=f"pg-refund-{run_id}-{index}",
                client_idempotency_key=f"pg-refund-{run_id}-{index}",
                status="paid",
                provider_transaction_id=f"pg-txn-{run_id}-{index}",
                paid_at=utcnow(),
            )
            session.add(order)
            order_ids.append(order.id)
        checkout_order = PaymentOrder(
            id=str(uuid.uuid4()),
            user_id=user_id,
            provider="wechatpay",
            credit_amount_microusd=100_000,
            payment_amount_minor=1_000,
            payment_currency="CNY",
            quote_numerator=100_000,
            quote_denominator=1_000,
            quote_id=f"pg-checkout-{run_id}",
            client_idempotency_key=f"pg-checkout-{run_id}",
            status="pending",
        )
        session.add(checkout_order)
        session.commit()
        checkout_order_id = checkout_order.id

    refund_barrier = Barrier(2)

    def apply_refund(index: int) -> str:
        with session_factory() as session:
            refund_barrier.wait(timeout=10)
            refund = PaymentDomainService(session).apply_refund(
                order_id=order_ids[index],
                idempotency_key=f"pg-refund-command-{run_id}-{index}",
                provider_refund_id=f"pg-refund-provider-{run_id}-{index}",
            )
            return refund.status

    checkout_barrier = Barrier(2)

    def claim_checkout() -> str:
        with session_factory() as session:
            checkout_barrier.wait(timeout=10)
            try:
                _order, claimed = PaymentDomainService(session).prepare_checkout(
                    order_id=checkout_order_id,
                )
                return "claimed" if claimed else "cached"
            except PaymentDomainError as exc:
                return f"rejected:{exc.status_code}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        checkout_results = sorted(pool.map(lambda _index: claim_checkout(), range(2)))

    # Exercise the checkout lease while the account is active. The following
    # concurrent refunds intentionally freeze the same account on shortfall;
    # after that point checkout must be rejected, so mixing the order would no
    # longer test lease exclusivity.
    with ThreadPoolExecutor(max_workers=2) as pool:
        refund_statuses = sorted(pool.map(apply_refund, range(2)))

    with session_factory() as session:
        wallet_balance = session.scalar(select(Wallet.balance_microusd).where(Wallet.user_id == user_id))
        user_status = session.scalar(select(User.status).where(User.id == user_id))
        refund_rows = session.scalars(select(PaymentRefund).where(
            PaymentRefund.user_id == user_id,
        )).all()
        order_statuses = session.scalars(select(PaymentOrder.status).where(
            PaymentOrder.id.in_(order_ids),
        )).all()
        unbalanced = session.scalar(
            select(func.count())
            .select_from(
                select(LedgerEntry.transaction_id)
                .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
                .where(LedgerTransaction.user_id == user_id)
                .group_by(LedgerEntry.transaction_id)
                .having(func.sum(LedgerEntry.amount_microusd) != 0)
                .subquery()
            )
        )
        customer_ledger = session.scalar(select(func.sum(LedgerEntry.amount_microusd)).where(
            LedgerEntry.user_id == user_id,
            LedgerEntry.account == CUSTOMER_AVAILABLE,
        ))
        checkout_status = session.scalar(select(PaymentOrder.status).where(
            PaymentOrder.id == checkout_order_id,
        ))

    expected = {
        "refund_statuses": ["refunded", "risk"],
        "checkout_results": ["claimed", "rejected:409"],
        "wallet_balance": 0,
        "customer_ledger": 0,
        "user_status": "frozen",
        "refund_count": 2,
        "order_statuses": ["refunded", "refunded"],
        "checkout_status": "checkout_requesting",
        "unbalanced_transactions": 0,
    }
    actual = {
        "refund_statuses": refund_statuses,
        "checkout_results": checkout_results,
        "wallet_balance": wallet_balance,
        "customer_ledger": int(customer_ledger or 0),
        "user_status": user_status,
        "refund_count": len(refund_rows),
        "order_statuses": sorted(order_statuses),
        "checkout_status": checkout_status,
        "unbalanced_transactions": unbalanced,
    }
    if actual != expected:
        raise RuntimeError("PostgreSQL 并发不变量失败: " + json.dumps(actual, sort_keys=True))
    print(json.dumps({"ok": True, **actual}, sort_keys=True))
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
