from __future__ import annotations

from fastapi.testclient import TestClient

from app import create_app


def test_register_login_and_duplicate_email_are_safe(client):
    payload = {"email": "a@example.com", "password": "a sufficiently long password"}
    assert client.post("/auth/register", json=payload).status_code == 201
    assert client.post("/auth/register", json=payload).status_code == 409

    login = client.post("/auth/login", json=payload)
    assert login.status_code == 200
    assert login.json()["token_type"] == "bearer"
    assert client.post("/auth/login", json={**payload, "password": "wrong"}).status_code == 401


def test_api_key_is_returned_once_and_stored_hashed(client, auth_headers):
    created = client.post("/v1/keys", headers=auth_headers, json={"name": "cli"})
    assert created.status_code == 201
    assert created.json()["key"].startswith("gw_")
    assert "hash" not in created.json()["key"]

    listed = client.get("/v1/keys", headers=auth_headers)
    assert listed.status_code == 200
    assert listed.json()["keys"][0]["name"] == "cli"
    assert "secret" not in listed.json()["keys"][0]


def test_revoked_api_key_cannot_call_model_endpoint(client, api_key, auth_headers):
    assert client.post("/v1/keys/revoke", headers=auth_headers, json={"key": api_key}).status_code == 204
    response = client.get("/v1/models", headers={"Authorization": f"Bearer {api_key}"})
    assert response.status_code == 401


def test_login_has_ip_and_account_rate_limits(tmp_path):
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'auth-rate.sqlite3'}",
        public_signup=True,
        rate_limit_per_minute=2,
        provider_clients=[],
    )
    payload = {"email": "limited@example.com", "password": "a sufficiently long password"}
    with TestClient(app) as limited:
        assert limited.post("/auth/register", json=payload).status_code == 201
        assert limited.post("/auth/login", json=payload).status_code == 200
        assert limited.post("/auth/login", json={**payload, "password": "wrong"}).status_code == 401
        blocked = limited.post("/auth/login", json={**payload, "password": "wrong-again"})
        assert blocked.status_code == 429
        assert blocked.headers["Retry-After"] == "60"


def test_logout_all_revokes_every_session(client, account):
    payload = {"email": "owner@example.com", "password": "correct horse battery staple"}
    first = client.post("/auth/login", json=payload).json()["access_token"]
    second = client.post("/auth/login", json=payload).json()["access_token"]

    response = client.post(
        "/auth/logout-all",
        headers={"Authorization": f"Bearer {first}"},
    )
    assert response.status_code == 204
    assert client.get("/v1/keys", headers={"Authorization": f"Bearer {first}"}).status_code == 401
    assert client.get("/v1/keys", headers={"Authorization": f"Bearer {second}"}).status_code == 401
