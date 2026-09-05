"""Deployment contracts with inert configuration; never connects to a service."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from app.config import Settings
from scripts import preflight


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (".env.managed.vercel.example", ".env.managed.compose.example")


def read_template(name):
    pairs = [line.split("=", 1) for line in (ROOT / name).read_text().splitlines()
             if line and not line.startswith("#")]
    assert len(dict(pairs)) == len(pairs), "duplicate environment field"
    return dict(pairs)


@pytest.fixture
def clean_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith("KUNLUN_") or key in {"CRON_SECRET", "VERCEL_ENV", "PUBLIC_DOMAIN", "ACME_EMAIL"}:
            monkeypatch.delenv(key)
    # Any accidental network-capable DB construction fails the test, even if
    # caught by the preflight's ordinary database-error handling.
    attempted = []
    def no_engine(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("configuration-only must not create a database engine")
    monkeypatch.setattr(preflight, "build_engine", no_engine)
    yield
    assert not attempted


def filled_template(name):
    env = read_template(name)
    ca = ROOT / "certs/supabase-prod-ca-2021.crt"
    def database(role):
        return (f"postgresql+psycopg://kunlun_{role}:inert-{role}-password@db.abcdefghijklmnopqrst.supabase.co/postgres"
                f"?sslmode=verify-full&sslrootcert={ca}")
    env.update({
        "KUNLUN_DATABASE_URL": database("runtime"),
        "KUNLUN_RUNTIME_DATABASE_URL": database("runtime"),
        "KUNLUN_MIGRATOR_DATABASE_URL": database("migrator"),
        "KUNLUN_VAULT_EXECUTOR_DATABASE_URL": database("vault_executor"),
        "PUBLIC_DOMAIN": "gateway.example.invalid", "ACME_EMAIL": "ops@example.invalid",
        "KUNLUN_PLATFORM_DAILY_BUDGET_MICROUSD": "1000000",
        "KUNLUN_SUPPLIER_USE_ACKNOWLEDGED": "true",
        "KUNLUN_PUBLIC_SIGNUP": "true", "KUNLUN_LIVE_PAYMENTS": "true", "KUNLUN_LIVE_UPSTREAM": "true",
        "KUNLUN_OPS_PRIVATE_ACCESS_ACKNOWLEDGED": "true", "KUNLUN_COMPLIANCE_ACKNOWLEDGED": "true",
        "KUNLUN_SMTP_URL": "smtps://inert:password@smtp.example.invalid:465",
        "KUNLUN_EMAIL_FROM": "noreply@example.invalid", "KUNLUN_PUBLIC_BASE_URL": "https://gateway.example.invalid",
        "KUNLUN_CAPTCHA_SITE_KEY": "inert-site-key", "KUNLUN_CAPTCHA_EXPECTED_HOSTNAME": "gateway.example.invalid",
        "KUNLUN_TERMS_URL": "https://gateway.example.invalid/terms", "KUNLUN_PRIVACY_URL": "https://gateway.example.invalid/privacy",
        "KUNLUN_COMPLAINT_EMAIL": "support@example.invalid",
        "KUNLUN_CONTENT_SAFETY_ENDPOINT": "https://safety.example.invalid/check",
        "KUNLUN_CONTENT_SAFETY_HOST_ALLOWLIST": "safety.example.invalid",
        "KUNLUN_CONTENT_SAFETY_POLICY_VERSION": "synthetic-v1",
        "KUNLUN_PAYMENT_PROVIDER": "inert-provider", "KUNLUN_PAYMENT_BRIDGE_ENDPOINT": "https://pay.example.invalid",
        "KUNLUN_PAYMENT_BRIDGE_MERCHANT_ID": "inert-merchant", "KUNLUN_PAYMENT_BRIDGE_HOST_ALLOWLIST": "pay.example.invalid",
        "KUNLUN_PAYMENT_BRIDGE_OFFICIAL_SDK_ACKNOWLEDGED": "true",
        "KUNLUN_TOPUP_PACKAGES_JSON": json.dumps({"inert-sku": {
            "payment_amount_minor": 100, "payment_currency": "USD", "credit_amount_microusd": 1000000}}),
        "KUNLUN_MODELS_JSON": json.dumps({"inert-model": {
            "input_microusd_per_million": 1000000, "output_microusd_per_million": 1000000, "max_output_tokens": 4096}}),
        "KUNLUN_PROVIDERS_JSON": json.dumps([{"name": "openai", "base_url": "https://api.openai.com/v1",
            "models": ["inert-model"], "pricing": {"inert-model": {
                "input_microusd_per_million": 500000, "output_microusd_per_million": 500000}}}]),
        "KUNLUN_PROVIDER_HOST_ALLOWLIST": "api.openai.com",
    })
    for field in ("KUNLUN_API_KEY_PEPPER", "KUNLUN_SESSION_PEPPER", "KUNLUN_IDENTITY_TOKEN_PEPPER",
                  "KUNLUN_OPERATOR_SIGNING_SECRET", "KUNLUN_TRUSTED_PROXY_SECRET", "KUNLUN_OPS_INGRESS_SECRET",
                  "CRON_SECRET", "KUNLUN_CAPTCHA_SECRET", "KUNLUN_CONTENT_SAFETY_API_KEY", "KUNLUN_PAYMENT_BRIDGE_SECRET"):
        env[field] = "inert-only-" + field + "-012345678901234567890123456789"
    return env


def set_environment(monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("name", TEMPLATES)
def test_unfilled_managed_template_is_closed_and_preflight_fails_without_network(name, clean_environment, monkeypatch, capsys):
    env = read_template(name)
    assert env["KUNLUN_GATEWAY_MODE"] == "managed_gateway"
    for field in ("KUNLUN_PUBLIC_SIGNUP", "KUNLUN_LIVE_PAYMENTS", "KUNLUN_LIVE_UPSTREAM", "KUNLUN_ENABLE_TEST_PAYMENTS",
                  "KUNLUN_SUPPLIER_USE_ACKNOWLEDGED", "KUNLUN_COMPLIANCE_ACKNOWLEDGED",
                  "KUNLUN_OPS_PRIVATE_ACCESS_ACKNOWLEDGED", "KUNLUN_PAYMENT_BRIDGE_OFFICIAL_SDK_ACKNOWLEDGED"):
        assert env[field] == "false"
    assert env["KUNLUN_REQUIRE_EMAIL_VERIFICATION"] == env["KUNLUN_CAPTCHA_REQUIRED"] == "true"
    assert env["KUNLUN_PROVIDERS_JSON"] == "[]" and env["KUNLUN_TOPUP_PACKAGES_JSON"] == "{}"
    assert env["KUNLUN_PLATFORM_DAILY_BUDGET_MICROUSD"] == "0"
    if "vercel" in name:
        assert env["KUNLUN_INGRESS_PROVIDER"] == "vercel"
        assert "KUNLUN_MIGRATOR_DATABASE_URL" not in env
    set_environment(monkeypatch, env)
    assert preflight.main(config_only=True, require_managed_launch=True) == 1
    assert "通过。" not in capsys.readouterr().out


@pytest.mark.parametrize("name", TEMPLATES)
def test_filled_configuration_checks_only_local_contract_not_readiness(name, clean_environment, monkeypatch, capsys):
    set_environment(monkeypatch, filled_template(name))
    assert preflight.main(config_only=True, require_managed_launch=True) == 0
    output = capsys.readouterr().out
    assert "配置静态检查通过" in output and "未连接数据库" in output and "不等于商业上线" in output
    assert "生产技术预检通过" not in output and "inert-" not in output


@pytest.mark.parametrize("flag", ["KUNLUN_PUBLIC_SIGNUP", "KUNLUN_LIVE_PAYMENTS"])
def test_launch_check_rejects_closed_customer_path(flag, clean_environment, monkeypatch, capsys):
    env = filled_template(TEMPLATES[0]); env[flag] = "false"
    set_environment(monkeypatch, env)
    assert preflight.main(config_only=True, require_managed_launch=True) == 1
    assert flag in capsys.readouterr().out


def test_launch_check_rejects_other_mode_and_does_not_modify_environment(clean_environment, monkeypatch, capsys):
    env = filled_template(TEMPLATES[0])
    env.update(KUNLUN_GATEWAY_MODE="byok", KUNLUN_PUBLIC_SIGNUP="false", KUNLUN_LIVE_PAYMENTS="false",
               KUNLUN_LIVE_UPSTREAM="false", KUNLUN_TOPUP_PACKAGES_JSON="{}")
    set_environment(monkeypatch, env)
    assert Settings.from_env().gateway_mode == "byok"
    assert preflight.main(config_only=True, require_managed_launch=True) == 1
    assert "managed_gateway" in capsys.readouterr().out
    assert os.environ["KUNLUN_LIVE_UPSTREAM"] == "false"


def test_configuration_error_is_sanitized_and_cli_selects_strict_profile(clean_environment, monkeypatch, capsys):
    env = filled_template(TEMPLATES[0]); env["KUNLUN_PROVIDERS_JSON"] = "inert-secret-malformed-json"
    set_environment(monkeypatch, env)
    assert preflight.cli(["--config-only", "--require-managed-launch"]) == 1
    assert "inert-secret" not in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        preflight.cli(["--typo-do-not-ignore"])
    assert error.value.code == 2


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is required")
def test_managed_compose_overlay_transmits_configuration_without_broadening_secret_scope(clean_environment, monkeypatch):
    env = filled_template(TEMPLATES[1])
    # Compose interpolation is a render only: no build, up, migration or API.
    set_environment(monkeypatch, env)
    result = subprocess.run(["docker", "compose", "--env-file", "/dev/null", "-f", "docker-compose.production.yml",
        "-f", "docker-compose.managed.yml", "config", "--format", "json"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, "synthetic compose render failed"
    services = json.loads(result.stdout)["services"]
    for name in ("api", "preflight"):
        rendered = services[name]["environment"]
        for key, value in read_template(TEMPLATES[1]).items():
            if key.startswith("KUNLUN_") and key not in {"KUNLUN_RUNTIME_DATABASE_URL", "KUNLUN_MIGRATOR_DATABASE_URL"}:
                assert rendered.get(key) == env[key], key
        set_environment(monkeypatch, rendered)
        assert Settings.from_env().gateway_mode == "managed_gateway"
    assert services["preflight"]["command"] == ["kunlun-production-preflight", "--require-managed-launch"]
    assert "migrate" in services["preflight"]["depends_on"]
    assert "preflight" in services["api"]["depends_on"]
    assert "KUNLUN_MIGRATOR_DATABASE_URL" not in services["api"]["environment"]
    assert set(services["migrate"]["environment"]) == {"KUNLUN_DATABASE_URL"}
    protected = {"KUNLUN_VAULT_EXECUTOR_DATABASE_URL", "KUNLUN_MIGRATOR_DATABASE_URL", "KUNLUN_PAYMENT_BRIDGE_SECRET",
                 "KUNLUN_SMTP_URL", "KUNLUN_CAPTCHA_SECRET", "KUNLUN_CONTENT_SAFETY_API_KEY"}
    assert not protected.intersection(services["maintenance"]["environment"])
    assert set(services["caddy"]["environment"]) == {"PUBLIC_DOMAIN", "ACME_EMAIL"}


def test_config_only_still_rejects_database_target_or_credential_mismatch(clean_environment, monkeypatch, capsys):
    env = filled_template(TEMPLATES[0])
    env["KUNLUN_MIGRATOR_DATABASE_URL"] = env["KUNLUN_MIGRATOR_DATABASE_URL"].replace("abcdefghijklmnopqrst", "zyxwvutsrqponmlkjihg")
    set_environment(monkeypatch, env)
    assert preflight.main(config_only=True, require_managed_launch=True) == 1
    assert "同一 Supabase" in capsys.readouterr().out
    env = filled_template(TEMPLATES[0])
    env["KUNLUN_MIGRATOR_DATABASE_URL"] = env["KUNLUN_MIGRATOR_DATABASE_URL"].replace("inert-migrator-password", "inert-runtime-password")
    set_environment(monkeypatch, env)
    assert preflight.main(config_only=True, require_managed_launch=True) == 1
    assert "凭据不得重复" in capsys.readouterr().out


def test_default_full_preflight_keeps_database_gate(clean_environment, monkeypatch, capsys):
    set_environment(monkeypatch, filled_template(TEMPLATES[0]))
    observations = []
    class Engine:
        def dispose(self):
            observations.append("disposed")
    monkeypatch.setattr(preflight, "build_engine", lambda url: Engine())
    def missing_schema(engine, revision):
        observations.append(revision)
        raise RuntimeError("inert internal database error")
    monkeypatch.setattr(preflight, "assert_schema_revision", missing_schema)
    assert preflight.main(require_managed_launch=True) == 1
    assert observations == [preflight.SCHEMA_HEAD, "disposed"]
    output = capsys.readouterr().out
    assert "Alembic head" in output and "inert internal" not in output and "检查通过" not in output
