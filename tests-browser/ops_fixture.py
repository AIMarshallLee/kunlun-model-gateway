"""Disposable loopback operator acceptance. All supply/payment/identity is fake."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import httpx
import pytest
from sqlalchemy import select
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from checkout_fixture import BrowserPaymentBridge
from test_managed_gateway import OPS, managed, ready_call
from app.models import ModelRequest
from app.services.ops_tokens import mint_operator_token


if __name__ == "__main__":
    with TemporaryDirectory(prefix="kunlun-ops-ui-") as directory, pytest.MonkeyPatch.context() as patch:
        fixture = managed.__wrapped__(Path(directory), patch)
        context = next(fixture)
        client, auth, *_ = context
        app = client.app
        _, model_headers, payload = ready_call(context)
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
        with app.state.SessionLocal() as db:
            requests = {row.idempotency_key: row.id for row in db.scalars(select(ModelRequest))}

        @app.get("/__fixture__/operator")
        def operator(profile: str = "read"):
            scopes = {"console:read", "accounts:read", "payments:read", "reconciliation:read", "models:read", "channels:read", "metrics:read", "audit:read"}
            if profile == "write":
                scopes |= {"accounts:write", "payments:write", "reconciliation:write", "payments:risk:write", "models:write"}
            return {"token": mint_operator_token(OPS, subject="synthetic-operator", scopes=scopes, ttl_seconds=300),
                    "requests": requests, "order_id": bridge.webhook.order_id}

        @app.get("/__fixture__/refund-calls")
        def refund_calls():
            return {"count": len(bridge.refund_calls)}

        try:
            uvicorn.run(app, host="127.0.0.1", port=8797, access_log=False, log_level="warning")
        finally:
            fixture.close()
