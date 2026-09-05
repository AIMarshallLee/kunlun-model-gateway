"""Synthetic supply/payment admission checks, never real model or payment I/O."""
from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app.models import ModelPrice, PaymentOrder
from app.services.credentials import SecretUnavailable
from tests.test_managed_gateway import managed
from tests.test_live_payment_routes import FakeLiveBridge


@pytest.fixture
def purchase(managed):
    client, auth, *_ = managed
    bridge = FakeLiveBridge()
    client.app.state.live_payment_bridge = bridge
    client.app.state.settings.payment_provider = "synthetic-provider"
    client.app.state.settings.topup_packages = {"synthetic": {
        "payment_amount_minor": 1999, "payment_currency": "CNY", "credit_amount_microusd": 1000000}}
    return client, auth, bridge


def create(purchase, key="supply-order"):
    client, auth, _ = purchase
    return client.post("/billing/checkout", headers={**auth, "Idempotency-Key": key}, json={"sku": "synthetic"})


def provision(client):
    client.app.state.platform_vault.write(provider="openai", secret="inert-platform", operation_id="provision",
                                          actor="test", reason="synthetic supply fixture")


@pytest.mark.parametrize("state", ["absent", "disabled", "cleanup", "unknown_channel", "unlisted", "vault_error"])
def test_unavailable_supply_cannot_create_order_or_payment(purchase, managed, monkeypatch, state):
    client, _, bridge = purchase
    vault = client.app.state.platform_vault
    if state != "absent":
        provision(client)
    if state == "disabled":
        vault.write(provider="openai", secret=None, operation_id="disable", actor="test", reason="synthetic shutdown")
    if state in {"cleanup", "unknown_channel"}:
        row = vault.list()[0]
        row["pending_cleanup"] = state == "cleanup"
        if state == "unknown_channel":
            row["provider"] = "not-allowed"
        monkeypatch.setattr(vault, "list", lambda: [row])
    if state == "unlisted":
        with client.app.state.SessionLocal() as db:
            db.scalar(select(ModelPrice)).active = False
            db.commit()
    if state == "vault_error":
        def unavailable():
            raise SecretUnavailable("inert-internal-secret")
        monkeypatch.setattr(vault, "list", unavailable)
    assert client.get("/billing/packages").json()["purchasing_enabled"] is False
    result = create(purchase)
    assert result.status_code == 503 and "inert-internal" not in result.text
    assert bridge.checkout_calls == [] and managed[-1] == []
    with client.app.state.SessionLocal() as db:
        assert db.scalar(select(func.count(PaymentOrder.id))) == 0


def test_active_supply_permits_one_checkout_and_outage_preserves_read_and_webhook(purchase, managed):
    client, auth, bridge = purchase
    provision(client)
    assert client.get("/billing/packages").json()["purchasing_enabled"] is True
    order = create(purchase)
    assert order.status_code == 201, order.text
    assert create(purchase).json()["id"] == order.json()["id"]
    assert len(bridge.checkout_calls) == 1
    client.app.state.platform_vault.write(provider="openai", secret=None, operation_id="disable", actor="test", reason="synthetic shutdown")
    assert create(purchase, "new-order-during-outage").status_code == 503
    lookup = client.post("/billing/checkout/lookup", headers={**auth, "Idempotency-Key": "supply-order"})
    assert lookup.status_code == 200 and lookup.json()["id"] == order.json()["id"]
    assert client.get(f'/billing/topups/{order.json()["id"]}', headers=auth).status_code == 200
    bridge.webhook = replace(bridge.webhook, order_id=order.json()["id"])
    assert client.post("/billing/live/webhook", content=b"signed-provider-event").status_code == 200
    assert client.get("/billing/balance", headers=auth).json()["balance"] == 1000000
    assert len(bridge.checkout_calls) == 1 and managed[-1] == []


def test_public_purchase_flag_follows_configured_supply_without_reading_keys(purchase, managed, monkeypatch):
    client, _, _ = purchase
    settings = client.app.state.settings
    # Only exercise the public projection under a production label; no app
    # startup, live adapters, database migration or external request occurs.
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "live_payments", True)
    assert client.get("/public/catalog").json()["purchasing_enabled"] is False


    provision(client)
    monkeypatch.setattr(client.app.state.platform_vault, "resolve", lambda *_: pytest.fail("public checks must not resolve raw keys"))
    result = client.get("/public/catalog")
    assert result.json()["purchasing_enabled"] is True
    assert "inert-platform" not in result.text and managed[-1] == []
    with client.app.state.SessionLocal() as db:
        db.scalar(select(ModelPrice)).active = False
        db.commit()
    assert client.get("/public/catalog").json()["purchasing_enabled"] is False


@pytest.mark.parametrize("secret", ["", " ", "\t\n"])
def test_blank_platform_key_cannot_enable_supply(managed, secret):
    client, _, _, ops, _ = managed
    result = client.put("/ops/channels/openai", headers=ops, json={
        "secret": secret, "operation_id": "blank-key", "reason": "synthetic empty key rejection"})
    assert result.status_code == 422
    assert client.app.state.platform_vault.list() == []
