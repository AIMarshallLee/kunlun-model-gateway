import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import create_app, providers
from app.models import ModelRequest, PlatformDailyBudget, ProviderAttempt, User, Wallet
from app.services.identity import InMemoryEmailSender
from app.services.ops_tokens import mint_operator_token

OPS = "managed-test-operator-signing-secret-123456"


def test_managed_mode_cannot_be_enabled_without_platform_boundary():
    with pytest.raises(RuntimeError, match="platform|平台"):
        create_app(environment="test", gateway_mode="managed_gateway")


@pytest.fixture
def managed(tmp_path, monkeypatch):
    from app.services.platform_credentials import InMemoryPlatformVault
    monkeypatch.setenv("KUNLUN_PROVIDERS_JSON", json.dumps([{
        "name": "openai", "base_url": "https://api.openai.com/v1", "models": ["test-model"],
        "pricing": {"test-model": {"input_microusd_per_million": 500_000, "output_microusd_per_million": 500_000}},
    }]))
    monkeypatch.setenv("KUNLUN_PROVIDER_HOST_ALLOWLIST", "api.openai.com")
    monkeypatch.setenv("KUNLUN_PLATFORM_DAILY_BUDGET_MICROUSD", "1000000")
    sender = InMemoryEmailSender()
    vault = InMemoryPlatformVault()
    calls = []
    def transport(request):
        calls.append(request.headers["authorization"])
        override = getattr(app.state, "test_upstream", None)
        if override is not None:
            return override(request)
        return httpx.Response(200, json={"model": "test-model", "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                                        "usage": {"prompt_tokens": 4, "completion_tokens": 2}})
    monkeypatch.setattr(providers, "build_managed_provider_client", lambda catalog, *, api_key, allowed_hosts:
        providers.OpenAICompatibleProvider(provider_name=catalog["name"], base_url=catalog["base_url"], api_key=api_key,
            models=set(catalog["models"]), pricing=catalog["pricing"], transport=httpx.MockTransport(transport)))
    app = create_app(database_url=f"sqlite:///{tmp_path / 'managed.db'}", environment="test",
                     gateway_mode="managed_gateway", platform_vault=vault, identity_sender=sender,
                     require_email_verification=True, public_signup=True, operator_signing_secret=OPS,
                     enable_test_payments=True, payment_webhook_secret="test-webhook-secret", rate_limit_per_minute=100)
    with TestClient(app) as client:
        credentials = {"email": "managed@example.com", "password": "a sufficiently long password"}
        assert client.post("/auth/register", json=credentials).status_code in (201, 202)
        assert client.post("/auth/login", json=credentials).status_code == 401
        assert client.post("/auth/verify-email", json={"token": sender.messages[-1].token}).status_code == 200
        auth = {"Authorization": "Bearer " + client.post("/auth/login", json=credentials).json()["access_token"]}
        key = client.post("/v1/keys", headers=auth, json={"name": "managed"}).json()["key"]
        ops = {"X-Kunlun-Ops-Token": mint_operator_token(OPS, subject="operator", scopes={"channels:write", "channels:read"})}
        yield client, auth, key, ops, calls


def fund(client, auth):
    order = client.post("/billing/topups", headers=auth, json={"amount": 100000}).json()
    body = json.dumps({"id": "evt-" + order["id"], "order_id": order["id"], "type": "topup.succeeded", "amount": 100000}).encode()
    headers = {"Content-Type": "application/json", "X-Webhook-Signature": hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()}
    for _ in range(10):
        assert client.post("/billing/webhook", content=body, headers=headers).status_code == 200


def test_managed_registration_funding_call_and_original_task(managed):
    client, auth, key, ops, calls = managed
    payload = {"secret": "platform-inert-key", "operation_id": "channel-1", "reason": "approved test supply channel"}
    assert client.put("/ops/channels/openai", json=payload, headers=auth).status_code in (401, 404)
    saved = client.put("/ops/channels/openai", json=payload, headers=ops)
    assert saved.status_code == 200, saved.text
    assert "platform-inert-key" not in saved.text
    headers = {"Authorization": "Bearer " + key, "Idempotency-Key": "managed-task-1"}
    task = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 16}
    assert client.post("/v1/chat/completions", headers=headers, json=task).status_code == 402
    assert calls == []
    fund(client, auth)
    result = client.post("/v1/chat/completions", headers=headers, json=task)
    assert result.status_code == 200, result.text
    assert calls == ["Bearer platform-inert-key"]
    assert client.post("/v1/chat/completions", headers=headers, json=task).status_code == 409
    with client.app.state.SessionLocal() as db:
        request = db.scalar(select(ModelRequest))
        assert request.billing_mode == "managed_gateway"
        assert request.charged_microusd == 6 and request.upstream_cost_microusd == 3
        wallet = db.scalar(select(Wallet))
        assert wallet.balance_microusd == 99994 and wallet.reserved_microusd == 0
        budget = db.scalar(select(PlatformDailyBudget))
        assert budget.spent_microusd == 3 and budget.reserved_microusd == 0
    assert client.get("/v1/provider-connections", headers=auth).status_code == 403


def test_managed_missing_idempotency_and_disabled_channel_never_call(managed):
    client, auth, key, ops, calls = managed
    fund(client, auth)
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]}
    assert client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key}, json=payload).status_code == 428
    assert client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key, "Idempotency-Key": "no-channel"}, json=payload).status_code == 503
    assert calls == []


def ready_call(managed):
    client, auth, key, ops, calls = managed
    fund(client, auth)
    assert client.put("/ops/channels/openai", headers=ops, json={
        "secret": "inert-platform", "operation_id": "provision", "reason": "isolated platform supply test",
    }).status_code == 200
    return client, {"Authorization": "Bearer " + key, "Idempotency-Key": "one-call"}, {
        "model": "test-model", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 16,
    }


@pytest.mark.parametrize("kind", ["missing_usage", "500", "timeout", "cost_overrun"])
def test_unknown_cost_preserves_both_holds_and_stops_retry(managed, kind):
    client, headers, payload = ready_call(managed)
    def response(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("inert test", request=request)
        if kind == "500":
            return httpx.Response(500, json={"error": "inert"})
        body = {"choices": []}
        if kind == "cost_overrun":
            body["usage"] = {"prompt_tokens": 999999, "completion_tokens": 2}
        return httpx.Response(200, json=body)
    client.app.state.test_upstream = response
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code in (502, 503)
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 409
    assert len(managed[-1]) == 1
    with client.app.state.SessionLocal() as db:
        request = db.scalar(select(ModelRequest))
        wallet = db.scalar(select(Wallet))
        budget = db.scalar(select(PlatformDailyBudget))
        assert request.status == "pending_reconciliation"
        assert wallet.reserved_microusd == request.reserved_microusd > 0
        assert budget.reserved_microusd == request.platform_reserved_microusd > 0
        assert budget.spent_microusd == 0
        assert db.scalar(select(ProviderAttempt)).billing_status == "unknown"


def test_global_cost_denial_rolls_back_customer_reservation(managed):
    client, headers, payload = ready_call(managed)
    client.app.state.settings.platform_daily_budget_microusd = 1
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 503
    assert managed[-1] == []
    with client.app.state.SessionLocal() as db:
        wallet = db.scalar(select(Wallet))
        assert wallet.balance_microusd == 100000 and wallet.reserved_microusd == 0
        assert db.scalar(select(ModelRequest)) is None


def test_revoke_and_safe_rejection_release_platform_hold(managed):
    client, headers, payload = ready_call(managed)
    client.app.state.test_upstream = lambda _: httpx.Response(429, json={"error": "limit"})
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 429
    with client.app.state.SessionLocal() as db:
        budget = db.scalar(select(PlatformDailyBudget))
        assert budget.reserved_microusd == budget.spent_microusd == 0
        assert db.scalar(select(Wallet)).balance_microusd == 100000
    ops = managed[3]
    assert client.post("/ops/channels/openai/revoke", headers=ops, json={
        "operation_id": "revoke", "reason": "isolated channel revocation test",
    }).status_code == 200
    headers["Idempotency-Key"] = "after-revoke"
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 503
    assert len(managed[-1]) == 1


def test_preexisting_unverified_account_cannot_spend_or_buy(managed):
    client, headers, payload = ready_call(managed)
    with client.app.state.SessionLocal() as db:
        db.scalar(select(User)).email_verified_at = None
        db.commit()
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 401
    assert client.post("/billing/topups", headers=managed[1], json={"amount": 100000}).status_code == 401
    assert managed[-1] == []


def test_cost_rounding_never_uses_float_precision():
    from app.services.gateway_billing import token_cost
    assert token_cost(999_999, 999_999_999_999) == 999_999_000_000


@pytest.mark.parametrize("first_status,expected_calls", [(429, 2), (500, 1), (401, 1)])
def test_managed_failover_only_for_definitely_non_billable_errors(managed, first_status, expected_calls):
    client, headers, payload = ready_call(managed)
    client.app.state.settings.providers.append({
        "name": "deepseek", "base_url": "https://api.deepseek.com/v1", "models": ["test-model"],
        "pricing": {"test-model": {"input_microusd_per_million": 500000, "output_microusd_per_million": 500000}},
    })
    client.app.state.settings.provider_host_allowlist.add("api.deepseek.com")
    client.app.state.platform_vault.write(provider="deepseek", secret="inert-second", operation_id="second",
                                         actor="ci-operator", reason="isolated fallback channel test")
    def upstream(request):
        if request.url.host == "api.openai.com":
            return httpx.Response(first_status, json={"error": "synthetic first failure"})
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2}})
    client.app.state.test_upstream = upstream
    response = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert len(managed[-1]) == expected_calls
    assert response.status_code == (200 if first_status == 429 else 502 if first_status == 500 else 401)
    with client.app.state.SessionLocal() as db:
        request = db.scalar(select(ModelRequest))
        attempts = db.scalars(select(ProviderAttempt).order_by(ProviderAttempt.ordinal)).all()
        assert len(attempts) == expected_calls
        if first_status == 429:
            assert request.charged_microusd == 6
            assert attempts[0].billing_status == "not_billed"
            assert attempts[1].upstream_cost_microusd == 3


def test_managed_sse_settles_from_final_usage(managed):
    client, headers, payload = ready_call(managed)
    events = [{"choices": [{"delta": {"content": "test"}}]},
              {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}]
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
    client.app.state.test_upstream = lambda _: httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})
    response = client.post("/v1/chat/completions", headers=headers, json={**payload, "stream": True})
    assert response.status_code == 200 and "[DONE]" in response.text
    with client.app.state.SessionLocal() as db:
        assert db.scalar(select(ModelRequest)).status == "settled"
        assert db.scalar(select(PlatformDailyBudget)).spent_microusd == 3


def test_deadline_is_absolute_across_stream_chunks():
    import asyncio
    from app.services.request_limits import chunks_with_deadline
    from gateway import ProviderError
    async def scenario():
        async def endless():
            while True:
                await asyncio.sleep(0.005)
                yield b"data"
        import time
        with pytest.raises(ProviderError) as error:
            async for _ in chunks_with_deadline(endless(), time.monotonic() + 0.025):
                pass
        assert error.value.category == "request_deadline_exceeded"
        assert error.value.request_may_be_billable and not error.value.safe_to_failover
    asyncio.run(scenario())


def test_operator_budget_and_original_credential_operation_are_metadata_only(managed):
    client, headers, payload = ready_call(managed)
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 200
    assert client.get("/ops/platform-budget", headers=managed[1]).status_code == 401
    metrics = {"X-Kunlun-Ops-Token": mint_operator_token(OPS, subject="auditor", scopes={"metrics:read"})}
    report = client.get("/ops/platform-budget", headers=metrics)
    assert report.status_code == 200 and report.json()["spent"] == 3
    assert report.json()["reserved"] == 0 and report.json()["pending_reconciliation_count"] == 0
    original = client.get("/ops/channel-operations/provision", headers=managed[3])
    assert original.status_code == 200 and original.json()["action"] == "provision"
    assert "inert-platform" not in original.text and original.headers["cache-control"] == "no-store"
    assert client.get("/ops/channel-operations/absent", headers=managed[3]).status_code == 404


@pytest.mark.parametrize("action", ["release", "settle"])
def test_manual_reconciliation_resolves_original_day_hold_once(managed, action):
    client, headers, payload = ready_call(managed)
    client.app.state.test_upstream = lambda _: httpx.Response(500, json={"error": "uncertain"})
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 502
    with client.app.state.SessionLocal() as db:
        request_id = db.scalar(select(ModelRequest.id))
    operator = {"X-Kunlun-Ops-Token": mint_operator_token(OPS, subject="reconciler", scopes={"reconciliation:write"})}
    decision = {"action": action, "reason": "verified synthetic supplier usage only", "input_tokens": 4,
                "output_tokens": 2, "upstream_cost_microusd": 2000000}
    response = client.post(f"/ops/reconciliation/{request_id}", headers=operator, json=decision)
    assert response.status_code == 200, response.text
    assert client.post(f"/ops/reconciliation/{request_id}", headers=operator, json=decision).status_code == 404
    with client.app.state.SessionLocal() as db:
        day = db.scalar(select(PlatformDailyBudget))
        assert day.reserved_microusd == 0
        assert day.spent_microusd == (2000000 if action == "settle" else 0)
        assert db.scalar(select(Wallet)).reserved_microusd == 0
    if action == "settle":
        headers["Idempotency-Key"] = "after-overrun"
        assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 503
        assert len(managed[-1]) == 1


@pytest.mark.parametrize("mutation", [
    {"api_key_env": "SHARED_KEY"}, {"name": "arbitrary"}, {"base_url": "http://127.0.0.1/v1"},
    {"models": ["missing-model"]}, {"pricing": {}},
    {"pricing": {"test-model": {"input_microusd_per_million": 0.5, "output_microusd_per_million": 1}}},
])
def test_managed_catalog_is_validated_before_serving(managed, monkeypatch, mutation):
    from app.config import Settings
    catalog = json.loads(__import__("os").environ["KUNLUN_PROVIDERS_JSON"])
    catalog[0].update(mutation)
    monkeypatch.setenv("KUNLUN_PROVIDERS_JSON", json.dumps(catalog))
    with pytest.raises(RuntimeError):
        Settings.from_env(environment="test", gateway_mode="managed_gateway", require_email_verification=True)


def test_managed_checkout_verified_event_and_full_refund_domain(managed):
    """Injected synthetic payment protocol, NOT an official SDK or real cash proof."""
    from app.services.live_payments import CheckoutResult, WebhookResult, RefundResult, PaymentBridgeError
    client, auth, _key, _ops, _calls = managed
    class SimulatedCheckout:
        order_id = ""
        creates = 0
        refunds = 0
        async def create_checkout(self, **kwargs):
            self.creates += 1
            self.order_id = kwargs["order_id"]
            return CheckoutResult(order_id=self.order_id, payment_amount_minor=100, currency="USD", status="pending",
                checkout_url="https://pay.example.test/ci-only", provider_transaction_id="ci-transaction",
                request_timestamp="1700000000", request_nonce="ci-nonce")
        def verify_webhook(self, raw_body, headers):
            if raw_body != b"synthetic-verified-event":
                raise PaymentBridgeError("synthetic signature rejection")
            return WebhookResult(order_id=self.order_id, payment_amount_minor=100, currency="USD", status="paid",
                provider_transaction_id="ci-transaction", event_id="ci-event", event_type="payment.succeeded",
                nonce="ci-nonce", idempotency_key="ci-event")
        async def refund_payment(self, **kwargs):
            self.refunds += 1
            return RefundResult(order_id=self.order_id, payment_amount_minor=100, currency="USD", status="refunded",
                provider_transaction_id="ci-transaction", provider_refund_id="ci-refund")
    adapter = SimulatedCheckout()
    client.app.state.live_payment_bridge = adapter
    client.app.state.settings.payment_provider = "simulated_checkout"
    client.app.state.settings.topup_packages = {"ci": {"payment_amount_minor": 100, "payment_currency": "USD", "credit_amount_microusd": 1000000}}
    headers = {**auth, "Idempotency-Key": "ci-checkout"}
    order = client.post("/billing/checkout", headers=headers, json={"sku": "ci"})
    assert order.status_code == 201, order.text
    assert client.post("/billing/checkout", headers=headers, json={"sku": "ci"}).status_code == 201
    assert adapter.creates == 1
    assert client.get("/billing/balance", headers=auth).json()["balance"] == 0
    assert client.post("/billing/live/webhook", content=b"forged").status_code == 401
    for _ in range(10):
        assert client.post("/billing/live/webhook", content=b"synthetic-verified-event").status_code == 200
    assert client.get("/billing/balance", headers=auth).json()["balance"] == 1000000
    operator = {"X-Kunlun-Ops-Token": mint_operator_token(OPS, subject="payments-operator", scopes={"payments:write"})}
    refund = client.post(f"/ops/payments/{adapter.order_id}/refund", headers=operator,
                         json={"reason": "synthetic full refund acceptance", "idempotency_key": "ci-refund-command"})
    assert refund.status_code == 200, refund.text
    assert refund.json()["status"] == "refunded" and adapter.refunds == 1
    assert client.get("/billing/balance", headers=auth).json()["balance"] == 0
