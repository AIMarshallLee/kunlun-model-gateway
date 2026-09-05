"""Chargeback races against an explicitly acknowledged disposable local DB."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
from threading import Barrier, Event
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import ApiKey, LedgerEntry, LedgerTransaction, ModelPrice, OperatorAction, PaymentChargeback, PaymentChargebackReturn, User, Wallet
from app.security import utcnow
from app.services.chargebacks import resolve_chargeback_risk
from app.services.gateway_billing import BillingError, release_model_request, reserve_model_request
from app.services.payment_domain import PaymentDomainError, PaymentDomainService
from app.services.ledger import CUSTOMER_AVAILABLE, PLATFORM_REVENUE, post_transaction


def main():
    url = make_url(os.environ.get("KUNLUN_RUNTIME_DATABASE_URL", "sqlite://"))
    if (os.environ.get("KUNLUN_CI_ISOLATED_DATABASE") != "kunlun-ci-disposable"
            or url.host != "127.0.0.1" or url.database != "kunlun_ci" or url.username != "kunlun_runtime"):
        raise RuntimeError("Requires acknowledged disposable local kunlun_ci runtime database")
    engine = create_engine(url, pool_size=8, max_overflow=0, connect_args={"options": "-c statement_timeout=10000"})
    run = uuid4().hex
    model = "chargeback-ci-" + run
    with Session(engine) as db:
        db.add(ModelPrice(id=str(uuid4()), model=model, version=1, active=True,
            input_microusd_per_million=1_000_000, output_microusd_per_million=1_000_000))
        db.commit()

    def funded():
        owner, key = str(uuid4()), uuid4().hex
        with Session(engine) as db:
            db.add(User(id=owner, email=f"{owner}@example.invalid", password_hash="inert", email_verified_at=utcnow()))
            db.flush()
            db.add(Wallet(user_id=owner))
            db.add(ApiKey(id=key, user_id=owner, name="CI only", secret_digest=uuid4().hex * 2, last_four="test"))
            db.commit()
            service = PaymentDomainService(db)
            order = service.create_order(user_id=owner, provider="ci-approved", payment_amount_minor=100,
                payment_currency="USD", credit_amount_microusd=100000, quote_id="ci-v1",
                quote_numerator=1000, quote_denominator=1, idempotency_key="ci-purchase")
            txn = "ci-" + order.id
            service.apply_webhook(provider=order.provider, event_id="paid-" + order.id, raw_digest="a" * 64,
                order_id=order.id, event_type="payment.succeeded", status="paid", payment_amount_minor=100,
                payment_currency="USD", provider_transaction_id=txn)
            return owner, key, order.id, txn

    def debit(data, suffix):
        _, _, order, txn = data
        with Session(engine) as db:
            return PaymentDomainService(db).apply_webhook(provider="ci-approved", event_id=f"debit-{order}-{suffix}",
                raw_digest="b" * 64, order_id=order, event_type="payment.charged_back", status="charged_back",
                payment_amount_minor=100, payment_currency="USD", provider_transaction_id=txn,
                provider_dispute_id="dispute-" + order)

    data = funded()
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda i: debit(data, str(i)), range(16)))
    assert outcomes.count(False) == 1 and outcomes.count(True) == 15
    with Session(engine) as db:
        row = db.scalar(select(PaymentChargeback).where(PaymentChargeback.order_id == data[2]))
        assert row.recovered_microusd == 100000 and row.status == "recovered"
        assert db.get(Wallet, data[0]).balance_microusd == 0
        assert db.get(ApiKey, data[1]).status == "revoked"
        assert db.scalar(select(func.count(LedgerTransaction.id)).where(LedgerTransaction.reference == row.id)) == 1
    print("PostgreSQL chargeback duplicate events: 16 events -> one case and one reversal.")

    def funds_return(data, suffix):
        _, _, order, txn = data
        with Session(engine) as db:
            return PaymentDomainService(db).apply_webhook(provider="ci-approved", event_id=f"return-{order}-{suffix}",
                raw_digest="c" * 64, order_id=order, event_type="payment.chargeback_returned", status="chargeback_returned",
                payment_amount_minor=100, payment_currency="USD", provider_transaction_id=txn,
                provider_dispute_id="dispute-" + order, provider_return_id="return-" + order)

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda i: funds_return(data, str(i)), range(16)))
    assert outcomes.count(False) == 1 and outcomes.count(True) == 15
    with Session(engine) as db:
        assert db.get(Wallet, data[0]).balance_microusd == 100000
        assert db.get(User, data[0]).status == "frozen"
        assert db.scalar(select(func.count(PaymentChargebackReturn.id)).where(PaymentChargebackReturn.order_id == data[2])) == 1
        assert db.scalar(select(func.count(LedgerTransaction.id)).where(
            LedgerTransaction.user_id == data[0], LedgerTransaction.kind == "chargeback_return")) == 1
    print("PostgreSQL funds returns: 16 events -> one restoration; account remains frozen.")

    for index in range(4):
        data = funded()
        barrier = Barrier(2)
        returned_first = Event()
        def paired_return():
            barrier.wait()
            try:
                return funds_return(data, "paired")
            finally:
                returned_first.set()
        def paired_debit():
            barrier.wait()
            if index == 0:
                assert returned_first.wait(10)
            return debit(data, "paired")
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(paired_return), pool.submit(paired_debit)
            assert first.result() is False and second.result() is False
        with Session(engine) as db:
            row = db.scalar(select(PaymentChargebackReturn).where(PaymentChargebackReturn.order_id == data[2]))
            assert row.status == "applied" and row.chargeback_id is not None
            assert db.get(Wallet, data[0]).balance_microusd == 100000
            assert db.get(User, data[0]).status == "frozen"
    print("PostgreSQL return/debit race: either event order retains both journals with no extra credit.")

    for index in range(6):
        data = funded()
        with Session(engine) as db:
            wallet = db.get(Wallet, data[0])
            wallet.balance_microusd -= 80000
            post_transaction(db, user_id=data[0], kind="ci_synthetic_spend", reference=data[2],
                idempotency_key="spend-" + data[2],
                entries=[(CUSTOMER_AVAILABLE, -80000), (PLATFORM_REVENUE, 80000)])
            db.commit()
        debit(data, "before-return-race")
        barrier = Barrier(2)
        disposed = Event()
        def disposition():
            barrier.wait()
            try:
                with Session(engine) as db:
                    case_id = db.scalar(select(PaymentChargeback.id).where(PaymentChargeback.order_id == data[2]))
                    try:
                        row, *_ = resolve_chargeback_risk(db, case_id, action="write_off", idempotency_key="ci-return-race")
                        db.add(OperatorAction(id=str(uuid4()), target_type="payment_chargeback", target_id=row.id,
                            action="chargeback_risk_write_off", actor="ci-only", scopes="payments:risk:write", token_id="ci-inert",
                            operation_id=str(uuid4()), reason="isolated CI return disposition race", before_status="risk", after_status="resolved"))
                        db.commit()
                    except PaymentDomainError as exc:
                        assert exc.status_code == 409
                        db.rollback()
            finally:
                disposed.set()
        def returning():
            barrier.wait()
            if index == 0:
                assert disposed.wait(10)
            return funds_return(data, "race")
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(disposition), pool.submit(returning)
            first.result()
            assert second.result() is False
        with Session(engine) as db:
            wallet = db.get(Wallet, data[0])
            assert wallet.balance_microusd == 20000 and wallet.reserved_microusd == 0
            row = db.scalar(select(PaymentChargebackReturn).where(PaymentChargebackReturn.order_id == data[2]))
            assert row.status == "applied" and row.restored_microusd == 20000
            assert row.canceled_risk_microusd + row.reversed_loss_microusd == 80000
            if index == 0:
                assert row.reversed_loss_microusd == 80000
            net = dict(db.execute(select(LedgerEntry.account, func.sum(LedgerEntry.amount_microusd))
                .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
                .where(LedgerTransaction.reference == row.chargeback_id).group_by(LedgerEntry.account)).all())
            assert all(value == 0 for value in net.values())
    print("PostgreSQL funds-return/write-off race: actual recovered credits only; risk/loss journals reversed once.")

    for index in range(6):
        data = funded()
        barrier = Barrier(2)
        admission_finished = Event()
        def admission():
            barrier.wait()
            with Session(engine) as db:
                try:
                    return reserve_model_request(db, user_id=data[0], api_key_id=data[1], model=model,
                        billable_payload={"messages": [{"role": "user", "content": "synthetic"}]},
                        max_output_tokens=32, idempotency_key="ci-race",
                        managed_cost_prices=(500000, 500000), platform_daily_limit=6000)
                except BillingError:
                    return None
                finally:
                    admission_finished.set()
        def chargeback():
            barrier.wait()
            if index == 0:
                assert admission_finished.wait(10)
            return debit(data, "race")
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(admission), pool.submit(chargeback)
            reservation, _ = first.result(), second.result()
        if index == 0:
            assert reservation is not None, "Deterministic hold-before-chargeback case must reserve"
        with Session(engine) as db:
            wallet = db.get(Wallet, data[0])
            row = db.scalar(select(PaymentChargeback).where(PaymentChargeback.order_id == data[2]))
            assert db.get(User, data[0]).status == "frozen"
            assert wallet.balance_microusd == 0
            assert wallet.reserved_microusd == row.outstanding_microusd
            if reservation:
                assert row.status == "risk" and row.outstanding_microusd > 0
                release_model_request(db, reservation.request_id, "ci-proven-not-sent")
                row, duplicate, recovered, written_off = resolve_chargeback_risk(db, row.id,
                    action="recover_available", idempotency_key="ci-recover")
                db.add(OperatorAction(id=str(uuid4()), target_type="payment_chargeback", target_id=row.id,
                    action="chargeback_risk_recover", actor="ci-only", scopes="payments:risk:write", token_id="ci-inert",
                    operation_id=str(uuid4()), reason="isolated CI hold-release recovery", before_status="risk", after_status="resolved"))
                db.commit()
                assert not duplicate and written_off == 0 and recovered > 0
                assert db.get(Wallet, data[0]).balance_microusd == 0
            else:
                assert row.status == "recovered" and row.outstanding_microusd == 0
            assert db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_microusd), 0))
                .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
                .where(LedgerTransaction.reference == row.id)) == 0
    print("PostgreSQL chargeback/admission race: holds retained; released holds recovered by audited command.")
    engine.dispose()


if __name__ == "__main__":
    main()
