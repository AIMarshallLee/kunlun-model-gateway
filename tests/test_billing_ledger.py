from __future__ import annotations

import hashlib
import hmac
import json

from app.services.gateway_billing import (
    estimate_tokens,
    input_token_reservation_upper_bound,
)


def _webhook(client, secret, event_id, order_id, amount):
    body = json.dumps({
        "id": event_id,
        "order_id": order_id,
        "type": "topup.succeeded",
        "amount": amount,
    }).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/billing/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Webhook-Signature": signature},
    )


def test_input_reservation_uses_utf8_upper_bound_for_chinese_and_emoji():
    payload = {
        "messages": [{"role": "user", "content": "你好🙂" * 1000}],
        "tools": [{"type": "function", "function": {
            "name": "查找", "description": "说明🙂" * 1000,
        }}],
    }
    serialized_bytes = len(json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))
    reserved = input_token_reservation_upper_bound(payload)
    assert reserved >= serialized_bytes + 4096
    assert reserved > estimate_tokens(payload) * 4


def test_topup_webhook_is_hmac_verified_and_idempotent(client, auth_headers, webhook_secret):
    order = client.post("/billing/topups", headers=auth_headers, json={"amount": 1000})
    assert order.status_code == 201
    assert "webhook_secret" not in order.json()
    event_id = "evt_topup_once"

    assert _webhook(client, webhook_secret, event_id, order.json()["id"], 1000).status_code == 200
    assert _webhook(client, webhook_secret, event_id, order.json()["id"], 1000).json()["duplicate"] is True
    assert client.get("/billing/balance", headers=auth_headers).json()["balance"] == 1000
    assert _webhook(client, "wrong", "different", order.json()["id"], 1000).status_code == 401


def test_topup_webhook_rejects_amount_tampering(client, auth_headers, webhook_secret):
    order = client.post("/billing/topups", headers=auth_headers, json={"amount": 1000}).json()
    response = _webhook(client, webhook_secret, "evt_tampered", order["id"], 999999)
    assert response.status_code == 409
    assert client.get("/billing/balance", headers=auth_headers).json()["balance"] == 0


def test_balance_ledger_is_immutable_and_reconciles(client, auth_headers, webhook_secret):
    order = client.post("/billing/topups", headers=auth_headers, json={"amount": 2000}).json()
    assert _webhook(client, webhook_secret, "evt_ledger", order["id"], 2000).status_code == 200
    ledger = client.get("/billing/ledger", headers=auth_headers)
    assert ledger.status_code == 200
    assert ledger.json()["entries"][-1]["kind"] == "credit"
    assert client.post("/billing/ledger", headers=auth_headers, json={"amount": 1}).status_code == 405
