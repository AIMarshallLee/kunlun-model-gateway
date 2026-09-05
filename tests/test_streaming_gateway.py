from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app import providers
from app.models import ModelRequest, ProviderAttempt
from app.providers import OpenAICompatibleProvider
from app.streaming import SSEUsageTracker


def _sse(events):
    return "".join(f"data: {json.dumps(event, separators=(',', ':'))}\n\n" for event in events) + "data: [DONE]\n\n"


def test_real_sse_is_forwarded_and_settled_from_final_usage(client, funded_api_key, monkeypatch):
    observed = {}
    events = [
        {
            "id": "chunk-1", "object": "chat.completion.chunk", "model": "test-model",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "你好"}, "finish_reason": None}],
        },
        {
            "id": "chunk-1", "object": "chat.completion.chunk", "model": "test-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "chunk-1", "object": "chat.completion.chunk", "model": "test-model",
            "choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
        },
    ]

    def handler(request: httpx.Request):
        observed.update(json.loads(request.content))
        return httpx.Response(200, text=_sse(events), headers={"Content-Type": "text/event-stream"})

    provider = OpenAICompatibleProvider(
        provider_name="stream-provider",
        base_url="https://provider.example/v1",
        api_key="upstream-secret",
        pricing={"test-model": {
            "input_microusd_per_million": 2_000_000,
            "output_microusd_per_million": 3_000_000,
        }},
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(providers, "ordered_clients", [provider])
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {funded_api_key}"},
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        },
    )
    assert response.status_code == 200
    assert response.text.endswith("data: [DONE]\n\n")
    assert observed["stream"] is True
    assert observed["stream_options"] == {"include_usage": True}
    assert observed["tools"][0]["function"]["name"] == "lookup"
    with client.app.state.SessionLocal() as session:
        request = session.scalar(select(ModelRequest))
        assert request is not None
        assert request.status == "settled"
        assert request.input_tokens == 8
        assert request.output_tokens == 3
        assert request.usage_estimated is False
        assert request.charged_microusd == 11
        assert request.upstream_cost_microusd == 25


def test_stream_without_done_marker_is_held_for_reconciliation(client, funded_api_key, monkeypatch):
    partial = "data: " + json.dumps({
        "id": "partial", "object": "chat.completion.chunk", "model": "test-model",
        "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}],
    }) + "\n\n"

    provider = OpenAICompatibleProvider(
        provider_name="partial-provider",
        base_url="https://provider.example/v1",
        api_key="upstream-secret",
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, text=partial, headers={"Content-Type": "text/event-stream"},
        )),
    )
    monkeypatch.setattr(providers, "ordered_clients", [provider])
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {funded_api_key}"},
        json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}], "stream": True},
    )
    assert response.status_code == 200
    assert "partial" in response.text
    with client.app.state.SessionLocal() as session:
        request = session.scalar(select(ModelRequest))
        assert request is not None and request.status == "pending_reconciliation"
        assert request.failure_category == "provider_stream_incomplete"


@pytest.mark.parametrize("usage", [
    {"prompt_tokens": True, "completion_tokens": 2},
    {"prompt_tokens": 2, "completion_tokens": "3"},
    {"prompt_tokens": -1, "completion_tokens": 2},
])
def test_sse_tracker_never_turns_malformed_usage_into_an_automatic_settlement(usage):
    tracker = SSEUsageTracker()
    tracker.feed(("data: " + json.dumps({
        "choices": [{"delta": {"content": "中文", "tool_calls": [{"function": {"arguments": "{\\\"x\\\":1}"}}]}}],
        "usage": usage,
    }) + "\n\ndata: [DONE]\n\n").encode())
    tracker.finish()
    _response, estimated = tracker.settlement_response("test-model", 5)
    assert tracker.done is True
    assert estimated is True


def test_stream_can_fail_over_before_downstream_headers(client, funded_api_key, monkeypatch):
    first = OpenAICompatibleProvider(
        provider_name="limited-provider",
        base_url="https://limited.example/v1",
        api_key="secret",
        transport=httpx.MockTransport(lambda request: httpx.Response(429, json={"error": "limited"})),
    )
    second = AsyncMock(return_value={
        "id": "fallback-ok",
        "model": "test-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "fallback"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    })
    monkeypatch.setattr(providers, "ordered_clients", [first, second])
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {funded_api_key}"},
        json={"model": "test-model", "messages": [{"role": "user", "content": "hello"}], "stream": True},
    )
    assert response.status_code == 200
    assert "fallback" in response.text
    assert second.await_count == 1
    with client.app.state.SessionLocal() as session:
        attempts = session.scalars(select(ProviderAttempt).order_by(ProviderAttempt.ordinal)).all()
        assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]
