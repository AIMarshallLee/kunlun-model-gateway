from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json

import pytest
from sqlalchemy import select

from app.models import ApiKey, LedgerEntry, LedgerTransaction, User, Wallet
from app.models import Budget
from app.services import gateway_billing
from app.services.gateway_billing import BillingError, reserve_model_request
from app.services.payments import process_test_webhook


def test_concurrent_reservations_never_overdraw_wallet(client, auth_headers, api_key, webhook_secret):
    order = client.post("/billing/topups", headers=auth_headers, json={"amount": 100_000}).json()
    body = json.dumps({
        "id": "evt_concurrent_wallet",
        "order_id": order["id"],
        "type": "topup.succeeded",
        "amount": 100_000,
    }).encode()
    signature = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    assert client.post("/billing/webhook", content=body, headers={
        "Content-Type": "application/json", "X-Webhook-Signature": signature,
    }).status_code == 200
    with client.app.state.SessionLocal() as session:
        user_id = session.scalar(select(User.id))
        key_id = session.scalar(select(ApiKey.id))

    def reserve(index):
        with client.app.state.SessionLocal() as session:
            try:
                return reserve_model_request(
                    session,
                    user_id=user_id,
                    api_key_id=key_id,
                    model="test-model",
                    billable_payload={
                        "model": "test-model",
                        "messages": [{"role": "user", "content": "concurrent"}],
                    },
                    max_output_tokens=256,
                    idempotency_key=f"concurrent-{index}",
                )
            except BillingError:
                return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(reserve, range(24)))
    successful = [result for result in results if result is not None]
    assert successful
    with client.app.state.SessionLocal() as session:
        wallet = session.get(Wallet, user_id)
        assert wallet.balance_microusd >= 0
        assert wallet.balance_microusd + wallet.reserved_microusd == 100_000
        assert wallet.reserved_microusd == sum(result.amount for result in successful)
        transactions = session.scalars(select(LedgerTransaction)).all()
        for transaction in transactions:
            entries = session.scalars(select(LedgerEntry).where(
                LedgerEntry.transaction_id == transaction.id,
            )).all()
            assert sum(entry.amount_microusd for entry in entries) == 0


def test_concurrent_duplicate_webhooks_credit_only_once(client, auth_headers, webhook_secret):
    order = client.post("/billing/topups", headers=auth_headers, json={"amount": 7777}).json()
    body = json.dumps({
        "id": "evt_concurrent_replay",
        "order_id": order["id"],
        "type": "topup.succeeded",
        "amount": 7777,
    }).encode()
    signature = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()

    def deliver(_index):
        with client.app.state.SessionLocal() as session:
            return process_test_webhook(
                session,
                raw_body=body,
                signature=signature,
                secret=webhook_secret,
            )

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(deliver, range(24)))
    assert results.count(False) == 1
    assert results.count(True) == 23
    assert client.get("/billing/balance", headers=auth_headers).json()["balance"] == 7777


def test_reservation_rechecks_budget_status_atomically(
    client, auth_headers, api_key, webhook_secret, monkeypatch,
):
    order = client.post("/billing/topups", headers=auth_headers, json={"amount": 10_000}).json()
    body = json.dumps({
        "id": "evt_budget_status_race",
        "order_id": order["id"],
        "type": "topup.succeeded",
        "amount": 10_000,
    }).encode()
    signature = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    assert client.post("/billing/webhook", content=body, headers={
        "Content-Type": "application/json", "X-Webhook-Signature": signature,
    }).status_code == 200
    assert client.post("/budgets", headers=auth_headers, json={"amount": 10_000}).status_code == 201
    with client.app.state.SessionLocal() as session:
        user_id = session.scalar(select(User.id))
        key_id = session.scalar(select(ApiKey.id))

    original = gateway_billing.active_budget

    def supersede_after_selection(session, selected_user_id):
        budget = original(session, selected_user_id)
        assert budget is not None
        budget.status = "superseded"
        session.flush()
        return budget

    monkeypatch.setattr(gateway_billing, "active_budget", supersede_after_selection)
    with client.app.state.SessionLocal() as session, pytest.raises(BillingError, match="已更新"):
        reserve_model_request(
            session,
            user_id=user_id,
            api_key_id=key_id,
            model="test-model",
            billable_payload={
                "model": "test-model",
                "messages": [{"role": "user", "content": "race"}],
            },
            max_output_tokens=256,
            idempotency_key="budget-status-race",
        )
    with client.app.state.SessionLocal() as session:
        wallet = session.get(Wallet, user_id)
        budget = session.scalar(select(Budget).where(Budget.user_id == user_id))
        assert wallet.balance_microusd == 10_000
        assert wallet.reserved_microusd == 0
        assert budget is not None and budget.reserved_microusd == 0
