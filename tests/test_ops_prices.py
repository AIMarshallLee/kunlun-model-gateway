from sqlalchemy import select
import pytest

from app import _seed_prices
from app.models import ApiKey, ModelPrice, ModelRequest, OperatorAction
from app.services.gateway_billing import reserve_model_request, settle_model_request
from tests.test_managed_gateway import managed, ready_call
from tests.test_ops_console import operator


def catalog(managed):
    client, *_ = managed
    response = client.get("/ops/models", headers=operator("models:read"))
    assert response.status_code == 200
    return response.json()["items"][0]


def change(client, model_id, **overrides):
    payload = {"action": "publish", "expected_version": 1, "operation_id": "price-change-1", "reason": "verified synthetic retail price change",
               "input_microusd_per_million": 2_000_000, "output_microusd_per_million": 3_000_000, "max_output_tokens": 1024}
    payload.update(overrides)
    return client.post(f"/ops/models/{model_id}/price", headers=operator("models:write"), json=payload)


def test_price_operations_require_separate_scope(managed):
    client, auth, *_ = managed
    assert client.get("/ops/models", headers=auth).status_code == 401
    item = catalog(managed)
    assert client.post(f"/ops/models/{item['id']}/price", headers=operator("models:read"), json={
        "action": "unpublish", "expected_version": 1, "operation_id": "no-write", "reason": "must not allow read-only changes",
    }).status_code == 401


def test_publish_freezes_old_requests_and_bootstrap_does_not_reset_price(managed):
    client, _, payload = ready_call(managed)
    item = catalog(managed)
    with client.app.state.SessionLocal() as db:
        key = db.scalar(select(ApiKey))
        held = reserve_model_request(db, user_id=key.user_id, api_key_id=key.id, model="test-model",
            billable_payload=payload, max_output_tokens=16, idempotency_key="before-price-change",
            managed_cost_prices=(500000, 500000), platform_daily_limit=1000000)
    assert change(client, item["id"]).status_code == 201
    assert change(client, item["id"]).status_code == 409
    assert change(client, item["id"], operation_id="stale-command").status_code == 409
    _seed_prices(client.app)
    with client.app.state.SessionLocal() as db:
        settle_model_request(db, request_id=held.request_id, response={"usage": {"prompt_tokens": 4, "completion_tokens": 2}},
                             provider="test", fallback_count=0, upstream_cost_override=3)
        old = db.get(ModelRequest, held.request_id)
        assert old.price_version == 1 and old.charged_microusd == 6
        prices = db.scalars(select(ModelPrice).order_by(ModelPrice.version)).all()
        assert len(prices) == 2
        assert prices[0].input_microusd_per_million == 1000000 and not prices[0].active
        assert prices[1].input_microusd_per_million == 2000000 and prices[1].active
        assert len(db.scalars(select(OperatorAction).where(OperatorAction.action == "model_publish")).all()) == 1
    public = client.get("/public/catalog").json()["models"][0]
    assert public["price_version"] == 2 and public["input_microusd_per_million"] == 2000000


def test_unpublish_blocks_outbound_survives_restart_and_can_relist(managed):
    client, headers, payload = ready_call(managed)
    item = catalog(managed)
    result = client.post(f"/ops/models/{item['id']}/price", headers=operator("models:write"), json={
        "action": "unpublish", "expected_version": 1, "operation_id": "unpublish-1", "reason": "synthetic maintenance stop new work",
    })
    assert result.status_code == 201
    _seed_prices(client.app)
    assert client.get("/public/catalog").json()["models"] == []
    assert client.get("/v1/models", headers=headers).json()["data"] == []
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 404
    assert managed[-1] == []
    assert change(client, item["id"], expected_version=2, operation_id="relist").status_code == 201
    detail = client.get(f"/ops/models/{item['id']}", headers=operator("models:read")).json()
    assert detail["model"]["version"] == 3 and detail["model"]["active"]
    assert [row["version"] for row in detail["history"]] == [3, 2, 1]


@pytest.mark.parametrize("values", [{"input_microusd_per_million": 0}, {"input_microusd_per_million": 1.5},
    {"input_microusd_per_million": True}, {"output_microusd_per_million": 499999}, {"max_output_tokens": 999999999},
    {"expected_version": 0}, {"operation_id": "bad key"}, {"reason": " " * 15},
    {"input_microusd_per_million": None}, {"action": "unpublish"}])
def test_invalid_or_below_current_supply_prices_are_rejected(managed, values):
    client, *_ = managed
    assert change(client, catalog(managed)["id"], **values).status_code == 422


def test_history_ids_cannot_be_used_as_a_second_lock_anchor(managed):
    client, *_ = managed
    item = catalog(managed)
    created = change(client, item["id"]).json()["model"]
    for anchor in (created["price_id"], "missing-model"):
        assert client.get(f"/ops/models/{anchor}", headers=operator("models:read")).status_code == 404
        assert change(client, anchor, expected_version=2, operation_id="wrong-anchor").status_code == 404
    assert catalog(managed)["version"] == 2


@pytest.mark.parametrize("setting", ["unsupported_model", "no_routes", "platform_output_cap"])
def test_publication_requires_configured_supply_and_platform_policy(managed, setting):
    client, *_ = managed
    item = catalog(managed)
    settings = client.app.state.settings
    if setting == "unsupported_model":
        settings.models = {}
    elif setting == "no_routes":
        settings.providers = []
    else:
        settings.max_output_tokens = 16
    assert change(client, item["id"]).status_code == 422
    assert catalog(managed)["version"] == 1


def test_already_unlisted_and_non_managed_mode_reject_changes(managed):
    client, *_ = managed
    item = catalog(managed)
    body = {"action": "unpublish", "expected_version": 1, "operation_id": "first-stop", "reason": "verified synthetic model shutdown"}
    path = f"/ops/models/{item['id']}/price"
    assert client.post(path, headers=operator("models:write"), json=body).status_code == 201
    body.update(expected_version=2, operation_id="second-stop")
    assert client.post(path, headers=operator("models:write"), json=body).status_code == 409
    client.app.state.settings.gateway_mode = "byok"
    assert client.get("/ops/models", headers=operator("models:read")).status_code == 404
    assert client.post(path, headers=operator("models:write"), json=body).status_code == 404
