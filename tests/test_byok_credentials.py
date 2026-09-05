from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.models import ApiKey, Budget, CredentialActionAudit, LedgerEntry, ModelRequest, ProviderAttempt, ProviderConnection, User, Wallet
from app.services.credentials import DisabledCredentialVault, InMemoryCredentialVault, SecretUnavailable
from app.services.gateway_billing import BillingError, mark_pending_reconciliation, record_attempt, reserve_byok_model_request, settle_model_request
from app.services.ops_tokens import mint_operator_token
from app.providers import OpenAICompatibleProvider


class FailsOnceOnDestroy(InMemoryCredentialVault):
    def __init__(self):
        super().__init__()
        self.destroy_calls = 0

    def destroy(self, **kwargs):  # type: ignore[no-untyped-def]
        self.destroy_calls += 1
        if self.destroy_calls == 1:
            raise SecretUnavailable("simulated Vault outage")
        return super().destroy(**kwargs)


class BindingCheckingVault(InMemoryCredentialVault):
    """Test seam for versioned private bindings without a public reference."""

    def __init__(self):
        super().__init__()
        self.observed_bindings: list[int] = []
        self.fail_next_put = False

    def put(self, credential_version=None, **kwargs):  # type: ignore[no-untyped-def]
        self.observed_bindings.append(credential_version)
        if self.fail_next_put:
            self.fail_next_put = False
            raise SecretUnavailable("simulated Vault outage")
        return super().put(
            credential_version=credential_version,
            **kwargs,
        )


def _login(client: TestClient, email: str) -> dict[str, str]:
    assert client.post("/auth/register", json={
        "email": email, "password": "correct horse battery staple",
    }).status_code == 201
    response = client.post("/auth/login", json={
        "email": email, "password": "correct horse battery staple",
    })
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_disabled_vault_fails_closed_and_never_returns_a_secret():
    vault = DisabledCredentialVault()
    with pytest.raises(SecretUnavailable):
        vault.put(user_id="user", connection_id="connection", provider="openai", credential_version=1, secret="not-a-real-secret")
    with pytest.raises(SecretUnavailable):
        vault.get(user_id="user", connection_id="connection", provider="openai", credential_version=1)


def test_provider_debug_representation_never_contains_the_api_key():
    secret = "sk-debug-output-must-stay-redacted"
    provider = OpenAICompatibleProvider(
        provider_name="openai",
        base_url="https://api.openai.example/v1",
        api_key=secret,
    )

    assert secret not in repr(provider)


def test_session_only_connection_crud_is_tenant_scoped_and_secret_free(tmp_path):
    vault = InMemoryCredentialVault()
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'byok.sqlite'}",
        credential_vault=vault,
        public_signup=True,
    )
    with TestClient(app) as client:
        owner = _login(client, "owner@example.com")
        other = _login(client, "other@example.com")

        anonymous = client.get("/v1/provider-connections")
        assert anonymous.status_code == 401
        created = client.put("/v1/provider-connections/openai", headers=owner, json={
            "secret": "sk-owner-secret", "label": "Editorial account",
        })
        assert created.status_code == 201
        body = created.json()
        rendered = json.dumps(body)
        assert "sk-owner-secret" not in rendered
        assert "vault_ref" not in rendered
        assert body["provider"] == "openai"
        assert body["credential_version"] == 1

        assert client.get("/v1/provider-connections", headers=other).json() == {"data": []}
        assert client.delete("/v1/provider-connections/openai", headers=other).status_code == 404
        assert client.delete("/v1/provider-connections/openai", headers=owner).status_code == 204

    with app.state.SessionLocal() as session:
        connection = session.scalar(select(ProviderConnection))
        assert connection is not None and connection.status == "revoked"
        actions = session.scalars(select(CredentialActionAudit).order_by(CredentialActionAudit.created_at)).all()
        assert [action.action for action in actions] == ["created", "revoked"]


def test_connection_rotation_never_stores_or_echoes_secret(tmp_path):
    vault = InMemoryCredentialVault()
    app = create_app(database_url=f"sqlite:///{tmp_path / 'rotate.sqlite'}", credential_vault=vault, public_signup=True)
    with TestClient(app) as client:
        headers = _login(client, "owner@example.com")
        assert client.put("/v1/provider-connections/openai", headers=headers, json={"secret": "first-secret"}).status_code == 201
        response = client.put("/v1/provider-connections/openai", headers=headers, json={"secret": "second-secret"})
        assert response.status_code == 200
        assert response.json()["credential_version"] == 2
        assert "second-secret" not in response.text
    assert set(vault.values()) == {"second-secret"}


def test_revoke_failure_returns_pending_and_retry_finishes(tmp_path):
    vault = FailsOnceOnDestroy()
    app = create_app(database_url=f"sqlite:///{tmp_path / 'revoke-retry.sqlite'}", credential_vault=vault, public_signup=True)
    with TestClient(app) as client:
        headers = _login(client, "retry@example.com")
        assert client.put("/v1/provider-connections/openai", headers=headers, json={"secret": "retry-secret"}).status_code == 201
        pending = client.delete("/v1/provider-connections/openai", headers=headers)
        assert pending.status_code == 202
        assert pending.json() == {"status": "revoked_pending_destroy"}
        assert client.delete("/v1/provider-connections/openai", headers=headers).status_code == 204
    with app.state.SessionLocal() as session:
        item = session.scalar(select(ProviderConnection))
        assert item is not None and item.status == "revoked"
    assert vault.destroy_calls == 2


def test_revoked_connection_can_reconnect_with_next_version(tmp_path):
    vault = BindingCheckingVault()
    app = create_app(database_url=f"sqlite:///{tmp_path / 'reconnect.sqlite'}", credential_vault=vault, public_signup=True)
    with TestClient(app) as client:
        headers = _login(client, "reconnect@example.com")
        assert client.put("/v1/provider-connections/openai", headers=headers, json={"secret": "first-secret"}).status_code == 201
        with app.state.SessionLocal() as session:
            first_connection = session.scalar(select(ProviderConnection))
            assert first_connection is not None
            first_user_id = first_connection.user_id
            first_connection_id = first_connection.id
        assert client.delete("/v1/provider-connections/openai", headers=headers).status_code == 204
        response = client.put("/v1/provider-connections/openai", headers=headers, json={"secret": "second-secret"})
        assert response.status_code == 200 and response.json()["credential_version"] == 2
    with app.state.SessionLocal() as session:
        connection = session.scalar(select(ProviderConnection))
        assert connection is not None
        assert connection.status == "active" and connection.credential_version == 2
        actions = session.scalars(select(CredentialActionAudit).order_by(CredentialActionAudit.created_at)).all()
        assert [item.action for item in actions] == ["created", "revoked", "reconnected"]
    assert vault.observed_bindings == [1, 2]
    with pytest.raises(SecretUnavailable):
        vault.get(user_id=first_user_id, connection_id=first_connection_id, provider="openai", credential_version=1)
    assert set(vault.values()) == {"second-secret"}


def test_revoked_reconnect_vault_failure_rolls_back_metadata_without_echoing_secret(tmp_path):
    vault = BindingCheckingVault()
    app = create_app(database_url=f"sqlite:///{tmp_path / 'reconnect-failure.sqlite'}", credential_vault=vault, public_signup=True)
    reconnect_secret = "reconnect-secret-must-not-leak"
    with TestClient(app) as client:
        headers = _login(client, "reconnect-failure@example.com")
        assert client.put("/v1/provider-connections/openai", headers=headers, json={"secret": "first-secret"}).status_code == 201
        assert client.delete("/v1/provider-connections/openai", headers=headers).status_code == 204
        vault.fail_next_put = True
        failed = client.put("/v1/provider-connections/openai", headers=headers, json={"secret": reconnect_secret})

    assert failed.status_code == 503
    assert reconnect_secret not in failed.text
    with app.state.SessionLocal() as session:
        connection = session.scalar(select(ProviderConnection))
        assert connection is not None
        assert connection.status == "revoked"
        assert connection.credential_version == 1
    assert vault.observed_bindings[-1] == 2
    assert list(vault.values()) == []


def test_provider_spend_cap_does_not_require_or_mutate_wallet(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'budget.sqlite'}", public_signup=True)
    with TestClient(app) as client:
        headers = _login(client, "owner@example.com")
        response = client.post("/budgets", headers=headers, json={
            "amount": 50_000, "kind": "provider_spend_cap",
        })
        assert response.status_code == 201
        assert response.json()["kind"] == "provider_spend_cap"
    with app.state.SessionLocal() as session:
        assert session.scalar(select(Wallet)) is not None
        wallet = session.scalar(select(Wallet))
        assert wallet.balance_microusd == 0


def test_byok_reservation_and_settlement_only_use_provider_spend_cap(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'byok-charge.sqlite'}", public_signup=True)
    with TestClient(app) as client:
        headers = _login(client, "owner@example.com")
        api_key = client.post("/v1/keys", headers=headers, json={"name": "byok"}).json()["key"]
        assert client.post("/budgets", headers=headers, json={
            "amount": 100_000, "kind": "provider_spend_cap",
        }).status_code == 201
    with app.state.SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == "owner@example.com"))
        key = session.scalar(select(ApiKey).where(ApiKey.user_id == user.id))
        wallet = session.scalar(select(Wallet).where(Wallet.user_id == user.id))
        reservation = reserve_byok_model_request(
            session, user_id=user.id, api_key_id=key.id, model="test-model",
            billable_payload={"messages": [{"role": "user", "content": "hello"}]},
            max_output_tokens=32, idempotency_key="byok-settlement",
        )
        assert wallet.balance_microusd == 0 and wallet.reserved_microusd == 0
        charged, _ = settle_model_request(
            session, request_id=reservation.request_id,
            response={"model": "test-model", "choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}},
            provider="openai", fallback_count=0,
        )
        assert charged == 0
        request = session.get(ModelRequest, reservation.request_id)
        budget = session.scalar(select(Budget).where(Budget.user_id == user.id))
        assert request.billing_mode == "byok" and request.charged_microusd == 0
        assert budget.spent_microusd == request.upstream_cost_microusd > 0
        assert session.scalar(select(LedgerEntry).where(LedgerEntry.user_id == user.id)) is None


def test_byok_chat_never_falls_back_to_a_global_provider_key(tmp_path, monkeypatch):
    monkeypatch.setenv("KUNLUN_PROVIDERS_JSON", json.dumps([{
        "name": "openai", "base_url": "https://api.openai.example/v1", "models": ["test-model"],
    }]))
    monkeypatch.setenv("KUNLUN_PROVIDER_HOST_ALLOWLIST", "api.openai.example")
    legacy_provider = AsyncMock()
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'byok-no-fallback.sqlite'}",
        public_signup=True,
        gateway_mode="byok",
        credential_vault=InMemoryCredentialVault(),
        provider_clients=[legacy_provider],
        environment="test",
    )
    with TestClient(app) as client:
        headers = _login(client, "owner@example.com")
        api_key = client.post("/v1/keys", headers=headers, json={"name": "byok"}).json()["key"]
        response = client.post("/v1/chat/completions", headers={
            "Authorization": f"Bearer {api_key}",
        }, json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "byok_credential_unavailable"
    legacy_provider.assert_not_awaited()


def test_production_byok_requires_non_disabled_vault_configuration(monkeypatch):
    monkeypatch.setenv("KUNLUN_ENV", "production")
    monkeypatch.setenv("KUNLUN_GATEWAY_MODE", "byok")
    monkeypatch.delenv("KUNLUN_VAULT_BACKEND", raising=False)
    from app.config import Settings

    with pytest.raises(RuntimeError, match="Vault"):
        Settings.from_env(
            database_url="postgresql+psycopg://user:password@db.example.com/app?sslmode=verify-full&sslrootcert=/definitely/missing.pem",
            api_key_pepper="a" * 32,
            session_pepper="b" * 32,
            identity_token_pepper="c" * 32,
            api_key_pepper_persisted=True,
            session_pepper_persisted=True,
            identity_token_pepper_persisted=True,
        )


def _configure_test_byok(monkeypatch) -> None:
    monkeypatch.setenv("KUNLUN_PROVIDERS_JSON", json.dumps([{
        "name": "openai",
        "base_url": "https://api.openai.example/v1",
        "models": ["test-model"],
        "pricing": {"test-model": {
            "input_microusd_per_million": 1_000_000,
            "output_microusd_per_million": 1_000_000,
        }},
    }]))
    monkeypatch.setenv("KUNLUN_PROVIDER_HOST_ALLOWLIST", "api.openai.example")


def test_byok_success_uses_only_transient_customer_key_and_keeps_wallet_unchanged(tmp_path, monkeypatch):
    _configure_test_byok(monkeypatch)
    seen_authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorizations.append(request.headers["authorization"])
        return httpx.Response(200, json={
            "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        })

    def build_client(_catalog, *, api_key, allowed_hosts):  # type: ignore[no-untyped-def]
        assert allowed_hosts == {"api.openai.example"}
        return OpenAICompatibleProvider(
            provider_name="openai",
            base_url="https://api.openai.example/v1",
            api_key=api_key,
            models={"test-model"},
            pricing={"test-model": {
                "input_microusd_per_million": 1_000_000,
                "output_microusd_per_million": 1_000_000,
            }},
            transport=httpx.MockTransport(handler),
        )

    from app import providers
    monkeypatch.setattr(providers, "build_byok_provider_client", build_client)
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'byok-success.sqlite'}",
        environment="test",
        gateway_mode="byok",
        credential_vault=InMemoryCredentialVault(),
        public_signup=True,
    )
    with TestClient(app) as client:
        headers = _login(client, "byok-success@example.com")
        key = client.post("/v1/keys", headers=headers, json={"name": "byok"}).json()["key"]
        assert client.put("/v1/provider-connections/openai", headers=headers, json={
            "secret": "customer-only-key",
        }).status_code == 201
        assert client.post("/budgets", headers=headers, json={
            "amount": 100_000, "kind": "provider_spend_cap",
        }).status_code == 201
        response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
            "model": "test-model", "max_tokens": 10,
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert response.status_code == 200
        with app.state.SessionLocal() as session:
            request = session.scalar(select(ModelRequest))
            attempt = session.scalar(select(ProviderAttempt))
            budget = session.scalar(select(Budget))
            wallet = session.scalar(select(Wallet))
            assert request is not None and attempt is not None and budget is not None and wallet is not None
            assert wallet.balance_microusd == wallet.reserved_microusd == 0
            assert request.status == "settled"
            assert request.upstream_cost_microusd == budget.spent_microusd == attempt.upstream_cost_microusd
            assert request.final_attempt_id == attempt.id
            assert attempt.billing_status == "settled"
    assert seen_authorizations == ["Bearer customer-only-key"]


def test_byok_rejects_stale_db_price_before_reservation_or_provider_call(tmp_path, monkeypatch):
    _configure_test_byok(monkeypatch)
    upstream_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={"model": "test-model", "choices": [], "usage": {
            "prompt_tokens": 1, "completion_tokens": 1,
        }})

    from app import providers
    monkeypatch.setattr(providers, "build_byok_provider_client", lambda _catalog, *, api_key, allowed_hosts: OpenAICompatibleProvider(
        provider_name="openai", base_url="https://api.openai.example/v1", api_key=api_key,
        models={"test-model"}, transport=httpx.MockTransport(handler),
    ))
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'byok-price-roll.sqlite'}",
        environment="test", gateway_mode="byok", credential_vault=InMemoryCredentialVault(), public_signup=True,
    )
    with TestClient(app) as client:
        headers = _login(client, "price-roll@example.com")
        api_key = client.post("/v1/keys", headers=headers, json={"name": "byok"}).json()["key"]
        assert client.put("/v1/provider-connections/openai", headers=headers, json={"secret": "customer-key"}).status_code == 201
        assert client.post("/budgets", headers=headers, json={"amount": 100_000, "kind": "provider_spend_cap"}).status_code == 201
        # A second process starts with the lowered durable model catalog. The
        # first app intentionally remains alive with its older provider
        # catalog, matching a rolling-deploy overlap.
        monkeypatch.setenv("KUNLUN_MODELS_JSON", json.dumps({"test-model": {
            "input_microusd_per_million": 1,
            "output_microusd_per_million": 1,
            "max_output_tokens": 4096,
        }}))
        newer_app = create_app(
            database_url=f"sqlite:///{tmp_path / 'byok-price-roll.sqlite'}",
            environment="test", gateway_mode="byok", credential_vault=InMemoryCredentialVault(), public_signup=True,
        )
        assert newer_app.state.settings.models["test-model"]["input_microusd_per_million"] == 1
        response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json={
            "model": "test-model", "max_tokens": 10,
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "billing_rejected"
        with app.state.SessionLocal() as session:
            budget = session.scalar(select(Budget))
            assert budget is not None and budget.reserved_microusd == budget.spent_microusd == 0
            assert session.scalar(select(ModelRequest)) is None
            assert session.scalar(select(ProviderAttempt)) is None
    assert upstream_calls == 0


def test_production_byok_requires_idempotency_key_before_provider_or_reservation(tmp_path, monkeypatch):
    _configure_test_byok(monkeypatch)
    upstream_calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={"model": "test-model", "choices": [], "usage": {
            "prompt_tokens": 1, "completion_tokens": 1,
        }})

    from app import providers
    monkeypatch.setattr(providers, "build_byok_provider_client", lambda _catalog, *, api_key, allowed_hosts: OpenAICompatibleProvider(
        provider_name="openai", base_url="https://api.openai.example/v1", api_key=api_key,
        models={"test-model"}, transport=httpx.MockTransport(handler),
    ))
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'byok-production-idempotency.sqlite'}",
        environment="test", gateway_mode="byok", credential_vault=InMemoryCredentialVault(), public_signup=True,
    )
    # The factory rejects test adapters in production. Set this one request
    # gate after test-only construction so the route's production branch can
    # be exercised without pretending this is a production deployment.
    app.state.settings.environment = "production"
    with TestClient(app) as client:
        headers = _login(client, "idempotency-required@example.com")
        api_key = client.post("/v1/keys", headers=headers, json={"name": "byok"}).json()["key"]
        assert client.put("/v1/provider-connections/openai", headers=headers, json={"secret": "customer-key"}).status_code == 201
        assert client.post("/budgets", headers=headers, json={"amount": 100_000, "kind": "provider_spend_cap"}).status_code == 201
        response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json={
            "model": "test-model", "max_tokens": 10,
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert response.status_code == 428
        assert response.json()["error"]["code"] == "idempotency_key_required"
        with app.state.SessionLocal() as session:
            budget = session.scalar(select(Budget))
            assert budget is not None and budget.reserved_microusd == budget.spent_microusd == 0
            assert session.scalar(select(ModelRequest)) is None
            assert session.scalar(select(ProviderAttempt)) is None
    assert upstream_calls == 0


def test_byok_automatic_settlement_never_exceeds_reserved_or_budget(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'byok-hard-cap.sqlite'}", public_signup=True)
    with TestClient(app) as client:
        headers = _login(client, "hard-cap@example.com")
        client.post("/v1/keys", headers=headers, json={"name": "byok"})
        assert client.post("/budgets", headers=headers, json={
            "amount": 100_000, "kind": "provider_spend_cap",
        }).status_code == 201
    with app.state.SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == "hard-cap@example.com"))
        user_id = user.id
        key = session.scalar(select(ApiKey).where(ApiKey.user_id == user.id))
        reservation = reserve_byok_model_request(
            session, user_id=user.id, api_key_id=key.id, model="test-model",
            billable_payload={"messages": [{"role": "user", "content": "hello"}]},
            max_output_tokens=10, idempotency_key="hard-cap",
        )
        with pytest.raises(BillingError, match="预授权上限"):
            settle_model_request(
                session, request_id=reservation.request_id,
                response={"model": "test-model", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
                provider="openai", fallback_count=0,
                upstream_cost_override=reservation.amount + 1,
            )
    with app.state.SessionLocal() as session:
        request = session.get(ModelRequest, reservation.request_id)
        budget = session.scalar(select(Budget).where(Budget.user_id == user_id))
        assert request is not None and budget is not None
        assert request.status == "reserved"
        assert budget.spent_microusd == 0 and budget.reserved_microusd == reservation.amount


def test_stale_session_cannot_release_a_request_already_settled(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'stale-settle.sqlite'}", public_signup=True)
    with TestClient(app) as client:
        headers = _login(client, "stale@example.com")
        client.post("/v1/keys", headers=headers, json={"name": "byok"})
        assert client.post("/budgets", headers=headers, json={
            "amount": 100_000, "kind": "provider_spend_cap",
        }).status_code == 201
    with app.state.SessionLocal() as setup:
        user = setup.scalar(select(User).where(User.email == "stale@example.com"))
        api_key = setup.scalar(select(ApiKey).where(ApiKey.user_id == user.id))
        reservation = reserve_byok_model_request(
            setup, user_id=user.id, api_key_id=api_key.id, model="test-model",
            billable_payload={"messages": [{"role": "user", "content": "hello"}]},
            max_output_tokens=10, idempotency_key="stale-settle",
        )
    stale = app.state.SessionLocal()
    settled = app.state.SessionLocal()
    try:
        assert stale.get(ModelRequest, reservation.request_id).status == "reserved"
        settle_model_request(
            settled, request_id=reservation.request_id,
            response={"model": "test-model", "choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            provider="openai", fallback_count=0,
        )
        # This must refresh the stale identity-map entry before evaluating its
        # status; it is a no-op, not a second reservation release attempt.
        from app.services.gateway_billing import release_model_request
        release_model_request(stale, reservation.request_id, "stale worker")
    finally:
        stale.close()
        settled.close()
    with app.state.SessionLocal() as session:
        request = session.get(ModelRequest, reservation.request_id)
        budget = session.scalar(select(Budget))
        assert request is not None and budget is not None
        assert request.status == "settled"
        assert budget.spent_microusd == request.upstream_cost_microusd
        assert budget.reserved_microusd == 0


def test_byok_operator_reconciliation_finalizes_the_original_uncertain_attempt(tmp_path, monkeypatch):
    _configure_test_byok(monkeypatch)
    secret = "operator-signing-secret-with-at-least-thirty-two-bytes"
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'byok-reconcile.sqlite'}",
        environment="test", gateway_mode="byok", credential_vault=InMemoryCredentialVault(),
        public_signup=True, operator_signing_secret=secret,
    )
    with TestClient(app) as client:
        headers = _login(client, "reconcile@example.com")
        client.post("/v1/keys", headers=headers, json={"name": "byok"}).json()["key"]
        assert client.post("/budgets", headers=headers, json={
            "amount": 100_000, "kind": "provider_spend_cap",
        }).status_code == 201
        with app.state.SessionLocal() as session:
            user = session.scalar(select(User).where(User.email == "reconcile@example.com"))
            api_key = session.scalar(select(ApiKey).where(ApiKey.id.is_not(None), ApiKey.user_id == user.id))
            reservation = reserve_byok_model_request(
                session, user_id=user.id, api_key_id=api_key.id, model="test-model",
                billable_payload={"messages": [{"role": "user", "content": "hello"}]},
                max_output_tokens=10, idempotency_key="operator-settle",
            )
            # Simulate a cap that was lowered after the in-flight request was
            # already authorised. Only audited operator reconciliation may
            # write the verified provider charge in this state.
            budget = session.scalar(select(Budget).where(Budget.user_id == user.id))
            assert budget is not None
            budget.limit_microusd = reservation.amount
            session.commit()
            attempt_id = record_attempt(
                session, request_id=reservation.request_id, ordinal=1, provider="openai", model="test-model", status="sent",
            )
            mark_pending_reconciliation(session, reservation.request_id, "ambiguous", provider="openai", attempt_id=attempt_id)
        token = mint_operator_token(secret, subject="oncall@example.com", scopes={"reconciliation:write"})
        settled = client.post(f"/ops/reconciliation/{reservation.request_id}", headers={"X-Kunlun-Ops-Token": token}, json={
            "action": "settle", "reason": "verified against provider billing record",
            "input_tokens": 2, "output_tokens": 3,
            "upstream_cost_microusd": reservation.amount + 1,
        })
        assert settled.status_code == 200
        with app.state.SessionLocal() as session:
            request = session.get(ModelRequest, reservation.request_id)
            attempt = session.get(ProviderAttempt, attempt_id)
            budget = session.scalar(select(Budget))
            assert request is not None and attempt is not None and budget is not None
            assert request.final_attempt_id == attempt.id
            assert attempt.billing_status == "settled" and attempt.upstream_cost_microusd == request.upstream_cost_microusd
            assert budget.spent_microusd == request.upstream_cost_microusd > budget.limit_microusd
            budget.reserved_microusd = 1
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
            budget.kind = "prepaid_credit"
            budget.reserved_microusd = 0
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()


def test_byok_operator_release_finalizes_the_original_uncertain_attempt(tmp_path, monkeypatch):
    _configure_test_byok(monkeypatch)
    secret = "operator-signing-secret-with-at-least-thirty-two-bytes"
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'byok-release.sqlite'}",
        environment="test", gateway_mode="byok", credential_vault=InMemoryCredentialVault(),
        public_signup=True, operator_signing_secret=secret,
    )
    with TestClient(app) as client:
        headers = _login(client, "release@example.com")
        client.post("/v1/keys", headers=headers, json={"name": "byok"})
        assert client.post("/budgets", headers=headers, json={
            "amount": 100_000, "kind": "provider_spend_cap",
        }).status_code == 201
        with app.state.SessionLocal() as session:
            user = session.scalar(select(User).where(User.email == "release@example.com"))
            api_key = session.scalar(select(ApiKey).where(ApiKey.user_id == user.id))
            reservation = reserve_byok_model_request(
                session, user_id=user.id, api_key_id=api_key.id, model="test-model",
                billable_payload={"messages": [{"role": "user", "content": "hello"}]},
                max_output_tokens=10, idempotency_key="operator-release",
            )
            attempt_id = record_attempt(
                session, request_id=reservation.request_id, ordinal=1, provider="openai", model="test-model", status="sent",
            )
            mark_pending_reconciliation(session, reservation.request_id, "ambiguous", provider="openai", attempt_id=attempt_id)
        token = mint_operator_token(secret, subject="oncall@example.com", scopes={"reconciliation:write"})
        released = client.post(f"/ops/reconciliation/{reservation.request_id}", headers={"X-Kunlun-Ops-Token": token}, json={
            "action": "release", "reason": "verified no provider charge was created",
        })
        assert released.status_code == 200
        with app.state.SessionLocal() as session:
            request = session.get(ModelRequest, reservation.request_id)
            attempt = session.get(ProviderAttempt, attempt_id)
            budget = session.scalar(select(Budget))
            assert request is not None and attempt is not None and budget is not None
            assert request.status == "reconciled_released"
            assert request.final_attempt_id == attempt.id
            assert attempt.billing_status == "not_billed"
            assert budget.reserved_microusd == 0 and budget.spent_microusd == 0


def test_request_validation_never_reflects_provider_key_or_prompt(tmp_path, monkeypatch):
    _configure_test_byok(monkeypatch)
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'validation-redaction.sqlite'}",
        environment="test", gateway_mode="byok", credential_vault=InMemoryCredentialVault(),
        public_signup=True,
    )
    secret = "sk-secret-leak with-space"
    unknown_field = "customer_provider_secret_do_not_echo"
    prompt = "customer-prompt-must-not-appear"
    with TestClient(app) as client:
        headers = _login(client, "validation@example.com")
        bad_key = client.put("/v1/provider-connections/openai", headers=headers, json={
            "secret": secret, unknown_field: secret,
        })
        assert bad_key.status_code == 422
        assert secret not in bad_key.text
        assert unknown_field not in bad_key.text
        assert all("loc" not in error for error in bad_key.json()["detail"])
        api_key = client.post("/v1/keys", headers=headers, json={"name": "byok"}).json()["key"]
        bad_prompt = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json={
            "model": "test-model",
            "messages": [{"role": "user", "content": {"prompt": prompt}}],
        })
        assert bad_prompt.status_code == 422
        assert prompt not in bad_prompt.text
        assert all(
            "input" not in error and "ctx" not in error and "loc" not in error
            for error in bad_prompt.json()["detail"]
        )


@pytest.mark.parametrize("usage", [
    None,
    {"prompt_tokens": True, "completion_tokens": 2},
    {"prompt_tokens": 2, "completion_tokens": -1},
    {"prompt_tokens": "2", "completion_tokens": 3},
])
def test_byok_nonstream_invalid_usage_stays_reserved_for_reconciliation(tmp_path, monkeypatch, usage):
    _configure_test_byok(monkeypatch)

    def handler(_request: httpx.Request) -> httpx.Response:
        result = {
            "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "中文回复"}}],
        }
        if usage is not None:
            result["usage"] = usage
        return httpx.Response(200, json=result)

    from app import providers
    monkeypatch.setattr(providers, "build_byok_provider_client", lambda _catalog, *, api_key, allowed_hosts: OpenAICompatibleProvider(
        provider_name="openai", base_url="https://api.openai.example/v1", api_key=api_key,
        models={"test-model"}, transport=httpx.MockTransport(handler),
    ))
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'byok-invalid-usage.sqlite'}",
        environment="test", gateway_mode="byok", credential_vault=InMemoryCredentialVault(), public_signup=True,
    )
    with TestClient(app) as client:
        headers = _login(client, "invalid-usage@example.com")
        api_key = client.post("/v1/keys", headers=headers, json={"name": "byok"}).json()["key"]
        assert client.put("/v1/provider-connections/openai", headers=headers, json={"secret": "customer-key"}).status_code == 201
        assert client.post("/budgets", headers=headers, json={"amount": 100_000, "kind": "provider_spend_cap"}).status_code == 201
        response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json={
            "model": "test-model", "max_tokens": 10,
            "messages": [{"role": "user", "content": "请调用工具并用中文回答"}],
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        })
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "settlement_pending"
        with app.state.SessionLocal() as session:
            request = session.scalar(select(ModelRequest))
            attempt = session.scalar(select(ProviderAttempt))
            budget = session.scalar(select(Budget))
            assert request is not None and attempt is not None and budget is not None
            assert request.status == request.cost_state == "pending_reconciliation"
            assert attempt.billing_status == "unknown"
            assert budget.spent_microusd == 0 and budget.reserved_microusd == request.reserved_microusd


def test_byok_sse_done_without_usage_stays_reserved_for_reconciliation(tmp_path, monkeypatch):
    _configure_test_byok(monkeypatch)
    stream = (
        b'data: {"choices":[{"delta":{"content":"\xe4\xb8\xad\xe6\x96\x87","tool_calls":[{"function":{"arguments":"{}"}}]}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    from app import providers
    monkeypatch.setattr(providers, "build_byok_provider_client", lambda _catalog, *, api_key, allowed_hosts: OpenAICompatibleProvider(
        provider_name="openai", base_url="https://api.openai.example/v1", api_key=api_key,
        models={"test-model"}, transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, content=stream, headers={"Content-Type": "text/event-stream"},
        )),
    ))
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'byok-sse-no-usage.sqlite'}",
        environment="test", gateway_mode="byok", credential_vault=InMemoryCredentialVault(), public_signup=True,
    )
    with TestClient(app) as client:
        headers = _login(client, "sse-no-usage@example.com")
        api_key = client.post("/v1/keys", headers=headers, json={"name": "byok"}).json()["key"]
        assert client.put("/v1/provider-connections/openai", headers=headers, json={"secret": "customer-key"}).status_code == 201
        assert client.post("/budgets", headers=headers, json={"amount": 100_000, "kind": "provider_spend_cap"}).status_code == 201
        response = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json={
            "model": "test-model", "max_tokens": 10, "stream": True,
            "messages": [{"role": "user", "content": "中文工具调用"}],
        })
        assert response.status_code == 200 and response.text.endswith("data: [DONE]\n\n")
        with app.state.SessionLocal() as session:
            request = session.scalar(select(ModelRequest))
            attempt = session.scalar(select(ProviderAttempt))
            budget = session.scalar(select(Budget))
            assert request is not None and attempt is not None and budget is not None
            assert request.status == "pending_reconciliation"
            assert attempt.billing_status == "unknown"
            assert budget.spent_microusd == 0 and budget.reserved_microusd == request.reserved_microusd
