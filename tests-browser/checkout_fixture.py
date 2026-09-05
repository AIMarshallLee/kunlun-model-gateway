"""Loopback-only, disposable managed-mode fixture. NEVER a deployment entrypoint.

Run with the project's test dependencies; all accounts, payments and model
adapters are synthetic. No credentials or payment providers are contacted.
"""
from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from test_managed_gateway import managed
from test_live_payment_routes import FakeLiveBridge


class BrowserPaymentBridge(FakeLiveBridge):
    async def create_checkout(self, **kwargs):
        result = await super().create_checkout(**kwargs)
        self.webhook = replace(self.webhook, order_id=result.order_id,
                               payment_amount_minor=result.payment_amount_minor,
                               currency=result.currency,
                               provider_transaction_id=result.provider_transaction_id)
        return result


if __name__ == "__main__":
    with TemporaryDirectory(prefix="kunlun-checkout-ui-") as directory, pytest.MonkeyPatch.context() as patch:
        fixture = managed.__wrapped__(Path(directory), patch)
        client, *_ = next(fixture)
        app = client.app
        bridge = BrowserPaymentBridge()
        app.state.live_payment_bridge = bridge
        app.state.settings.payment_provider = "simulated_checkout"
        app.state.settings.public_base_url = "https://gateway.example"
        app.state.settings.topup_packages = {"starter": {
            "payment_amount_minor": 1999, "payment_currency": "USD", "credit_amount_microusd": 1_000_000,
        }}

        @app.get("/__fixture__/payment-calls")
        def payment_calls():
            return {"count": len(bridge.checkout_calls)}

        try:
            uvicorn.run(app, host="127.0.0.1", port=8796, access_log=False, log_level="warning")
        finally:
            fixture.close()
