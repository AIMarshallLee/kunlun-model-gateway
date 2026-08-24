from __future__ import annotations

import hashlib
import hmac
import json


def _register_login(client, email):
    password = "a sufficiently long password"
    assert client.post("/auth/register", json={"email": email, "password": password}).status_code == 201
    token = client.post("/auth/login", json={"email": email, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_wallet_keys_costs_and_revocation_are_tenant_isolated(client, auth_headers, api_key, webhook_secret):
    second_headers = _register_login(client, "second@example.com")
    second_key = client.post("/v1/keys", headers=second_headers, json={"name": "second"}).json()["key"]

    order = client.post("/billing/topups", headers=auth_headers, json={"amount": 3000}).json()
    body = json.dumps({
        "id": "evt_tenant_one",
        "order_id": order["id"],
        "type": "topup.succeeded",
        "amount": 3000,
    }).encode()
    signature = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    assert client.post("/billing/webhook", content=body, headers={
        "Content-Type": "application/json", "X-Webhook-Signature": signature,
    }).status_code == 200

    assert client.get("/billing/balance", headers=auth_headers).json()["balance"] == 3000
    assert client.get("/billing/balance", headers=second_headers).json()["balance"] == 0
    assert client.post("/v1/keys/revoke", headers=second_headers, json={"key": api_key}).status_code == 404
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {api_key}"}).status_code == 200
    assert client.get("/billing/costs", headers={"Authorization": f"Bearer {second_key}"}).json()["entries"] == []
