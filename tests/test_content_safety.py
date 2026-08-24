from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.services.content_safety import ContentSafetyError, HttpContentSafetyAdapter


def test_content_safety_adapter_allows_or_blocks_with_stable_reason_codes():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer moderation-secret"
        payload = json.loads(request.content)
        seen.append(payload)
        allowed = payload["content"] != "blocked content"
        return httpx.Response(200, json={
            "allowed": allowed,
            "reason_code": "ok" if allowed else "policy_disallowed",
            "decision_id": "decision-123",
        })

    adapter = HttpContentSafetyAdapter(
        endpoint="https://safety.example/v1/check",
        api_key="moderation-secret",
        allowed_hosts={"safety.example"},
        transport=httpx.MockTransport(handler),
    )
    allowed = asyncio.run(adapter.check(kind="input", model="test-model", content="hello"))
    blocked = asyncio.run(adapter.check(kind="output", model="test-model", content="blocked content"))
    assert allowed.allowed is True
    assert blocked.allowed is False
    assert blocked.reason_code == "policy_disallowed"
    assert [item["kind"] for item in seen] == ["input", "output"]


def test_content_safety_is_fail_closed_and_sanitizes_provider_failures():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret-provider-debug")

    adapter = HttpContentSafetyAdapter(
        endpoint="https://safety.example/v1/check",
        api_key="moderation-secret",
        allowed_hosts={"safety.example"},
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ContentSafetyError, match="内容安全服务不可用") as exc_info:
        asyncio.run(adapter.check(kind="input", model="test-model", content="private prompt"))
    assert "secret-provider-debug" not in str(exc_info.value)
    assert "private prompt" not in str(exc_info.value)


def test_content_safety_rejects_unlisted_hosts_and_oversized_payloads():
    with pytest.raises(ValueError, match="允许列表"):
        HttpContentSafetyAdapter(
            endpoint="https://internal.example/v1/check",
            api_key="secret",
            allowed_hosts={"safety.example"},
        )
    adapter = HttpContentSafetyAdapter(
        endpoint="https://safety.example/v1/check",
        api_key="secret",
        allowed_hosts={"safety.example"},
        max_content_bytes=16,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"allowed": True})),
    )
    with pytest.raises(ContentSafetyError, match="正文超过"):
        asyncio.run(adapter.check(kind="input", model="test-model", content="x" * 17))


def test_content_safety_rejects_oversized_response_during_stream_read():
    adapter = HttpContentSafetyAdapter(
        endpoint="https://safety.example/v1/check",
        api_key="secret",
        allowed_hosts={"safety.example"},
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"x" * 65_537),
        ),
    )
    with pytest.raises(ContentSafetyError, match="服务不可用"):
        asyncio.run(adapter.check(kind="input", model="test-model", content="hello"))
