from datetime import timedelta
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import create_app
from app.models import OperatorAction, PasswordResetToken, User
from app.security import utcnow
from app.services.ops_tokens import mint_operator_token


def test_uncertain_duplicate_retains_reserve_and_never_calls_again(client, funded_api_key, monkeypatch):
    from app import providers
    from app.models import ModelRequest, ProviderAttempt, Wallet
    from gateway import ProviderError
    calls = 0

    async def uncertain(_payload):
        nonlocal calls
        calls += 1
        raise ProviderError(502, category="upstream_timeout", safe_to_failover=False, request_may_be_billable=True)

    monkeypatch.setattr(providers, "ordered_clients", [uncertain])
    headers = {"Authorization": f"Bearer {funded_api_key}", "Idempotency-Key": "uncertain-recovery"}
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "do not retain"}]}
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 502
    repeated = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert repeated.status_code == 409
    metadata = repeated.json()["request"]
    assert metadata["next_action"] == "contact_operator_for_reconciliation"
    assert metadata["status"] == "pending_reconciliation"
    assert calls == 1
    with client.app.state.SessionLocal() as db:
        assert len(db.scalars(select(ProviderAttempt)).all()) == 1
        assert db.scalar(select(Wallet)).reserved_microusd == db.scalar(select(ModelRequest)).reserved_microusd > 0


def test_duplicate_request_exposes_owned_status_without_second_call(client, funded_api_key, auth_headers):
    headers = {"Authorization": f"Bearer {funded_api_key}", "Idempotency-Key": "recovery-task"}
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "PRIVATE BODY CANARY"}]}
    completed = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert completed.status_code == 200
    duplicate = client.post("/v1/chat/completions", headers=headers, json=payload)
    assert duplicate.status_code == 409
    data = duplicate.json()
    assert data["error"]["code"] == "request_already_recorded"
    request_id = data["request"]["request_id"]
    assert data["request"]["status"] == "settled"
    assert data["request"]["response_retained"] is False
    assert data["request"]["automatic_resubmit_allowed"] is False
    found = client.post("/v1/requests/lookup", headers=headers)
    assert found.status_code == 200
    assert found.json()["request_id"] == request_id
    assert len(found.json()["attempts"]) == 1
    assert "PRIVATE BODY CANARY" not in found.text
    assert client.get(f"/requests/{request_id}", headers=auth_headers).status_code == 200
    assert client.get(f"/v1/requests/{request_id}").status_code == 401
    assert client.post("/auth/register", json={"email": "outsider@example.com", "password": "outsider password long"}).status_code == 201
    login = client.post("/auth/login", json={"email": "outsider@example.com", "password": "outsider password long"})
    other = {"Authorization": "Bearer " + login.json()["access_token"]}
    assert client.get(f"/requests/{request_id}", headers=other).status_code == 404
    key = client.post("/v1/keys", headers=other, json={"name": "other"}).json()["key"]
    other_api = {"Authorization": "Bearer " + key, "Idempotency-Key": "recovery-task"}
    assert client.post("/v1/requests/lookup", headers=other_api).status_code == 404
    assert client.get(f"/v1/requests/{request_id}", headers=other_api).status_code == 404


def test_private_invitation_activation_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("KUNLUN_IDENTITY_TOKEN_PEPPER", "i" * 40)
    secret = "o" * 40
    app = create_app(database_url=f"sqlite:///{tmp_path / 'delivery.db'}",
                     public_signup=False, operator_signing_secret=secret,
                     rate_limit_per_minute=100, provider_clients=[AsyncMock()])
    token = mint_operator_token(secret, subject="delivery-operator", scopes={"accounts:invite"})
    headers = {"X-Kunlun-Ops-Token": token}
    payload = {"email": "customer@example.com", "operation_id": "onboard-001",
               "identity_confirmed": True, "reason": "verified customer onboarding"}
    with TestClient(app) as client:
        assert client.post("/ops/accounts/invitations", json=payload).status_code in (401, 404)
        denied = mint_operator_token(secret, subject="reader", scopes={"accounts:read"})
        assert client.post("/ops/accounts/invitations", json=payload,
                           headers={"X-Kunlun-Ops-Token": denied}).status_code == 401
        assert client.post("/auth/register", json={"email": payload["email"],
                           "password": "customer password long"}).status_code == 403
        invited = client.post("/ops/accounts/invitations", json=payload, headers=headers)
        assert invited.status_code == 201, invited.text
        assert invited.headers["cache-control"] == "no-store"
        data = invited.json()
        assert client.post("/ops/accounts/invitations", json=payload, headers=headers).status_code == 409
        assert client.post("/ops/accounts/invitations", json={**payload, "email": "another@example.com"}, headers=headers).status_code == 409
        with app.state.SessionLocal() as db:
            assert db.scalar(select(User).where(User.email == "another@example.com")) is None
        assert client.get("/v1/provider-catalog").status_code == 401
        raw = data["activation_path"].split("#token=")[1]
        with app.state.SessionLocal() as db:
            assert db.scalar(select(PasswordResetToken)).token_digest != raw
            assert raw not in db.scalar(select(OperatorAction)).reason
            assert db.scalar(select(User)).email_verified_at is not None
        reset = {"token": raw, "new_password": "customer password long"}
        assert client.post("/auth/reset-password", json=reset).status_code == 200
        assert client.post("/auth/reset-password", json=reset).status_code == 400
        login = client.post("/auth/login", json={"email": payload["email"], "password": reset["new_password"]})
        auth = {"Authorization": "Bearer " + login.json()["access_token"]}
        assert client.post("/v1/keys", headers=auth, json={"name": "OpenCode"}).status_code == 201
        recovery = client.post(f"/ops/accounts/{data['user_id']}/recovery", headers=headers,
                               json={k: v for k, v in {**payload, "operation_id": "recovery-001"}.items() if k != "email"})
        assert recovery.status_code == 201, recovery.text
        with app.state.SessionLocal() as db:
            records = db.scalars(select(PasswordResetToken).where(PasswordResetToken.consumed_at.is_(None))).all()
            assert len(records) == 1
            records[0].expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
        assert client.post("/auth/reset-password", json={
            "token": recovery.json()["activation_path"].split("#token=")[1],
            "new_password": "a different long password",
        }).status_code == 400


def test_invitation_requires_persisted_pepper_and_verified_identity(tmp_path):
    secret = "o" * 40
    app = create_app(database_url=f"sqlite:///{tmp_path / 'guard.db'}", public_signup=False,
                     operator_signing_secret=secret)
    headers = {"X-Kunlun-Ops-Token": mint_operator_token(secret, subject="operator", scopes={"accounts:invite"})}
    payload = {"email": "new@example.com", "operation_id": "new-001",
               "identity_confirmed": True, "reason": "verified identity by operator"}
    with TestClient(app) as client:
        assert client.post("/ops/accounts/invitations", headers=headers,
                           json={**payload, "identity_confirmed": False}).status_code == 422
        assert client.post("/ops/accounts/invitations", headers=headers, json=payload).status_code == 503
