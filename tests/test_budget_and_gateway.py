from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app import providers
from gateway import ProviderError


def test_user_cannot_manually_settle_or_refund_budget(client, auth_headers):
    created = client.post("/budgets", headers=auth_headers, json={"amount": 100}).json()
    budget_id = created["id"]
    assert client.get(f"/budgets/{budget_id}", headers=auth_headers).json()["available"] == 100
    assert client.post(f"/budgets/{budget_id}/settle", headers=auth_headers, json={"amount": 35}).status_code in {404, 405}
    assert client.post(f"/budgets/{budget_id}/refund", headers=auth_headers, json={"amount": 25}).status_code in {404, 405}


def test_insufficient_balance_and_budget_are_rejected(client, auth_headers, api_key):
    assert client.post("/budgets", headers=auth_headers, json={"amount": 1}).status_code == 201
    response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json={
        "model": "test-model", "messages": [{"role": "user", "content": "hello"}]
    })
    assert response.status_code == 402


def test_budget_replacement_is_blocked_while_requests_are_reserved(
    client, funded_api_key, auth_headers, monkeypatch,
):
    assert client.post("/budgets", headers=auth_headers, json={"amount": 100_000}).status_code == 201
    uncertain = AsyncMock(side_effect=ProviderError(
        504,
        category="provider_ambiguous_timeout",
        safe_to_failover=False,
        request_may_be_billable=True,
    ))
    monkeypatch.setattr(providers, "ordered_clients", [uncertain])
    call = client.post("/v1/chat/completions", headers={
        "Authorization": f"Bearer {funded_api_key}",
    }, json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}]})
    assert call.status_code == 502

    replacement = client.post("/budgets", headers=auth_headers, json={"amount": 50_000})
    assert replacement.status_code == 409
    assert "待结算" in replacement.json()["detail"]


def test_budget_replacement_carries_month_to_date_spend(client, funded_api_key, auth_headers):
    first = client.post("/budgets", headers=auth_headers, json={"amount": 100_000}).json()
    call = client.post("/v1/chat/completions", headers={
        "Authorization": f"Bearer {funded_api_key}",
    }, json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}]})
    assert call.status_code == 200
    spent = client.get(f"/budgets/{first['id']}", headers=auth_headers).json()["spent"]
    assert spent > 0

    replacement = client.post("/budgets", headers=auth_headers, json={"amount": 50_000})
    assert replacement.status_code == 201
    assert replacement.json()["spent"] == spent
    assert client.get(f"/budgets/{first['id']}", headers=auth_headers).json()["status"] == "superseded"


def test_openai_compatible_models_and_completion_keep_prompt_out_of_logs(client, funded_api_key, caplog):
    secret_prompt = "UNIQUE-DO-NOT-LOG-9f4c"
    models = client.get("/v1/models", headers={"Authorization": f"Bearer {funded_api_key}"})
    assert models.status_code == 200
    assert all("id" in model for model in models.json()["data"])
    completion = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {funded_api_key}"}, json={
        "model": "test-model", "messages": [{"role": "user", "content": secret_prompt}]
    })
    assert completion.status_code in (200, 402)
    assert secret_prompt not in caplog.text


def test_provider_429_fails_over(client, funded_api_key, monkeypatch):
    from app import providers
    first = AsyncMock(side_effect=ProviderError(429))
    second = AsyncMock(return_value={"id": "ok", "choices": [{"message": {"content": "done"}}]})
    monkeypatch.setattr(providers, "ordered_clients", [first, second])
    response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {funded_api_key}"}, json={
        "model": "test-model", "messages": [{"role": "user", "content": "hello"}]
    })
    assert response.status_code == 200
    assert second.await_count == 1


@pytest.mark.parametrize("status", [500, 502, 503])
def test_provider_5xx_is_held_for_reconciliation_without_failover(
    client, funded_api_key, monkeypatch, status,
):
    from app import providers
    first = AsyncMock(side_effect=ProviderError(status))
    second = AsyncMock(return_value={"id": "must-not-run"})
    monkeypatch.setattr(providers, "ordered_clients", [first, second])
    response = client.post("/v1/chat/completions", headers={
        "Authorization": f"Bearer {funded_api_key}",
    }, json={
        "model": "test-model", "messages": [{"role": "user", "content": "hello"}],
    })
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "reconciliation_pending"
    assert second.await_count == 0
    costs = client.get(
        "/billing/costs", headers={"Authorization": f"Bearer {funded_api_key}"},
    ).json()["entries"]
    assert costs[-1]["status"] == "pending_reconciliation"


def test_provider_4xx_does_not_fail_over(client, funded_api_key, monkeypatch):
    from app import providers
    first = AsyncMock(side_effect=ProviderError(400))
    second = AsyncMock(return_value={"id": "must-not-run"})
    monkeypatch.setattr(providers, "ordered_clients", [first, second])
    response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {funded_api_key}"}, json={
        "model": "test-model", "messages": [{"role": "user", "content": "hello"}]
    })
    assert response.status_code == 400
    assert second.await_count == 0


def test_large_tool_schema_is_fully_preauthorized_before_provider_call(
    client, funded_api_key, monkeypatch,
):
    from app import providers
    provider = AsyncMock(return_value={
        "id": "must-not-run",
        "choices": [{"message": {"content": "unexpected"}}],
    })
    monkeypatch.setattr(providers, "ordered_clients", [provider])
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {funded_api_key}"},
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "large_schema",
                    "description": "中" * 120_000,
                    "parameters": {"type": "object"},
                },
            }],
        },
    )
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "billing_rejected"
    assert provider.await_count == 0


def test_each_request_writes_cost_ledger_entry(client, funded_api_key, caplog):
    response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {funded_api_key}"}, json={
        "model": "test-model", "messages": [{"role": "user", "content": "hello"}]
    })
    assert response.status_code in (200, 402)
    costs = client.get("/billing/costs", headers={"Authorization": f"Bearer {funded_api_key}"})
    assert costs.status_code == 200
    assert costs.json()["entries"][-1]["request_id"]
    assert costs.json()["entries"][-1]["amount"] >= 0
