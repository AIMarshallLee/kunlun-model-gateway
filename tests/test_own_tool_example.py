from contextlib import contextmanager
import io
import json

import httpx
import pytest
from sqlalchemy import select

from app.models import ModelRequest, Wallet
from tests.test_managed_gateway import managed, ready_call
from examples.own_tool_gateway import GatewayTool, bounded_body, main, stream_output


def tool_for(client, key):
    return GatewayTool("https://testserver", key, client=client)


def test_example_calls_once_and_recovers_duplicate_without_replaying_content(managed):
    client, _, payload = ready_call(managed)
    tool = tool_for(client, managed[2])
    first = tool.submit("sample-1", payload)
    assert first["delivery"] == "complete"
    assert first["output"]["choices"][0]["message"]["content"] == "OK"
    duplicate = tool.submit("sample-1", payload)
    assert duplicate["delivery"] == "needs_review"
    assert duplicate["task"]["status"] == "settled"
    assert duplicate["output"] is None
    assert duplicate["automatic_resubmit_allowed"] is False
    assert len(managed[-1]) == 1
    with client.app.state.SessionLocal() as db:
        assert len(db.scalars(select(ModelRequest)).all()) == 1
        assert db.scalar(select(Wallet)).balance_microusd == 99994


def test_example_insufficient_balance_does_not_call_provider(managed):
    client, _, key, _, calls = managed
    result = tool_for(client, key).submit("no-funds", {
        "model": "test-model", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 16,
    })
    assert result["http_status"] == 402 and result["lookup_http_status"] == 404
    assert result["delivery"] == "needs_review" and calls == []


@pytest.mark.parametrize("done", [True, False])
def test_example_stream_requires_done_and_settled_task(managed, done):
    client, _, payload = ready_call(managed)
    events = [{"choices": [{"delta": {"content": "draft"}}]},
              {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}]
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    if done:
        body += "data: [DONE]\n\n"
    client.app.state.test_upstream = lambda _: httpx.Response(
        200, content=body, headers={"Content-Type": "text/event-stream"})
    result = tool_for(client, managed[2]).submit("stream-1", {**payload, "stream": True})
    assert result["delivery"] == ("complete" if done else "needs_review")
    assert result["task"]["status"] == ("settled" if done else "pending_reconciliation")
    assert len(managed[-1]) == 1


def test_example_preserves_tool_call_without_executing_it(managed):
    client, _, payload = ready_call(managed)
    call = {"id": "call-1", "type": "function", "function": {"name": "preview", "arguments": "{}"}}
    client.app.state.test_upstream = lambda _: httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    })
    result = tool_for(client, managed[2]).submit("tools-1", {**payload,
        "tools": [{"type": "function", "function": {"name": "preview", "parameters": {"type": "object"}}}],
    })
    assert result["output"]["choices"][0]["message"]["tool_calls"] == [call]
    assert len(managed[-1]) == 1


@pytest.mark.parametrize("failure", ["timeout", "redirect", "invalid_json"])
def test_example_transport_failure_only_looks_up_original_task(failure):
    seen = []
    def respond(request):
        seen.append((request.url.path, request.headers["Idempotency-Key"]))
        if request.url.path.endswith("lookup"):
            return httpx.Response(404)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret raw exception")
        if failure == "redirect":
            return httpx.Response(307, headers={"Location": "https://untrusted.invalid"})
        return httpx.Response(200, content="not json")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = GatewayTool("https://gateway.example", "inert-key", client=client).submit("same-op", {})
    assert seen == [("/v1/chat/completions", "same-op"), ("/v1/requests/lookup", "same-op")]
    assert result["delivery"] == "needs_review"
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("origin", ["http://example.com", "https://user:pass@example.com", "https://example.com/v1", "https://example.com?key=secret"])
def test_example_rejects_unsafe_or_ambiguous_origin(origin):
    with pytest.raises(ValueError):
        GatewayTool(origin, "inert", client=None)


def test_example_preview_does_not_read_key_or_input(monkeypatch, capsys):
    monkeypatch.delenv("KUNLUN_TOOL_API_KEY", raising=False)
    monkeypatch.setenv("KUNLUN_TOOL_ORIGIN", "https://gateway.example")
    assert main(["--operation-id", "preview-1"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "preview"


def test_example_lost_response_recovers_settled_task_without_second_charge(managed):
    client, _, payload = ready_call(managed)
    class LostAnswer:
        @contextmanager
        def stream(self, method, url, **kwargs):
            with client.stream(method, url, **kwargs) as response:
                if url.endswith("completions"):
                    response.read()
                    raise httpx.ReadTimeout("client lost already settled answer")
                yield response
    result = GatewayTool("https://testserver", managed[2], client=LostAnswer()).submit("lost-answer", payload)
    assert result["delivery"] == "needs_review" and result["output"] is None
    assert result["task"]["status"] == "settled"
    assert len(managed[-1]) == 1


def test_example_bounded_body_and_incomplete_sse(monkeypatch):
    monkeypatch.setattr("examples.own_tool_gateway.MAX_BYTES", 3)
    with pytest.raises(ValueError):
        bounded_body(httpx.Response(200, content=b"1234"))
    assert stream_output(b'data: {"choices": []}\n\ndata: [DONE]') == ([{"choices": []}], False)
    with pytest.raises(ValueError):
        stream_output(b'data: [DONE]\n\ndata: {}\n\n')


def test_example_cli_execution_uses_only_site_key_and_returns_business_output(monkeypatch, capsys):
    seen = []
    def respond(request):
        seen.append(request)
        if request.url.path.endswith("lookup"):
            return httpx.Response(200, json={"status": "settled"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "synthetic draft"}}]})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr("examples.own_tool_gateway.httpx.Client", lambda **_: client)
    monkeypatch.setenv("KUNLUN_TOOL_ORIGIN", "https://gateway.example")
    monkeypatch.setenv("KUNLUN_TOOL_API_KEY", "site-inert-key")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b'{"model":"test-model"}')))
    assert main(["--operation-id", "cli-1", "--execute"]) == 0
    assert [r.headers["authorization"] for r in seen] == ["Bearer site-inert-key"] * 2
    assert json.loads(capsys.readouterr().out)["delivery"] == "complete"
