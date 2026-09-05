"""Managed-cost concurrency proof. Writes only to an acknowledged disposable CI DB."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import ApiKey, LedgerEntry, ModelPrice, ModelRequest, PlatformDailyBudget, User, Wallet
from app.security import utcnow
from app.services.gateway_billing import BillingError, reserve_model_request, release_model_request
from app.services.ledger import CUSTOMER_AVAILABLE, PLATFORM_CLEARING, post_transaction


def main():
    url = make_url(os.environ.get("KUNLUN_RUNTIME_DATABASE_URL", "sqlite://"))
    if (os.environ.get("KUNLUN_CI_ISOLATED_DATABASE") != "kunlun-ci-disposable"
        or url.host != "127.0.0.1" or url.database != "kunlun_ci" or url.username != "kunlun_runtime"):
        raise RuntimeError("Requires acknowledged isolated local kunlun_ci runtime database")
    engine = create_engine(url, pool_size=8, max_overflow=0)
    run = uuid4().hex
    model = "managed-ci-" + run
    principals = []
    with Session(engine) as db:
        db.add(ModelPrice(id=str(uuid4()), model=model, version=1, input_microusd_per_million=1_000_000,
                          output_microusd_per_million=1_000_000, active=True))
        for i in range(3):
            user, key = str(uuid4()), uuid4().hex
            principals.append((user, key))
            db.add(User(id=user, email=f"managed-{run}-{i}@example.invalid", password_hash="not-a-login",
                        status="active", email_verified_at=utcnow()))
            db.flush()
            db.add(ApiKey(id=key, user_id=user, name="CI only", secret_digest="0" * 64, last_four="test"))
            db.add(Wallet(user_id=user, balance_microusd=100000))
            db.flush()
            post_transaction(db, user_id=user, kind="managed_ci_seed", reference=run,
                             idempotency_key=f"managed-ci:{user}",
                             entries=[(CUSTOMER_AVAILABLE,100000),(PLATFORM_CLEARING,-100000)])
        db.commit()

    def reserve(i, duplicate=False):
        user, key = principals[0 if duplicate else i % len(principals)]
        with Session(engine) as db:
            try:
                return reserve_model_request(db, user_id=user, api_key_id=key, model=model,
                    billable_payload={"messages": [{"role":"user","content":"CI synthetic"}]},
                    max_output_tokens=32, idempotency_key=f"{run}:same" if duplicate else f"{run}:{i}",
                    managed_cost_prices=(500000,500000), platform_daily_limit=6000)
            except BillingError:
                return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        duplicates = [r for r in pool.map(lambda i: reserve(i, True), range(16)) if r]
    assert len(duplicates) == 1, "duplicate logical call acquired multiple holds"
    with Session(engine) as db:
        release_model_request(db, duplicates[0].request_id, "ci_definitely_not_sent")

    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = [r for r in pool.map(reserve, range(24)) if r]
    assert 0 < len(admitted) < 24
    with Session(engine) as db:
        requests = db.scalars(select(ModelRequest).where(ModelRequest.requested_model == model, ModelRequest.status == "reserved")).all()
        day = db.get(PlatformDailyBudget, utcnow().date().isoformat())
        assert day.spent_microusd + day.reserved_microusd <= day.limit_microusd == 6000
        assert day.reserved_microusd == sum(r.platform_reserved_microusd for r in requests)
        assert len(requests) == len(admitted)
        for user, _ in principals:
            wallet = db.get(Wallet, user)
            assert wallet.balance_microusd >= 0
            assert wallet.balance_microusd + wallet.reserved_microusd == 100000
            assert wallet.reserved_microusd == sum(r.reserved_microusd for r in requests if r.user_id == user)
        assert not db.execute(select(LedgerEntry.transaction_id).group_by(LedgerEntry.transaction_id)
                             .having(func.sum(LedgerEntry.amount_microusd) != 0)).first()
    for request in admitted:
        with Session(engine) as db:
            release_model_request(db, request.request_id, "ci_definitely_not_sent")
            release_model_request(db, request.request_id, "ci_duplicate_release")
    with Session(engine) as db:
        assert db.get(PlatformDailyBudget, utcnow().date().isoformat()).reserved_microusd == 0
    # Admission and finalization interleave across workers and tenants. Both
    # must use the same wallet/global lock order, not only avoid overdrawing.
    def mixed(i):
        result = reserve(100 + i)
        if result:
            with Session(engine) as db:
                release_model_request(db, result.request_id, "ci_mixed_release")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(mixed, range(24)))
    with Session(engine) as db:
        assert db.get(PlatformDailyBudget, utcnow().date().isoformat()).reserved_microusd == 0
    engine.dispose()
    print(f"Managed PostgreSQL concurrency passed: 3 tenants, 16 duplicate attempts -> 1 hold; 24 competing requests -> {len(admitted)} admitted; balanced ledgers and no negative wallets.")


if __name__ == "__main__":
    main()
