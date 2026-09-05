from tests.test_managed_gateway import managed, ready_call
from tests.test_ops_console import operator
from app.services.credentials import SecretUnavailable


def test_channel_catalog_and_detail_include_unconfigured_allowlist_without_secrets(managed):
    client, auth, _, ops, calls = managed
    assert client.get("/ops/channels/openai", headers=auth).status_code == 401
    assert client.get("/ops/channels/openai", headers=operator("channels:write")).status_code == 401
    listing = client.get("/ops/channels", headers=ops).json()
    assert listing["channels"] == []
    row = listing["catalog"][0]
    assert row["provider"] == "openai" and row["status"] == "unconfigured"
    assert row["models"] == ["test-model"] and row["priority"] == 1
    assert row["upstream_host"] == "api.openai.com"
    assert row["version"] == 0 and row["id"] is None
    result = client.get("/ops/channels/openai", headers=ops)
    assert result.json()["channel"] == row and "no-store" in result.headers["cache-control"]
    assert client.get("/ops/channels/not-allowed", headers=ops).status_code == 404
    ready_call(managed)
    saved = client.get("/ops/channels/openai", headers=ops).json()["channel"]
    assert saved["status"] == "enabled" and saved["version"] == 1
    assert "inert-platform" not in str(saved)
    assert calls == []


def test_channel_cleanup_state_and_metadata_read_failure_are_not_health_success(managed, monkeypatch):
    client, _, _, ops, _ = managed
    vault = client.app.state.platform_vault
    monkeypatch.setattr(vault, "list", lambda: [{"provider": "openai", "id": "test-id", "version": 1,
                                                "active": False, "pending_cleanup": True}])
    row = client.get("/ops/channels/openai", headers=ops).json()["channel"]
    assert row["status"] == "pending_cleanup"
    def unavailable():
        raise SecretUnavailable("inert secret detail must not be exposed")
    monkeypatch.setattr(vault, "list", unavailable)
    result = client.get("/ops/channels/openai", headers=ops)
    assert result.status_code == 503 and "inert secret" not in result.text
