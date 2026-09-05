"""Key policies are admission controls, not client-side hints."""

import httpx
import pytest
from sqlalchemy import select

from app.models import ApiKey, ModelRequest
from app.services.gateway_billing import release_model_request
from tests.test_managed_gateway import managed, ready_call


def create_limited(managed, **policy):
    client, auth, *_ = managed
    result = client.post("/v1/keys", headers=auth, json={"name": "restricted", **policy})
    assert result.status_code == 201, result.text
    return result.json()


def send(client, key, payload, operation):
    return client.post("/v1/chat/completions", json=payload, headers={
        "Authorization": "Bearer " + key, "Idempotency-Key": operation,
    })


@pytest.mark.parametrize("policy", [
    {"allowed_models": []}, {"allowed_models": ["missing-model"]},
    {"allowed_models": ["test-model", "test-model"]},
    {"max_output_tokens": 0}, {"max_output_tokens": True},
    {"spend_limit_microusd": 0}, {"spend_limit_microusd": 1.5},
    {"spend_limit_microusd": "100"}, {"spend_limit_microusd": 10_000_000_001},
])
def test_invalid_key_policy_rejected(managed, policy):
    client, auth, *_ = managed
    result = client.post("/v1/keys", headers=auth, json={"name": "bad", **policy})
    assert result.status_code == 422
    assert len(client.get("/v1/keys", headers=auth).json()["keys"]) == 1


def test_key_scope_and_output_reject_before_any_outbound(managed):
    client, _, payload = ready_call(managed)
    limited = create_limited(managed, allowed_models=["test-model"], max_output_tokens=16)
    class MustNotCall:
        async def check(self, **kwargs):
            raise AssertionError("policy rejection reached moderation")
    client.app.state.content_safety = MustNotCall()
    assert send(client, limited["key"], {**payload, "model": "another-model"}, "scope").status_code == 403
    assert send(client, limited["key"], {**payload, "max_tokens": 17}, "output").status_code == 422
    assert managed[-1] == []
    with client.app.state.SessionLocal() as db:
        assert db.scalar(select(ModelRequest.id)) is None


def test_key_budget_rejects_before_moderation_and_model_call(managed):
    client, _, payload = ready_call(managed)
    limited = create_limited(managed, spend_limit_microusd=1)
    class MustNotCall:
        async def check(self, **kwargs):
            raise AssertionError("budget rejection reached moderation")
    client.app.state.content_safety = MustNotCall()
    assert send(client, limited["key"], payload, "budget").status_code == 402
    assert managed[-1] == []


def test_key_usage_uses_customer_charge_not_upstream_cost(managed):
    client, _, payload = ready_call(managed)
    limited = create_limited(managed, allowed_models=["test-model"], max_output_tokens=16,
                             spend_limit_microusd=5000)
    assert send(client, limited["key"], payload, "settle").status_code == 200
    assert send(client, limited["key"], payload, "settle").status_code == 409
    rows = client.get("/v1/keys", headers=managed[1]).json()["keys"]
    item = next(row for row in rows if row["id"] == limited["id"])
    assert item["allowed_models"] == ["test-model"]
    assert item["max_output_tokens"] == 16
    assert item["spend_limit_microusd"] == 5000
    assert item["spent_microusd"] == 6
    assert item["reserved_microusd"] == 0
    assert item["available_microusd"] == 4994
    assert "key" not in item and "secret_digest" not in item
    assert len(managed[-1]) == 1


def test_settled_spend_remains_in_cap_and_exact_boundary_is_allowed(managed):
    client, _, payload = ready_call(managed)
    # Measure the normal conservative hold, then make a synthetic key whose
    # cap allows that hold plus exactly one prior charge.
    assert send(client, managed[2], payload, "measure-hold").status_code == 200
    with client.app.state.SessionLocal() as db:
        reference = db.scalar(select(ModelRequest))
        cap = reference.reserved_microusd + reference.charged_microusd
    limited = create_limited(managed, spend_limit_microusd=cap)
    assert send(client, limited["key"], payload, "first-charge").status_code == 200
    assert send(client, limited["key"], payload, "exact-boundary").status_code == 200
    assert send(client, limited["key"], payload, "third-charge").status_code == 402
    assert len(managed[-1]) == 3


def test_unknown_cost_holds_key_cap_release_restores_it_and_keys_are_independent(managed):
    client, _, payload = ready_call(managed)
    limited = create_limited(managed, spend_limit_microusd=5000)
    client.app.state.test_upstream = lambda request: httpx.Response(500, json={"error": "unknown"})
    assert send(client, limited["key"], payload, "uncertain").status_code in (502, 503)
    assert send(client, limited["key"], payload, "overlap").status_code == 402
    assert len(managed[-1]) == 1
    item = next(row for row in client.get("/v1/keys", headers=managed[1]).json()["keys"] if row["id"] == limited["id"])
    assert item["spent_microusd"] == 0
    assert 2500 < item["reserved_microusd"] < 5000
    assert item["available_microusd"] == 5000 - item["reserved_microusd"]
    del client.app.state.test_upstream
    assert send(client, managed[2], payload, "independent").status_code == 200
    with client.app.state.SessionLocal() as db:
        request = db.scalar(select(ModelRequest).where(ModelRequest.api_key_id == limited["id"]))
        release_model_request(db, request.id, "test_verified_not_billed", allowed_statuses=("pending_reconciliation",))
    assert send(client, limited["key"], payload, "after-release").status_code == 200


def test_model_discovery_obeys_key_scope_without_exposing_other_keys(managed):
    client, auth, *_ = managed
    limited = create_limited(managed, allowed_models=["test-model"])
    # Other catalog rows must not appear for a restricted key.
    from app.models import ModelPrice
    with client.app.state.SessionLocal() as db:
        db.add(ModelPrice(id="other-price", model="other-model", version=1, input_microusd_per_million=1,
                          output_microusd_per_million=1, max_output_tokens=16, active=True))
        db.commit()
    result = client.get("/v1/models", headers={"Authorization": "Bearer " + limited["key"]})
    assert [item["id"] for item in result.json()["data"]] == ["test-model"]
    assert client.get("/v1/keys", headers={"Authorization": "Bearer " + limited["key"]}).status_code == 401
    with client.app.state.SessionLocal() as db:
        old_key = db.scalar(select(ApiKey).where(ApiKey.id != limited["id"]))
        assert old_key.spend_limit_microusd is None


def test_byok_cap_counts_verified_provider_cost_and_overrun_blocks_new_work(client, auth_headers):
    from app.services.gateway_billing import BillingError, key_usage, mark_pending_reconciliation, reserve_byok_model_request, settle_model_request
    created = client.post("/v1/keys", headers=auth_headers, json={
        "name": "byok-cap", "spend_limit_microusd": 5000,
    }).json()
    assert client.post("/budgets", headers=auth_headers, json={"amount": 100000, "kind": "provider_spend_cap"}).status_code == 201
    with client.app.state.SessionLocal() as db:
        key = db.get(ApiKey, created["id"])
        options = dict(user_id=key.user_id, api_key_id=key.id, model="test-model", billable_payload={}, max_output_tokens=16)
        first = reserve_byok_model_request(db, **options, idempotency_key="byok-first")
        with pytest.raises(BillingError, match="API Key"):
            reserve_byok_model_request(db, **options, idempotency_key="byok-overlap")
        mark_pending_reconciliation(db, first.request_id, "test_unknown")
        settle_model_request(db, request_id=first.request_id, response={"usage": {"prompt_tokens": 2, "completion_tokens": 2}},
                             provider="test", fallback_count=0, upstream_cost_override=5001,
                             allowed_statuses=("pending_reconciliation",), allow_budget_overrun=True)
        assert key_usage(db, key.user_id, key.id)[key.id] == {"spent_microusd": 5001, "reserved_microusd": 0}
        with pytest.raises(BillingError, match="API Key"):
            reserve_byok_model_request(db, **options, idempotency_key="byok-exhausted")
    item = client.get("/v1/keys", headers=auth_headers).json()["keys"][0]
    assert item["spent_microusd"] == 5001 and item["available_microusd"] == 0


def test_other_customer_cannot_observe_key_policy_or_usage(client, auth_headers):
    from tests.test_byok_credentials import _login
    own = client.post("/v1/keys", headers=auth_headers, json={"name": "private", "spend_limit_microusd": 1234}).json()
    outsider = _login(client, "outsider@example.test")
    assert client.get("/v1/keys", headers=outsider).json() == {"keys": []}
    assert client.post("/v1/keys/revoke", headers=outsider, json={"key_id": own["id"]}).status_code == 404
