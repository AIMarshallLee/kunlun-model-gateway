from __future__ import annotations

import json

from sqlalchemy import select

from app.models import ModelRequest, ProviderAttempt, Wallet
from app import providers
from app import create_app


def test_final_attempt_carries_usage_cost_pricing_and_actual_timing(client, funded_api_key, monkeypatch):
    class PricedProvider:
        provider_name = "priced-provider"

        def upstream_prices(self, _model, _input, _output):
            return 2_000_000, 3_000_000

        async def __call__(self, _payload):
            return {
                "model": "test-model", "choices": [],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }

    provider = PricedProvider()
    monkeypatch.setattr(providers, "ordered_clients", [provider])
    response = client.post("/v1/chat/completions", headers={
        "Authorization": f"Bearer {funded_api_key}",
    }, json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 200
    with client.app.state.SessionLocal() as session:
        request = session.scalar(select(ModelRequest))
        attempt = session.scalar(select(ProviderAttempt))
        assert request is not None and attempt is not None
        assert request.final_attempt_id == attempt.id
        assert request.cost_state == "settled"
        assert attempt.is_final is True
        assert attempt.billing_status == "settled"
        assert attempt.input_tokens == request.input_tokens == 5
        assert attempt.output_tokens == request.output_tokens == 2
        assert attempt.upstream_cost_microusd == request.upstream_cost_microusd
        assert attempt.completed_at is not None
        assert attempt.duration_ms is not None and attempt.duration_ms >= 0
        assert json.loads(attempt.pricing_snapshot_json) == {
            "input_microusd_per_million": 2_000_000,
            "output_microusd_per_million": 3_000_000,
        }


def test_uncertain_attempt_stops_fallback_and_keeps_reservation(client, funded_api_key, monkeypatch):
    async def uncertain(_payload):
        from gateway import ProviderError
        raise ProviderError(502, category="upstream_timeout", safe_to_failover=False, request_may_be_billable=True)

    async def must_not_run(_payload):
        raise AssertionError("uncertain upstream attempt must not fail over")

    monkeypatch.setattr(providers, "ordered_clients", [uncertain, must_not_run])
    response = client.post("/v1/chat/completions", headers={
        "Authorization": f"Bearer {funded_api_key}",
    }, json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 502
    with client.app.state.SessionLocal() as session:
        request = session.scalar(select(ModelRequest))
        attempt = session.scalar(select(ProviderAttempt))
        wallet = session.scalar(select(Wallet))
        assert request is not None and attempt is not None and wallet is not None
        assert request.status == "pending_reconciliation"
        assert request.cost_state == "pending_reconciliation"
        assert request.final_attempt_id == attempt.id
        assert attempt.billing_status == "unknown"
        assert attempt.is_final is True
        assert wallet.reserved_microusd == request.reserved_microusd


def test_readyz_fails_closed_when_byok_vault_probe_fails(tmp_path):
    class UnreadyVault:
        def probe(self):
            return False

    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'ready.sqlite'}",
        credential_vault=UnreadyVault(),
    )
    app.state.settings.gateway_mode = "byok"
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["credential_vault"] == "failed"
