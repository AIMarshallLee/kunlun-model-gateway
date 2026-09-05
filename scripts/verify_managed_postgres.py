"""Managed-cost concurrency proof. Writes only to an acknowledged disposable CI DB."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
from threading import Barrier
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import ApiKey, LedgerEntry, ModelPrice, ModelRequest, OperatorAction, PlatformDailyBudget, User, Wallet
from app.security import utcnow
from app.services.gateway_billing import BillingError, key_usage, reserve_model_request, release_model_request, settle_model_request
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
    # A key cap is narrower than both wallet and platform budget. With no key
    # policy the platform admits two holds; the 4500 cap must admit only one.
    user, key = principals[0]
    with Session(engine) as db:
        db.get(ApiKey, key).spend_limit_microusd = 4500
        db.commit()
    with ThreadPoolExecutor(max_workers=8) as pool:
        key_admitted = [r for r in pool.map(lambda i: reserve(3000 + i * 3), range(16)) if r]
    assert len(key_admitted) == 1, "concurrent requests bypassed the independent key cap"
    with Session(engine) as db:
        usage = key_usage(db, user, key)[key]
        assert usage["spent_microusd"] == 0
        assert usage["reserved_microusd"] == key_admitted[0].amount <= 4500
        release_model_request(db, key_admitted[0].request_id, "ci_key_release")
        assert key_usage(db, user, key)[key]["reserved_microusd"] == 0
    # Exercise the actual key-freeze handler against concurrent admission,
    # including its audit insert under the restricted runtime database role.
    # HTTP token/ingress checks are covered separately by API/browser tests.
    from types import SimpleNamespace
    from sqlalchemy.orm import sessionmaker
    from app.ops_console import KeyStatusRequest, key_status
    from app.services.ops_tokens import OperatorClaims
    request_context = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), app=SimpleNamespace(state=SimpleNamespace(
        SessionLocal=sessionmaker(engine), settings=SimpleNamespace(session_pepper="ci-inert-pepper"))))
    claims = OperatorClaims("ci-operator", frozenset({"accounts:write"}), 0, 1, "ci-inert-token")
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(reserve, 6000 + i * 3) for i in range(16)]
        frozen = pool.submit(key_status, key, KeyStatusRequest(action="freeze", expected_status="active",
            reason="isolated concurrent key freeze acceptance"), request_context, claims)
        raced = [result for future in pending if (result := future.result()) is not None]
        assert frozen.result()["status"] == "frozen"
    assert reserve(9000) is None, "a new admission crossed the committed key freeze"
    from app.ops_console import account
    key_page = account(user, request_context, key_limit=1, key_offset=0, key_id=key)
    assert key_page["keys_pagination"] == {"limit": 1, "offset": 0, "total": 1}
    assert key_page["keys"][0]["id"] == key and key_page["keys"][0]["status"] == "frozen"
    for reservation in raced:
        with Session(engine) as db:
            release_model_request(db, reservation.request_id, "ci_frozen_key_existing_hold")
    # Actual price commands under the restricted runtime role: competing
    # operators cannot both publish over v1, and v1 holds settle at v1 prices.
    from fastapi import HTTPException
    from app.model_catalog import PriceChange, change_price
    price_claims = OperatorClaims("ci-price-operator", frozenset({"models:write"}), 0, 1, "ci-inert-price-token")
    request_context.app.state.settings = SimpleNamespace(
        session_pepper="ci-inert-pepper", gateway_mode="managed_gateway", max_output_tokens=4096,
        models={model: {}}, providers=[{"models": [model], "pricing": {model: {
            "input_microusd_per_million": 500000, "output_microusd_per_million": 500000}}}])
    with Session(engine) as db:
        anchor_id = db.scalar(select(ModelPrice.id).where(ModelPrice.model == model))
    old_hold = reserve(12001)  # non-frozen principal 1
    assert old_hold is not None
    start = Barrier(2)
    def publish(i):
        start.wait(timeout=10)
        try:
            change_price(anchor_id, PriceChange(action="publish", expected_version=1,
                operation_id=f"{run}:price:{i}", reason="isolated concurrent price version acceptance",
                input_microusd_per_million=2000000, output_microusd_per_million=3000000,
                max_output_tokens=4096), request_context, price_claims)
            return 201
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(publish, range(2))) == [201, 409]
    with Session(engine) as db:
        settle_model_request(db, request_id=old_hold.request_id, response={"usage": {"prompt_tokens": 4, "completion_tokens": 2}},
                            provider="ci-synthetic", fallback_count=0, upstream_cost_override=3)
        old_request = db.get(ModelRequest, old_hold.request_id)
        assert old_request.price_version == 1 and old_request.charged_microusd == 6
        assert db.scalar(select(func.count()).select_from(OperatorAction).where(
            OperatorAction.target_id == anchor_id, OperatorAction.action == "model_publish")) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(reserve, 15001 + i * 3) for i in range(16)]
        unlisted = pool.submit(change_price, anchor_id, PriceChange(action="unpublish", expected_version=2,
            operation_id=f"{run}:unlist", reason="isolated unlisting versus new admissions acceptance"), request_context, price_claims)
        price_raced = [result for future in pending if (result := future.result()) is not None]
        assert unlisted.result()["model"]["active"] is False
    assert reserve(18001) is None, "new admission crossed committed unlisting"
    with Session(engine) as db:
        for reservation in price_raced:
            assert db.get(ModelRequest, reservation.request_id).price_version == 2
            release_model_request(db, reservation.request_id, "ci_unlisted_existing_hold")
        versions = db.scalars(select(ModelPrice).where(ModelPrice.model == model).order_by(ModelPrice.version)).all()
        assert [row.version for row in versions] == [1, 2, 3]
        assert not any(row.active for row in versions)
        assert versions[0].input_microusd_per_million == 1000000
    # Alert receipts use the existing append-only audit, with no balance or
    # incident writes. Duplicate concurrent confirmations insert only once.
    from app.ops_alerts import AlertReceipt, acknowledge, collect_alerts
    alert_settings = request_context.app.state.settings
    alert_settings.model_reservation_lease_seconds = 300
    alert_settings.platform_daily_budget_microusd = 3
    request_context.app.state.platform_vault = SimpleNamespace(list=lambda: [])
    alert_claims = OperatorClaims("ci-alert-operator", frozenset({"alerts:write"}), 0, 1, "ci-inert-alert-token")
    with Session(engine) as db:
        observation = next(row for row in collect_alerts(db, alert_settings, request_context.app.state.platform_vault)["items"]
                           if row["id"] == "platform_budget")
        wallets_before = [(db.get(Wallet, uid).balance_microusd, db.get(Wallet, uid).reserved_microusd) for uid, _ in principals]
    receipt = AlertReceipt(expected_revision=observation["revision"], operation_id=f"{run}:alert",
                           reason="isolated concurrent alert receipt acceptance")
    ready = Barrier(2)
    def confirm_alert(_):
        ready.wait(timeout=10)
        try:
            acknowledge("platform_budget", receipt, request_context, alert_claims)
            return 201
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(confirm_alert, range(2))) == [201, 409]
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(OperatorAction).where(OperatorAction.operation_id == receipt.operation_id)) == 1
        assert wallets_before == [(db.get(Wallet, uid).balance_microusd, db.get(Wallet, uid).reserved_microusd) for uid, _ in principals]
        assert next(row for row in collect_alerts(db, alert_settings, request_context.app.state.platform_vault)["items"]
                    if row["id"] == "platform_budget")["status"] == "attention"
    # A real runtime role and concurrent workers share one durable mail claim.
    # The injected transport does not contact any mail server.
    from app.services.alert_notifications import queue_digest, dispatch_digest
    from app.models import OutboxEvent
    factory = sessionmaker(engine)
    with factory() as db:
        digest_observation = collect_alerts(db, alert_settings, request_context.app.state.platform_vault)
    recipient = f"ci-{run}@example.invalid"
    digest_now = utcnow()
    with ThreadPoolExecutor(max_workers=8) as pool:
        digest_ids = list(pool.map(lambda _: queue_digest(factory, digest_observation, recipient, now=digest_now), range(16)))
    assert len(set(digest_ids)) == 1
    mail_attempts = []
    class FakeSMTP:
        def send_operator_alert(self, address, notification_id, summary):
            with factory() as db:
                assert db.get(OutboxEvent, notification_id).status == "sending"
            mail_attempts.append(notification_id)
    with ThreadPoolExecutor(max_workers=8) as pool:
        delivery_states = list(pool.map(lambda _: dispatch_digest(factory, digest_ids[0], recipient, FakeSMTP()), range(16)))
    assert len(mail_attempts) == 1 and delivery_states.count("accepted") == 1
    engine.dispose()
    print("Notification PostgreSQL race passed: 16 queues -> one record; 16 workers -> one simulated SMTP attempt outside the claim transaction.")
    print("Alert PostgreSQL race passed: duplicate receipt -> one immutable audit; wallets unchanged; condition remains active.")
    print("Price PostgreSQL races passed: competing v1 commands -> 201/409; historical settlement unchanged; no new admission after unlisting.")
    print("Key freeze PostgreSQL race passed: no new admission after freeze; existing holds can finalize.")
    print("Key PostgreSQL concurrency passed: 16 distinct requests on one capped key -> 1 hold; release restored capacity.")
    print(f"Managed PostgreSQL concurrency passed: 3 tenants, 16 duplicate attempts -> 1 hold; 24 competing requests -> {len(admitted)} admitted; balanced ledgers and no negative wallets.")


if __name__ == "__main__":
    main()
