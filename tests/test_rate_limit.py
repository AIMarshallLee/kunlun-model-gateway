def test_rate_limit_returns_429_with_retry_after(client, api_key):
    headers = {"Authorization": f"Bearer {api_key}"}
    responses = [client.get("/v1/models", headers=headers) for _ in range(20)]
    limited = [response for response in responses if response.status_code == 429]
    assert limited, "gateway must enforce a per-key request limit"
    assert "Retry-After" in limited[0].headers
