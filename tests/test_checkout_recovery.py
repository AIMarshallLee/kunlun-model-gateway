from dataclasses import replace

from sqlalchemy import select

from app.models import PaymentOrder, Wallet
from test_live_payment_routes import FakeLiveBridge, FailingLiveBridge, _client


def test_checkout_lookup_is_read_only_scoped_and_survives_payment_disable(tmp_path):
    bridge = FakeLiveBridge()
    app, client, auth = _client(tmp_path, bridge)
    try:
        headers = {**auth, "Idempotency-Key": "recover-original"}
        assert client.get("/readyz").json()["environment"] != "production"
        order = client.post("/billing/checkout", headers=headers, json={"sku": "starter"}).json()
        for _ in range(3):
            result = client.post("/billing/checkout/lookup", headers=headers)
            assert result.status_code == 200, result.text
            assert result.json()["id"] == order["id"]
            assert result.json()["next_action"] == "resume_checkout"
            assert result.json()["checkout_url"] == order["checkout_url"]
            assert "provider_transaction_id" not in result.text
        assert len(bridge.checkout_calls) == 1
        identity = {"email": "another@example.com", "password": "another long test password"}
        assert client.post("/auth/register", json=identity).status_code == 201
        other = client.post("/auth/login", json=identity).json()["access_token"]
        assert client.post("/billing/checkout/lookup", headers={
            "Authorization": f"Bearer {other}", "Idempotency-Key": "recover-original",
        }).status_code == 404
        assert client.get(f'/billing/topups/{order["id"]}', headers={"Authorization": f"Bearer {other}"}).status_code == 404
        assert client.get(f'/billing/topups/{order["id"]}').status_code == 401
        app.state.live_payment_bridge = None
        app.state.settings.topup_packages = {}
        # A changed SKU/catalog must not prevent reading the frozen order.
        result = client.get(f'/billing/topups/{order["id"]}', headers=auth)
        assert result.status_code == 200
        assert result.json()["payment_amount_minor"] == 1999
        with app.state.SessionLocal() as db:
            assert db.scalar(select(Wallet).where(Wallet.user_id == db.scalar(select(PaymentOrder)).user_id)).balance_microusd == 0
    finally:
        client.__exit__(None, None, None)


def test_checkout_lookup_does_not_recreate_uncertain_or_paid_payment(tmp_path):
    app, client, auth = _client(tmp_path, FailingLiveBridge())
    try:
        headers = {**auth, "Idempotency-Key": "uncertain-purchase"}
        assert client.post("/billing/checkout", headers=headers, json={"sku": "starter"}).status_code == 503
        result = client.post("/billing/checkout/lookup", headers=headers).json()
        assert result["status"] == "pending_reconciliation"
        assert result["checkout_url"] is None
        assert result["next_action"] == "contact_support"
        bridge = FakeLiveBridge()
        app.state.live_payment_bridge = bridge
        bridge.webhook = replace(bridge.webhook, order_id=result["id"])
        assert client.post("/billing/live/webhook", content=b"signed-provider-event").status_code == 200
        paid = client.post("/billing/checkout/lookup", headers=headers).json()
        assert paid["status"] == "paid" and paid["next_action"] == "check_balance"
        assert paid["checkout_url"] is None
        assert not bridge.checkout_calls
    finally:
        client.__exit__(None, None, None)


def test_checkout_lookup_requires_valid_header_and_suppresses_unsafe_urls(tmp_path):
    app, client, auth = _client(tmp_path, FakeLiveBridge())
    try:
        assert client.post("/billing/checkout/lookup", headers=auth).status_code == 422
        assert client.post("/billing/checkout/lookup", headers={**auth, "Idempotency-Key": "bad key"}).status_code == 422
        assert client.post("/billing/checkout/lookup", headers={**auth, "Idempotency-Key": "missing"}).status_code == 404
        order = client.post("/billing/checkout", headers={**auth, "Idempotency-Key": "unsafe-url"}, json={"sku": "starter"}).json()
        for url in ("javascript:alert(1)", "https://user:password@pay.example.test/session"):
            with app.state.SessionLocal() as db:
                db.get(PaymentOrder, order["id"]).checkout_url = url
                db.commit()
            result = client.get(f'/billing/topups/{order["id"]}', headers=auth).json()
            assert result["checkout_url"] is None
            assert result["next_action"] == "contact_support"
    finally:
        client.__exit__(None, None, None)
