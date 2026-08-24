"""Gateway contract fixtures.  Production code is intentionally not supplied here."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock

from app import create_app


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "gateway.sqlite3"
    fake_provider = AsyncMock(return_value={
        "id": "test-completion",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    })
    app = create_app(
        database_url=f"sqlite:///{db}",
        payment_webhook_secret="test-webhook-secret",
        enable_test_payments=True,
        public_signup=True,
        rate_limit_per_minute=10,
        operator_token="test-operator-token-change-me-1234567890",
        provider_clients=[fake_provider],
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def webhook_secret():
    """Simulates a provider-held secret; it must never come from an API response."""
    return "test-webhook-secret"


@pytest.fixture
def account(client):
    response = client.post(
        "/auth/register",
        json={"email": "owner@example.com", "password": "correct horse battery staple"},
    )
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def auth_headers(client, account):
    response = client.post(
        "/auth/login",
        json={"email": "owner@example.com", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def api_key(client, auth_headers):
    response = client.post("/v1/keys", headers=auth_headers, json={"name": "test-client"})
    assert response.status_code == 201
    return response.json()["key"]


@pytest.fixture
def funded_api_key(client, auth_headers, api_key, webhook_secret):
    order = client.post("/billing/topups", headers=auth_headers, json={"amount": 100000})
    assert order.status_code == 201
    body = json.dumps({
        "id": f"evt_{order.json()['id']}",
        "order_id": order.json()["id"],
        "type": "topup.succeeded",
        "amount": 100000,
    }).encode()
    signature = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    assert client.post("/billing/webhook", content=body, headers={
        "Content-Type": "application/json", "X-Webhook-Signature": signature,
    }).status_code == 200
    return api_key
