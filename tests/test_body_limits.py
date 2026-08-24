from __future__ import annotations


def test_chunked_chat_body_cannot_bypass_actual_byte_limit(client, api_key):
    def chunks():
        yield b'{"model":"test-model","messages":[{"role":"user","content":"'
        for _ in range(17):
            yield b"x" * 65_536
        yield b'"}]}'

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Transfer-Encoding": "chunked"},
        content=chunks(),
    )
    assert response.status_code == 413
    assert response.headers["Connection"] == "close"


def test_payment_webhook_has_smaller_actual_byte_limit(client):
    response = client.post(
        "/billing/webhook",
        headers={"Transfer-Encoding": "chunked", "X-Webhook-Signature": "invalid"},
        content=(b"x" * 20_000 for _ in range(4)),
    )
    assert response.status_code == 413
