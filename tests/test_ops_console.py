import pytest
from sqlalchemy import select

from app.models import ApiKey, OperatorAction
from app.services.ops_tokens import mint_operator_token
from tests.test_managed_gateway import OPS, fund, managed, ready_call


def operator(*scopes):
    return {"X-Kunlun-Ops-Token": mint_operator_token(OPS, subject="inert-operator", scopes=set(scopes))}


def test_account_key_history_is_paginated_filtered_and_scoped(managed):
    client, auth, *_ = managed
    from app.models import User
    with client.app.state.SessionLocal() as db:
        user_id = db.scalar(select(User.id))
        for i in range(205):
            db.add(ApiKey(id=f"history-{i:03}", user_id=user_id, name=f"History {i}",
                          status="revoked", secret_digest="never-serialize-this-digest", last_four="test"))
        db.add(User(id="history-other-user", email="history-other@example.invalid", password_hash="inert"))
        db.flush()
        db.add(ApiKey(id="another-user-key", user_id="history-other-user", name="Other account",
                      secret_digest="inert-other-digest", last_four="test"))
        db.commit()
    path = f"/ops/accounts/{user_id}"
    headers = operator("accounts:read")
    first = client.get(path + "?key_limit=100&key_offset=0", headers=headers).json()
    second = client.get(path + "?key_limit=100&key_offset=100", headers=headers).json()
    last = client.get(path + "?key_limit=100&key_offset=200", headers=headers).json()
    assert first["keys_pagination"] == {"limit": 100, "offset": 0, "total": 206}
    assert len(first["keys"]) == len(second["keys"]) == 100 and len(last["keys"]) == 6
    assert len({k["id"] for page in (first, second, last) for k in page["keys"]}) == 206
    assert first["keys_truncated"] and not last["keys_truncated"]
    filtered = client.get(path + "?key_id=history-000", headers=headers)
    assert [k["id"] for k in filtered.json()["keys"]] == ["history-000"]
    assert "never-serialize" not in filtered.text and "secret_digest" not in filtered.text
    assert client.get(path + "?key_id=another-user-key", headers=headers).json()["keys"] == []
    assert client.get(path + "?key_limit=20", headers=auth).status_code == 401
    assert client.get(path + "?key_limit=20", headers=operator("console:read")).status_code == 401
    assert client.get("/ops/accounts/missing?key_id=history-000", headers=headers).status_code == 404


@pytest.mark.parametrize("query", ["key_limit=0", "key_limit=201", "key_offset=-1", "key_offset=1000001", "key_id=", "key_id=gw_example.not-an-id"])
def test_account_key_pagination_rejects_invalid_bounds(managed, query):
    client, *_ = managed
    assert client.get("/ops/accounts/missing?" + query, headers=operator("accounts:read")).status_code == 422


def test_customer_credentials_cannot_read_operator_data(managed):
    client, auth, *_ = managed
    for path in ("/ops/session", "/ops/accounts", "/ops/orders", "/ops/audit", "/ops/requests/missing"):
        assert client.get(path, headers=auth).status_code == 401


def test_console_identity_is_scoped_expiring_and_does_not_echo_token(managed):
    client, *_ = managed
    headers = operator("console:read")
    result = client.get("/ops/session", headers=headers)
    assert result.status_code == 200
    assert result.json()["scopes"] == ["console:read"]
    assert result.json()["subject"] == "inert-operator"
    assert headers["X-Kunlun-Ops-Token"] not in result.text
    assert client.get("/ops/accounts", headers=headers).status_code == 401
    assert "no-store" in result.headers["cache-control"]


def test_operator_queries_are_paginated_and_secret_free(managed):
    client, auth, *_ = managed
    fund(client, auth)
    accounts = client.get("/ops/accounts?limit=1", headers=operator("accounts:read")).json()
    user = accounts["items"][0]
    assert user["email"] == "managed@example.com"
    detail = client.get("/ops/accounts/" + user["id"], headers=operator("accounts:read")).json()
    assert detail["wallet"]["balance_microusd"] == 100000
    assert len(detail["keys"]) == 1
    orders = client.get("/ops/orders?limit=1", headers=operator("payments:read")).json()
    assert orders["items"][0]["credit_amount_microusd"] == 100000
    order = client.get("/ops/orders/" + orders["items"][0]["id"], headers=operator("payments:read")).json()
    assert order["refunds"] == []
    for value in (accounts, detail, orders, order):
        assert not any(secret in str(value) for secret in ("password_hash", "secret_digest", "checkout_url", "vault_ref", "token_digest"))
    assert client.get("/ops/accounts?limit=201", headers=operator("accounts:read")).status_code == 422
    assert client.get("/ops/accounts?offset=-1", headers=operator("accounts:read")).status_code == 422


def test_key_freeze_unfreeze_is_audited_and_cannot_restore_revoked_key(managed):
    client, auth, key, *_ = managed
    headers = operator("accounts:read", "accounts:write", "audit:read")
    user = client.get("/ops/accounts", headers=headers).json()["items"][0]
    key_id = client.get("/ops/accounts/" + user["id"], headers=headers).json()["keys"][0]["id"]
    path = "/ops/keys/" + key_id + "/status"
    command = {"action": "freeze", "expected_status": "active", "reason": "isolated test suspected abuse"}
    assert client.post(path, headers=operator("accounts:read"), json=command).status_code == 401
    assert client.post(path, headers=headers, json=command).status_code == 200
    assert client.get("/v1/models", headers={"Authorization": "Bearer " + key}).status_code == 401
    assert client.post(path, headers=headers, json=command).status_code == 409
    command.update(action="unfreeze", expected_status="frozen")
    assert client.post(path, headers=headers, json=command).status_code == 200
    assert client.get("/v1/models", headers={"Authorization": "Bearer " + key}).status_code == 200
    assert client.post("/v1/keys/revoke", headers=auth, json={"key_id": key_id}).status_code == 204
    assert client.post(path, headers=headers, json=command).status_code == 409
    actions = client.get("/ops/audit?target_id=" + key_id, headers=headers).json()["items"]
    assert len(actions) == 2
    assert {item["action"] for item in actions} == {"key_freeze", "key_unfreeze"}
    assert all(item["actor"] == "inert-operator" for item in actions)


def test_account_freeze_revokes_already_frozen_keys(managed):
    client, *_ = managed
    headers = operator("accounts:read", "accounts:write")
    user_id = client.get("/ops/accounts", headers=headers).json()["items"][0]["id"]
    with client.app.state.SessionLocal() as db:
        db.scalar(select(ApiKey)).status = "frozen"
        db.commit()
    assert client.post("/ops/accounts/" + user_id + "/status", headers=headers, json={
        "action": "freeze", "reason": "confirmed isolated account risk",
    }).status_code == 200
    with client.app.state.SessionLocal() as db:
        assert db.scalar(select(ApiKey)).status == "revoked"


def test_request_detail_links_final_attempt_without_content(managed):
    client, headers, payload = ready_call(managed)
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 200
    from app.models import ModelRequest
    with client.app.state.SessionLocal() as db:
        request_id = db.scalar(select(ModelRequest.id))
    result = client.get("/ops/requests/" + request_id, headers=operator("reconciliation:read"))
    assert result.status_code == 200
    assert result.json()["request"]["charged_microusd"] == 6
    assert len(result.json()["attempts"]) == 1
    assert "messages" not in result.text and "hello" not in result.text


def test_frozen_keys_count_toward_quota_and_owner_can_revoke(managed):
    client, auth, *_ = managed
    client.app.state.settings.max_active_api_keys = 1
    with client.app.state.SessionLocal() as db:
        key = db.scalar(select(ApiKey))
        key.status = "frozen"
        key_id = key.id
        db.commit()
    assert client.post("/v1/keys", headers=auth, json={"name": "over-quota"}).status_code == 409
    assert client.post("/v1/keys/revoke", headers=auth, json={"key_id": key_id}).status_code == 204
    assert client.post("/v1/keys", headers=auth, json={"name": "replacement"}).status_code == 201


def test_password_reset_permanently_revokes_frozen_keys(managed):
    client, auth, *_ = managed
    with client.app.state.SessionLocal() as db:
        key = db.scalar(select(ApiKey)); key.status = "frozen"; key_id = key.id; db.commit()
    assert client.post("/auth/forgot-password", json={"email": "managed@example.com"}).status_code == 202
    raw = client.app.state.identity_sender.messages[-1].token
    assert client.post("/auth/reset-password", json={"token": raw, "new_password": "changed synthetic password only"}).status_code == 200
    with client.app.state.SessionLocal() as db:
        assert db.get(ApiKey, key_id).status == "revoked"
    assert client.post("/ops/keys/" + key_id + "/status", headers=operator("accounts:write"), json={
        "action": "unfreeze", "expected_status": "frozen", "reason": "must not restore a reset credential",
    }).status_code == 409


def test_stale_account_command_cannot_override_current_state(managed):
    client, *_ = managed
    user_id = client.get("/ops/accounts", headers=operator("accounts:read")).json()["items"][0]["id"]
    result = client.post("/ops/accounts/" + user_id + "/status", headers=operator("accounts:write"), json={
        "action": "freeze", "expected_status": "frozen", "reason": "stale view must not modify account",
    })
    assert result.status_code == 409
    with client.app.state.SessionLocal() as db:
        assert db.scalar(select(OperatorAction.id)) is None


def test_operator_shell_has_strict_csp_and_no_credential_persistence(managed):
    from pathlib import Path
    client, *_ = managed
    response = client.get("/ops/console")
    assert response.status_code == 200
    assert "form-action 'none'" in response.headers["content-security-policy"]
    assert "challenges.cloudflare.com" not in response.headers["content-security-policy"]
    assert 'name="operator-token"' not in response.text
    for filename in ("ops.js", "ops-client.js"):
        source = (Path(__file__).parents[1] / "app" / "static" / filename).read_text()
        assert not any(unsafe in source for unsafe in ("localStorage", "sessionStorage", "innerHTML", "document.cookie", "console.log"))
