"""Disposable loopback operator acceptance. All supply/payment/identity is fake."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from checkout_fixture import BrowserPaymentBridge
from test_managed_gateway import OPS, managed, ready_call
from app.models import ModelRequest, PaymentChargeback, PaymentChargebackReturn, User, Wallet
from app.services.payment_domain import PaymentDomainService
from app.services.ledger import CUSTOMER_AVAILABLE, PLATFORM_CLEARING, post_transaction
from app.services.ops_tokens import mint_operator_token
from app.ops_alerts import collect_alerts
from app.services.alert_notifications import queue_digest, dispatch_digest


if __name__ == "__main__":
    with TemporaryDirectory(prefix="kunlun-ops-ui-") as directory, pytest.MonkeyPatch.context() as patch:
        fixture = managed.__wrapped__(Path(directory), patch)
        context = next(fixture)
        client, auth, *_ = context
        app = client.app
        _, model_headers, payload = ready_call(context)
        if "--channels" in sys.argv:
            # Fixed synthetic catalog entry; no supplier connection or real key.
            app.state.settings.providers.append({**app.state.settings.providers[0], "name": "backup"})
        app.state.test_upstream = lambda request: httpx.Response(500, json={"error": "synthetic uncertain cost"})
        for operation in ("ops-release-case", "ops-settle-case"):
            model_headers["Idempotency-Key"] = operation
            assert client.post("/v1/chat/completions", headers=model_headers, json=payload).status_code == 502
        bridge = BrowserPaymentBridge()
        app.state.live_payment_bridge = bridge
        app.state.settings.payment_provider = "simulated_checkout"
        app.state.settings.public_base_url = "http://127.0.0.1:8797"
        app.state.settings.topup_packages = {"starter": {
            "payment_amount_minor": 1999, "payment_currency": "USD", "credit_amount_microusd": 1_000_000,
        }}
        order = client.post("/billing/checkout", headers={**auth, "Idempotency-Key": "ops-order"}, json={"sku": "starter"})
        assert order.status_code == 201, order.text
        assert client.post("/billing/live/webhook", content=b"signed-provider-event").status_code == 200
        chargebacks = {}
        returns = {}
        if "--chargebacks" in sys.argv:
            for case in ("recover", "write_off", "pending", "recovered"):
                with app.state.SessionLocal() as db:
                    owner = str(uuid4())
                    db.add(User(id=owner, email=f"{case}@example.invalid", password_hash="inert"))
                    db.flush(); db.add(Wallet(user_id=owner)); db.commit()
                    service = PaymentDomainService(db)
                    paid = service.create_order(user_id=owner, provider="fixture", payment_amount_minor=100,
                        payment_currency="USD", credit_amount_microusd=1000, quote_id="fixture-only",
                        quote_numerator=10, quote_denominator=1, idempotency_key=case)
                    service.apply_webhook(provider="fixture", event_id=f"paid-{case}", raw_digest="a" * 64,
                        order_id=paid.id, event_type="payment.succeeded", status="paid", payment_amount_minor=100,
                        payment_currency="USD", provider_transaction_id=f"txn-{case}")
                    if case in {"recover", "write_off"}:
                        db.get(Wallet, owner).balance_microusd -= 800
                        post_transaction(db, user_id=owner, kind="fixture_spend", reference=case,
                            idempotency_key=f"fixture-spend-{case}", entries=[(CUSTOMER_AVAILABLE, -800), (PLATFORM_CLEARING, 800)])
                        db.commit()
                    service.apply_webhook(provider="fixture", event_id=f"cb-{case}", raw_digest="b" * 64,
                        order_id=paid.id, event_type="payment.charged_back", status="charged_back", payment_amount_minor=50 if case == "pending" else 100,
                        payment_currency="USD", provider_transaction_id=f"txn-{case}", provider_dispute_id=f"dispute-{case}")
                    if case == "recover":
                        db.get(Wallet, owner).balance_microusd += 800
                        post_transaction(db, user_id=owner, kind="fixture_credit", reference=case,
                            idempotency_key="fixture-recovery-credit", entries=[(CUSTOMER_AVAILABLE, 800), (PLATFORM_CLEARING, -800)])
                        db.commit()
                    chargebacks[case] = db.scalar(select(PaymentChargeback.id).where(PaymentChargeback.order_id == paid.id))
        if "--returns" in sys.argv:
            from app.services.chargebacks import resolve_chargeback_risk
            for case in ("pending", "applied", "loss_reversed"):
                with app.state.SessionLocal() as db:
                    owner = str(uuid4())
                    db.add(User(id=owner, email=f"return-{case}@example.invalid", password_hash="inert"))
                    db.flush(); db.add(Wallet(user_id=owner)); db.commit()
                    service = PaymentDomainService(db)
                    paid = service.create_order(user_id=owner, provider="fixture", payment_amount_minor=100,
                        payment_currency="USD", credit_amount_microusd=1000, quote_id="fixture-only",
                        quote_numerator=10, quote_denominator=1, idempotency_key=f"return-{case}")
                    service.apply_webhook(provider="fixture", event_id=f"return-paid-{case}", raw_digest="a" * 64,
                        order_id=paid.id, event_type="payment.succeeded", status="paid", payment_amount_minor=100,
                        payment_currency="USD", provider_transaction_id=f"return-txn-{case}")
                    if case == "loss_reversed":
                        db.get(Wallet, owner).balance_microusd -= 800
                        post_transaction(db, user_id=owner, kind="fixture_spend", reference=case,
                            idempotency_key=f"return-spend-{case}", entries=[(CUSTOMER_AVAILABLE, -800), (PLATFORM_CLEARING, 800)])
                        db.commit()
                    if case != "pending":
                        service.apply_webhook(provider="fixture", event_id=f"return-cb-{case}", raw_digest="b" * 64,
                            order_id=paid.id, event_type="payment.charged_back", status="charged_back", payment_amount_minor=100,
                            payment_currency="USD", provider_transaction_id=f"return-txn-{case}", provider_dispute_id=f"return-dispute-{case}")
                    if case == "loss_reversed":
                        cb = db.scalar(select(PaymentChargeback).where(PaymentChargeback.order_id == paid.id))
                        resolve_chargeback_risk(db, cb.id, action="write_off", idempotency_key="fixture-write-off")
                        db.commit()
                    service.apply_webhook(provider="fixture", event_id=f"return-event-{case}", raw_digest="c" * 64,
                        order_id=paid.id, event_type="payment.chargeback_returned", status="chargeback_returned", payment_amount_minor=100,
                        payment_currency="USD", provider_transaction_id=f"return-txn-{case}",
                        provider_dispute_id=f"return-dispute-{case}", provider_return_id=f"cash-return-{case}")
                    record = db.scalar(select(PaymentChargebackReturn).where(PaymentChargebackReturn.order_id == paid.id))
                    returns[case] = {"id": record.id, "order_id": paid.id, "chargeback_id": record.chargeback_id}
        with app.state.SessionLocal() as db:
            requests = {row.idempotency_key: row.id for row in db.scalars(select(ModelRequest))}
            observation = collect_alerts(db, app.state.settings, app.state.platform_vault)
        notification = queue_digest(app.state.SessionLocal, observation, "synthetic-ops@example.invalid")
        class FakeSMTP:
            def send_operator_alert(self, *args): pass
        assert dispatch_digest(app.state.SessionLocal, notification, "synthetic-ops@example.invalid", FakeSMTP()) == "accepted"

        @app.get("/__fixture__/operator")
        def operator(profile: str = "read"):
            scopes = {"console:read", "alerts:read", "accounts:read", "payments:read", "reconciliation:read", "models:read", "channels:read", "metrics:read", "audit:read"}
            if profile == "write":
                scopes |= {"alerts:write", "accounts:write", "payments:write", "reconciliation:write", "payments:risk:write", "models:write"}
            if profile == "limited":
                scopes = {"console:read", "alerts:read"}
            if profile in {"channel_read", "channel_write"}:
                scopes = {"console:read", "channels:read"}
                if profile == "channel_write":
                    scopes.add("channels:write")
            return {"token": mint_operator_token(OPS, subject="synthetic-operator", scopes=scopes, ttl_seconds=300),
                    "requests": requests, "order_id": bridge.webhook.order_id, "chargebacks": chargebacks, "returns": returns}

        @app.get("/__fixture__/refund-calls")
        def refund_calls():
            return {"count": len(bridge.refund_calls)}

        @app.get("/__fixture__/channel-observation")
        def channel_observation():
            return {"model_calls": len(context[-1]), "operations": len(app.state.platform_vault._operations)}

        try:
            uvicorn.run(app, host="127.0.0.1", port=8797, access_log=False, log_level="warning")
        finally:
            fixture.close()
