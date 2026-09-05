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


def test_production_compose_uses_external_supabase_and_caddy_ingress():
    compose = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    env = (ROOT / ".env.production.example").read_text(encoding="utf-8")
    assert "  postgres:" not in compose
    assert "kunlun-postgres" not in compose
    assert "init-postgres-roles.sh" not in compose
    assert "KUNLUN_TRUSTED_PROXY_CIDRS=172.30.50.2/32" in env
    assert "KUNLUN_INGRESS_PROVIDER=" in env
    assert "KUNLUN_INGRESS_PROVIDER=vercel" not in env


def test_vercel_ingress_settings_are_kept_in_dedicated_template():
    template = (ROOT / ".env.vercel.production.example").read_text(encoding="utf-8")
    for key in (
        "KUNLUN_INGRESS_PROVIDER=vercel",
        "KUNLUN_TRUSTED_PROXY_CIDRS=",
        "KUNLUN_TRUSTED_PROXY_SECRET=",
        "KUNLUN_OPS_INGRESS_SECRET=",
    ):
        assert key in template
    for key in (
        "KUNLUN_DATABASE_URL=",
        "KUNLUN_VAULT_EXECUTOR_DATABASE_URL=",
        "KUNLUN_ENV=production",
        "KUNLUN_GATEWAY_MODE=byok",
        "KUNLUN_VAULT_BACKEND=supabase_vault",
        "KUNLUN_PUBLIC_SIGNUP=false",
        "KUNLUN_LIVE_PAYMENTS=false",
        "KUNLUN_LIVE_UPSTREAM=false",
        "KUNLUN_TOPUP_PACKAGES_JSON={}",
        "KUNLUN_PROVIDERS_JSON=",
        "KUNLUN_PROVIDER_HOST_ALLOWLIST=",
        "KUNLUN_MODELS_JSON=",
        "KUNLUN_OPERATOR_SIGNING_SECRET=",
        "CRON_SECRET=",
    ):
        assert key in template
    for prohibited in (
        "KUNLUN_MIGRATOR_DATABASE_URL",
        "POSTGRES_ADMIN_PASSWORD",
        "POSTGRES_ADMIN_USER",
    ):
        assert prohibited not in template


def test_concurrency_verification_requires_explicit_isolated_database_ack(monkeypatch):
    monkeypatch.delenv("KUNLUN_CONFIRM_TEST_DATABASE", raising=False)
    assert not _require_isolated_database_confirmation()
    monkeypatch.setenv("KUNLUN_CONFIRM_TEST_DATABASE", ISOLATED_DATABASE_CONFIRMATION)
    assert _require_isolated_database_confirmation()
