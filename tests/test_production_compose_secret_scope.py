from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.production.yml"


def _compose_environment(tmp_path: Path) -> dict[str, object]:
    """Render the production compose file using inert, non-secret test values."""
    values = {
        "KUNLUN_RUNTIME_DATABASE_URL": "postgresql://kunlun_runtime:test-runtime-password@db.invalid/kunlun",
        "KUNLUN_MIGRATOR_DATABASE_URL": "postgresql://kunlun_migrator:test-migrator-password@db.invalid/kunlun",
        "KUNLUN_VAULT_EXECUTOR_DATABASE_URL": "postgresql://kunlun_vault_executor:test-executor-password@db.invalid/kunlun",
        "PUBLIC_DOMAIN": "example.invalid",
        "ACME_EMAIL": "ops@example.invalid",
        "KUNLUN_ENV": "production",
        "KUNLUN_GATEWAY_MODE": "byok",
        "KUNLUN_VAULT_BACKEND": "supabase_vault",
        "KUNLUN_PUBLIC_SIGNUP": "false",
        "KUNLUN_ENABLE_TEST_PAYMENTS": "false",
        "KUNLUN_LIVE_PAYMENTS": "false",
        "KUNLUN_LIVE_UPSTREAM": "false",
        "KUNLUN_API_KEY_PEPPER": "a" * 32,
        "KUNLUN_SESSION_PEPPER": "b" * 32,
        "KUNLUN_IDENTITY_TOKEN_PEPPER": "i" * 32,
        "KUNLUN_OPERATOR_SIGNING_SECRET": "c" * 32,
        "KUNLUN_OPS_PRIVATE_ACCESS_ACKNOWLEDGED": "true",
        "KUNLUN_TRUSTED_PROXY_CIDRS": "",
        "KUNLUN_TRUSTED_PROXY_SECRET": "d" * 32,
        "KUNLUN_INGRESS_PROVIDER": "",
        "KUNLUN_OPS_INGRESS_SECRET": "e" * 32,
        "CRON_SECRET": "f" * 32,
        "KUNLUN_PROVIDERS_JSON": "[]",
        "KUNLUN_PROVIDER_HOST_ALLOWLIST": "example.invalid",
        "KUNLUN_MODELS_JSON": '{"test-model":{"input_microusd_per_million":1,"output_microusd_per_million":1}}',
    }
    env_file = tmp_path / "compose.env"
    env_file.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")
    completed = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f", str(COMPOSE), "config", "--format", "json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    return json.loads(completed.stdout)


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is required for compose config validation")
def test_production_compose_scopes_database_and_bootstrap_secrets(tmp_path):
    config = _compose_environment(tmp_path)
    services = config["services"]

    prohibited_bootstrap = {
        "POSTGRES_ADMIN_PASSWORD",
        "POSTGRES_ADMIN_USER",
        "KUNLUN_RUNTIME_DB_PASSWORD",
        "KUNLUN_MIGRATOR_DB_PASSWORD",
        "KUNLUN_VAULT_EXECUTOR_DB_PASSWORD",
    }
    prohibited_long_lived = prohibited_bootstrap | {"KUNLUN_MIGRATOR_DATABASE_URL"}

    api_environment = services["api"]["environment"]
    maintenance_environment = services["maintenance"]["environment"]
    migrate_environment = services["migrate"]["environment"]
    preflight_environment = services["preflight"]["environment"]

    assert not (set(api_environment) & prohibited_long_lived)
    assert not (set(maintenance_environment) & (prohibited_long_lived | {"KUNLUN_VAULT_EXECUTOR_DATABASE_URL"}))
    assert not (set(migrate_environment) & prohibited_bootstrap)
    assert not (set(preflight_environment) & prohibited_bootstrap)
    assert "KUNLUN_DATABASE_URL" in api_environment
    assert "KUNLUN_VAULT_EXECUTOR_DATABASE_URL" in api_environment
    assert set(migrate_environment) == {"KUNLUN_DATABASE_URL"}
    assert {"KUNLUN_DATABASE_URL", "KUNLUN_MIGRATOR_DATABASE_URL", "KUNLUN_VAULT_EXECUTOR_DATABASE_URL"} <= set(preflight_environment)
    assert services["preflight"]["restart"] == "no"
    assert "postgres" not in services

    compose_text = COMPOSE.read_text(encoding="utf-8")
    for service in ("migrate", "preflight", "api", "maintenance"):
        service_block = re.search(
            rf"(?ms)^  {service}:\n(.*?)(?=^  [a-z][a-z_-]*:|\Z)", compose_text,
        )
        assert service_block is not None
        assert "env_file:" not in service_block.group(1)
