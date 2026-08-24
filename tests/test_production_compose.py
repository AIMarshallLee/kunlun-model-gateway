from __future__ import annotations

from pathlib import Path

from scripts.verify_postgres_concurrency import (
    ISOLATED_DATABASE_CONFIRMATION,
    _require_isolated_database_confirmation,
)


ROOT = Path(__file__).resolve().parents[1]


def test_production_compose_has_migration_followed_by_runtime_preflight_gate():
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    assert "  preflight:" in compose
    assert 'command: ["kunlun-production-preflight"]' in compose
    assert "KUNLUN_DATABASE_URL: ${KUNLUN_RUNTIME_DATABASE_URL:?set KUNLUN_RUNTIME_DATABASE_URL}" in compose
    assert "KUNLUN_MIGRATOR_DATABASE_URL: ${KUNLUN_MIGRATOR_DATABASE_URL:?set KUNLUN_MIGRATOR_DATABASE_URL}" in compose
    preflight = compose.split("  preflight:", 1)[1].split("\n  api:", 1)[0]
    assert "migrate:" in preflight
    assert "condition: service_completed_successfully" in preflight
    api = compose.split("  api:", 1)[1].split("\n  maintenance:", 1)[0]
    maintenance = compose.split("  maintenance:", 1)[1].split("\n  caddy:", 1)[0]
    assert "preflight:" in api
    assert "preflight:" in maintenance
    assert "condition: service_completed_successfully" in api
    assert "condition: service_completed_successfully" in maintenance


def test_concurrency_verification_requires_explicit_isolated_database_ack(monkeypatch):
    monkeypatch.delenv("KUNLUN_CONFIRM_TEST_DATABASE", raising=False)
    assert not _require_isolated_database_confirmation()
    monkeypatch.setenv("KUNLUN_CONFIRM_TEST_DATABASE", ISOLATED_DATABASE_CONFIRMATION)
    assert _require_isolated_database_confirmation()
