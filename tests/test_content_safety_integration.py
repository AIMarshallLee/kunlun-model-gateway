from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import create_app
from app.models import ModelRequest, SafetyAudit
from app.services.content_safety import ContentSafetyError, SafetyDecision


class FakeSafety:
    def __init__(self, *, block_phase: str | None = None, fail_phase: str | None = None):
        self.block_phase = block_phase
        self.fail_phase = fail_phase
        self.calls: list[tuple[str, object]] = []

    async def check(self, *, kind: str, model: str, content):
        self.calls.append((kind, content))
        if kind == self.fail_phase:
            raise ContentSafetyError("内容安全服务不可用，已拒绝本次请求")
        return SafetyDecision(
            allowed=kind != self.block_phase,
            reason_code="policy_disallowed" if kind == self.block_phase else "ok",
            decision_id=f"decision-{kind}",
        )


def _funded(tmp_path, safety: FakeSafety, provider=None):
    secret = "test-webhook-secret"
    provider = provider or AsyncMock(return_value={
        "id": "ok",
        "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "safe answer"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    })
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'safety.sqlite3'}",
        payment_webhook_secret=secret,
        enable_test_payments=True,
        public_signup=True,
        content_safety_required=True,
        content_safety_adapter=safety,
        provider_clients=[provider],
    )
    client = TestClient(app)
    client.__enter__()
    payload = {"email": "safe@example.com", "password": "correct horse battery staple"}
    assert client.post("/auth/register", json=payload).status_code == 201
    session_token = client.post("/auth/login", json=payload).json()["access_token"]
    session_headers = {"Authorization": f"Bearer {session_token}"}
    api_key = client.post("/v1/keys", headers=session_headers, json={"name": "safety"}).json()["key"]
    order = client.post("/billing/topups", headers=session_headers, json={"amount": 100_000}).json()
    body = json.dumps({
        "id": "evt_safety",
        "order_id": order["id"],
        "type": "topup.succeeded",
        "amount": 100_000,
    }).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert client.post("/billing/webhook", content=body, headers={"X-Webhook-Signature": signature}).status_code == 200
    return app, client, {"Authorization": f"Bearer {api_key}"}, provider


def test_blocked_input_never_reaches_provider_or_creates_billable_request(tmp_path):
    safety = FakeSafety(block_phase="input")
    app, client, headers, provider = _funded(tmp_path, safety)
    try:
        response = client.post("/v1/chat/completions", headers=headers, json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "blocked"}],
        })
        assert response.status_code == 403
        assert provider.await_count == 0
        with app.state.SessionLocal() as session:
            assert session.scalar(select(ModelRequest)) is None
            audit = session.scalar(select(SafetyAudit))
            assert audit is not None and audit.phase == "input" and audit.outcome == "blocked"
    finally:
        client.__exit__(None, None, None)


def test_blocked_tool_description_never_reaches_provider_or_reservation(tmp_path):
    safety = FakeSafety(block_phase="input")
    app, client, headers, provider = _funded(tmp_path, safety)
    try:
        response = client.post("/v1/chat/completions", headers=headers, json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "safe visible message"}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "blocked hidden tool instruction",
                    "parameters": {"type": "object"},
                },
            }],
            "tool_choice": "auto",
            "response_format": {"type": "json_object"},
        })
        assert response.status_code == 403
        assert provider.await_count == 0
        checked = safety.calls[0][1]
        assert checked["tools"][0]["function"]["description"] == "blocked hidden tool instruction"
        assert checked["tool_choice"] == "auto"
        assert checked["response_format"] == {"type": "json_object"}
        with app.state.SessionLocal() as session:
            assert session.scalar(select(ModelRequest)) is None
    finally:
        client.__exit__(None, None, None)


def test_blocked_output_is_settled_but_not_returned(tmp_path):
    safety = FakeSafety(block_phase="output")
    app, client, headers, provider = _funded(tmp_path, safety)
    try:
        response = client.post("/v1/chat/completions", headers=headers, json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert response.status_code == 403
        assert "safe answer" not in response.text
        assert provider.await_count == 1
        with app.state.SessionLocal() as session:
            request = session.scalar(select(ModelRequest))
            assert request is not None and request.status == "settled"
            audits = session.scalars(select(SafetyAudit).order_by(SafetyAudit.created_at)).all()
            assert [item.phase for item in audits] == ["input", "output"]
            assert audits[-1].outcome == "blocked"
    finally:
        client.__exit__(None, None, None)


def test_safety_enabled_streaming_is_buffered_then_synthesized(tmp_path):
    seen = {}

    async def provider(payload):
        seen.update(payload)
        return {
            "id": "buffered",
            "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "checked"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }

    safety = FakeSafety()
    app, client, headers, _ = _funded(tmp_path, safety, AsyncMock(side_effect=provider))
    try:
        response = client.post("/v1/chat/completions", headers=headers, json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        })
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert seen["stream"] is False
        assert response.text.endswith("data: [DONE]\n\n")
        assert [phase for phase, _ in safety.calls] == ["input", "output"]
    finally:
        client.__exit__(None, None, None)


def test_safety_failure_is_fail_closed_before_provider(tmp_path):
    safety = FakeSafety(fail_phase="input")
    app, client, headers, provider = _funded(tmp_path, safety)
    try:
        response = client.post("/v1/chat/completions", headers=headers, json={
            "model": "test-model", "messages": [{"role": "user", "content": "private"}],
        })
        assert response.status_code == 503
        assert "private" not in response.text
        assert provider.await_count == 0
    finally:
        client.__exit__(None, None, None)
