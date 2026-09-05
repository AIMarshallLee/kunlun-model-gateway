from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app import create_app, providers
from app.models import ApiKey, LedgerEntry, LedgerTransaction, ModelRequest, OperatorAction, User
from gateway import ProviderError


def test_public_signup_and_test_payments_default_closed(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'closed.sqlite3'}", provider_clients=[])
    with TestClient(app) as closed:
        assert closed.post("/auth/register", json={
            "email": "closed@example.com", "password": "a sufficiently long password",
        }).status_code == 403


def test_developer_console_has_security_headers_and_no_inline_credentials(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "昆仑模型网关" in page.text
    assert "default-src 'self'" in page.headers["Content-Security-Policy"]
    assert "script-src 'self'; frame-src 'none'" in page.headers["Content-Security-Policy"]
    assert 'id="register-captcha"' in page.text
    assert 'id="forgot-captcha"' in page.text
    assert 'id="resend-captcha"' in page.text
    assert 'id="resend-submit"' in page.text
    assert "验证码票据" not in page.text
    assert "apiKey" not in page.text
    script = client.get("/assets/app.js")
    assert script.status_code == 200
    assert "window.location.hash" in script.text
    assert "window.location.search).get(\"token\")" not in script.text
    assert "window.history.replaceState" in script.text
    consume = "state.identityToken = consumeIdentityFragment();"
    assert script.text.count(consume) == 1
    assert script.text.index(consume) < script.text.index("loadReady()")
    assert "captcha_required && !isIdentityRoute" in script.text
    assert '<script src="/assets/app.js" type="module"></script>' in page.text
    assert script.text.startswith('"use strict";\nimport {createCheckoutFlow, checkoutDestination} from "./checkout.js";\n\n(() => {\nconst state = {')
    assert script.text.rstrip().endswith("})();")
    assert "export " not in script.text
    module = client.get("/assets/checkout.js").text
    for forbidden in ("localStorage", "sessionStorage", "document.cookie", "window.location", "fetch("):
        assert forbidden not in module


def test_turnstile_widget_uses_public_site_key_and_exact_csp(tmp_path, monkeypatch):
    monkeypatch.setenv("KUNLUN_CAPTCHA_PROVIDER", "turnstile")
    monkeypatch.setenv("KUNLUN_CAPTCHA_SITE_KEY", "public-site-key")
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'turnstile.sqlite3'}",
        public_signup=True,
        captcha_required=True,
        captcha_adapter=object(),
        provider_clients=[],
    )
    with TestClient(app) as turnstile_client:
        page = turnstile_client.get("/")
        csp = page.headers["Content-Security-Policy"]
        assert "script-src 'self' https://challenges.cloudflare.com" in csp
        assert "frame-src https://challenges.cloudflare.com" in csp
        ready = turnstile_client.get("/readyz").json()
        assert ready["captcha_provider"] == "turnstile"
        assert ready["captcha_site_key"] == "public-site-key"
        assert "captcha_secret" not in json.dumps(ready)
        script = turnstile_client.get("/assets/app.js").text
        assert 'register: "register"' in script
        assert 'forgot: "password_reset"' in script
        assert 'resend: "resend_verification"' in script
        assert '["/verify-email", "/reset-password"]' in script


def test_api_key_and_password_are_not_stored_plaintext(client, auth_headers, api_key):
    with client.app.state.SessionLocal() as session:
        key_record = session.scalar(select(ApiKey))
        user_record = session.scalar(select(User))
        assert key_record is not None and user_record is not None
        assert api_key not in key_record.secret_digest
        assert key_record.secret_digest not in api_key
        assert "correct horse battery staple" not in user_record.password_hash


def test_streaming_synthesizes_openai_sse_and_settles(client, funded_api_key):
    provider = providers.ordered_clients[0]
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {funded_api_key}"},
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "stream this"}],
            "stream": True,
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "data: [DONE]" in response.text
    assert provider.await_count == 1
    with client.app.state.SessionLocal() as session:
        request = session.scalar(select(ModelRequest))
        assert request is not None and request.status == "settled"


def test_ambiguous_timeout_does_not_fail_over_or_release_hold(
    client, funded_api_key, auth_headers, monkeypatch,
):
    first = AsyncMock(side_effect=ProviderError(
        504,
        category="provider_ambiguous_timeout",
        safe_to_failover=False,
        request_may_be_billable=True,
    ))
    second = AsyncMock(return_value={"id": "must-not-run"})
    monkeypatch.setattr(providers, "ordered_clients", [first, second])
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {funded_api_key}"},
        json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 502
    assert second.await_count == 0
    assert client.get("/billing/balance", headers=auth_headers).json()["reserved"] > 0
    with client.app.state.SessionLocal() as session:
        request = session.scalar(select(ModelRequest))
        assert request is not None and request.status == "pending_reconciliation"


def test_operator_can_release_a_verified_nonbillable_pending_request(
    client, funded_api_key, auth_headers, monkeypatch,
):
    uncertain = AsyncMock(side_effect=ProviderError(
        504,
        category="provider_ambiguous_timeout",
        safe_to_failover=False,
        request_may_be_billable=True,
    ))
    monkeypatch.setattr(providers, "ordered_clients", [uncertain])
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {funded_api_key}"},
        json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    request_id = response.json()["error"]["request_id"]
    ops = {"X-Kunlun-Ops-Token": "test-operator-token-change-me-1234567890"}
    assert client.get("/ops/reconciliation", headers={"X-Kunlun-Ops-Token": "wrong"}).status_code == 401
    queue = client.get("/ops/reconciliation", headers=ops)
    assert queue.status_code == 200 and queue.json()["requests"][0]["request_id"] == request_id
    assert queue.json()["pagination"] == {"limit": 100, "offset": 0, "total": 1}
    next_page = client.get("/ops/reconciliation?limit=1&offset=1", headers=ops)
    assert next_page.status_code == 200
    assert next_page.json()["requests"] == []
    assert next_page.json()["pagination"] == {"limit": 1, "offset": 1, "total": 1}
    released = client.post(
        f"/ops/reconciliation/{request_id}",
        headers=ops,
        json={"action": "release", "reason": "已向供应商账单核验，此请求未产生费用"},
    )
    assert released.status_code == 200
    assert released.json()["status"] == "reconciled_released"
    assert client.get("/billing/balance", headers=auth_headers).json()["reserved"] == 0
    with client.app.state.SessionLocal() as session:
        action = session.scalar(select(OperatorAction))
        assert action is not None and action.action == "release"
        assert (action.target_type, action.target_id) == ("model_request", request_id)


def test_operator_settlement_requires_verified_usage_and_upstream_cost(client, funded_api_key, monkeypatch):
    uncertain = AsyncMock(side_effect=ProviderError(
        504,
        category="provider_ambiguous_timeout",
        safe_to_failover=False,
        request_may_be_billable=True,
    ))
    monkeypatch.setattr(providers, "ordered_clients", [uncertain])
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {funded_api_key}"},
        json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    request_id = response.json()["error"]["request_id"]
    ops = {"X-Kunlun-Ops-Token": "test-operator-token-change-me-1234567890"}
    assert client.post(
        f"/ops/reconciliation/{request_id}",
        headers=ops,
        json={"action": "settle", "reason": "供应商账单已有记录，需要据实结算"},
    ).status_code == 422
    settled = client.post(
        f"/ops/reconciliation/{request_id}",
        headers=ops,
        json={
            "action": "settle",
            "reason": "已通过供应商请求编号与日账单完成核验",
            "input_tokens": 7,
            "output_tokens": 3,
            "upstream_cost_microusd": 12,
        },
    )
    assert settled.status_code == 200
    assert settled.json()["status"] == "settled"
    assert settled.json()["upstream_cost"] == 12


def test_idempotency_key_prevents_duplicate_model_charge(client, funded_api_key):
    headers = {
        "Authorization": f"Bearer {funded_api_key}",
        "Idempotency-Key": "same-business-request",
    }
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]}
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 200
    assert client.post("/v1/chat/completions", headers=headers, json=payload).status_code == 409
    with client.app.state.SessionLocal() as session:
        assert len(session.scalars(select(ModelRequest)).all()) == 1


@pytest.mark.parametrize("bad_key", ["contains space", "\u0000control", "x" * 121])
def test_model_idempotency_key_is_rejected_before_provider_or_reservation(
    client, funded_api_key, bad_key,
):
    provider = providers.ordered_clients[0]
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {funded_api_key}", "Idempotency-Key": bad_key},
        json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_idempotency_key"
    assert provider.await_count == 0
    with client.app.state.SessionLocal() as session:
        assert session.scalar(select(ModelRequest)) is None


def test_prompt_is_absent_from_database_dump(client, funded_api_key):
    canary = "DB-CANARY-PROMPT-5d9e00"
    assert client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {funded_api_key}"},
        json={"model": "test-model", "messages": [{"role": "user", "content": canary}]},
    ).status_code == 200
    raw = client.app.state.engine.raw_connection()
    try:
        dump = "\n".join(raw.driver_connection.iterdump())
    finally:
        raw.close()
    assert canary not in dump


def test_each_ledger_transaction_balances_to_zero(client, auth_headers, webhook_secret):
    order = client.post("/billing/topups", headers=auth_headers, json={"amount": 5000}).json()
    body = json.dumps({
        "id": "evt_balanced",
        "order_id": order["id"],
        "type": "topup.succeeded",
        "amount": 5000,
    }).encode()
    signature = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    assert client.post("/billing/webhook", content=body, headers={
        "Content-Type": "application/json", "X-Webhook-Signature": signature,
    }).status_code == 200
    with client.app.state.SessionLocal() as session:
        transactions = session.scalars(select(LedgerTransaction)).all()
        for transaction in transactions:
            entries = session.scalars(select(LedgerEntry).where(
                LedgerEntry.transaction_id == transaction.id,
            )).all()
            assert sum(entry.amount_microusd for entry in entries) == 0


def test_sqlite_ledger_rows_are_append_only(client, auth_headers, webhook_secret):
    order = client.post("/billing/topups", headers=auth_headers, json={"amount": 5000}).json()
    body = json.dumps({
        "id": "evt_append_only",
        "order_id": order["id"],
        "type": "topup.succeeded",
        "amount": 5000,
    }).encode()
    signature = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    assert client.post("/billing/webhook", content=body, headers={
        "Content-Type": "application/json", "X-Webhook-Signature": signature,
    }).status_code == 200

    with client.app.state.engine.begin() as connection:
        entry_id = connection.execute(text("SELECT id FROM ledger_entries LIMIT 1")).scalar_one()
    with pytest.raises(DBAPIError, match="append-only"):
        with client.app.state.engine.begin() as connection:
            connection.execute(
                text("UPDATE ledger_entries SET amount_microusd = 1 WHERE id = :id"),
                {"id": entry_id},
            )
