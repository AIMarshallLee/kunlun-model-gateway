from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app import create_app
from app.config import Settings, _production_database_url_is_safe
from app.models import ModelPrice
from app.providers import (
    OpenAICompatibleProvider,
    build_byok_provider_client,
    build_provider_clients,
    supports_model,
)
from gateway import ProviderError


CA_PATH = Path(__file__).resolve().parents[1] / "certs" / "supabase-prod-ca-2021.crt"


def _production_database_url(user: str = "gateway", password: str = "secret") -> str:
    return (
        f"postgresql+psycopg://{user}:{password}@db.oyhavtaalkidrllxfigw.supabase.co/gateway"
        f"?sslmode=verify-full&sslrootcert={CA_PATH}"
    )


def test_production_config_fails_closed_and_accepts_safe_control_plane():
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        Settings.from_env(
            environment="production",
            gateway_mode="disabled",
            database_url="sqlite:///unsafe.sqlite3",
            api_key_pepper="a" * 32,
            session_pepper="b" * 32,
        )

    settings = Settings.from_env(
        environment="production",
        gateway_mode="disabled",
        database_url=_production_database_url(),
        api_key_pepper="a" * 32,
        session_pepper="b" * 32,
        api_key_pepper_persisted=True,
        session_pepper_persisted=True,
        trusted_proxy_cidrs={"127.0.0.1/32"},
    )
    assert settings.is_production
    assert settings.public_signup is False
    assert settings.live_payments is False

    for unsafe_url in (
        "postgresql+psycopg://gateway:secret@db/gateway",
        f"postgresql+psycopg://gateway:secret@db/gateway?sslmode=require&sslrootcert={CA_PATH}",
        "postgresql+psycopg://gateway:secret@db/gateway?sslmode=verify-full&sslrootcert=/missing/ca.crt",
        f"postgresql://gateway:secret@db/gateway?sslmode=verify-full&sslrootcert={CA_PATH}",
    ):
        with pytest.raises(RuntimeError, match="verify-full"):
            Settings.from_env(
                environment="production",
                gateway_mode="disabled",
                database_url=unsafe_url,
                api_key_pepper="a" * 32,
                session_pepper="b" * 32,
                api_key_pepper_persisted=True,
                session_pepper_persisted=True,
                trusted_proxy_cidrs={"127.0.0.1/32"},
            )

    with pytest.raises(RuntimeError, match="精确"):
        Settings.from_env(
            environment="production",
            gateway_mode="disabled",
            database_url=_production_database_url(),
            api_key_pepper="a" * 32,
            session_pepper="b" * 32,
            api_key_pepper_persisted=True,
            session_pepper_persisted=True,
            trusted_proxy_cidrs={"0.0.0.0/0"},
        )


@pytest.mark.parametrize(
    "key,value",
    (
        ("user", "kunlun_vault_executor"),
        ("password", "other-secret"),
        ("host", "other.example"),
        ("hostaddr", "127.0.0.1"),
        ("port", "6543"),
        ("dbname", "other_database"),
        ("options", "-csearch_path=public"),
        ("service", "other"),
        ("servicefile", "/tmp/other-service"),
        ("application_name", "unexpected"),
    ),
)
def test_production_database_url_rejects_query_parameter_overrides(key, value):
    base = _production_database_url()
    overridden = base.replace("?sslmode=", f"?{key}={value}&sslmode=")
    assert not _production_database_url_is_safe(overridden)


def test_production_database_url_rejects_duplicate_tls_query_parameters():
    base = _production_database_url()
    duplicate_mode = base.replace(
        "?sslmode=verify-full", "?sslmode=verify-full&sslmode=verify-full",
    )
    duplicate_certificate = base.replace(
        "&sslrootcert=", "&sslrootcert=/tmp/other.pem&sslrootcert=",
    )
    assert not _production_database_url_is_safe(duplicate_mode)
    assert not _production_database_url_is_safe(duplicate_certificate)


def test_production_app_factory_rejects_injected_adapters(monkeypatch):
    settings = Settings(
        environment="production",
        gateway_mode="disabled",
        database_url=_production_database_url("kunlun_runtime"),
        vault_executor_database_url=_production_database_url("kunlun_vault_executor", "executor-secret"),
        api_key_pepper="a" * 32,
        session_pepper="b" * 32,
        api_key_pepper_persisted=True,
        session_pepper_persisted=True,
        trusted_proxy_cidrs={"127.0.0.1/32"},
    )
    settings.validate()
    monkeypatch.setattr(
        Settings,
        "from_env",
        classmethod(lambda cls, **_kwargs: settings),
    )
    for injected in (
        {"provider_clients": []},
        {"provider_clients": [object()]},
        {"live_payment_bridge": object()},
        {"content_safety_adapter": object()},
        {"captcha_adapter": object()},
        {"identity_sender": object()},
    ):
        with pytest.raises(RuntimeError, match="生产环境禁止注入"):
            create_app(**injected)


def test_unsafe_feature_flag_combinations_are_rejected():
    with pytest.raises(RuntimeError, match="回调密钥"):
        Settings.from_env(enable_test_payments=True, payment_webhook_secret="short")
    with pytest.raises(RuntimeError, match="仅允许在 production"):
        Settings.from_env(live_payments=True)
    with pytest.raises(RuntimeError, match="仅允许在 production"):
        Settings.from_env(live_upstream=True, providers=[])
    with pytest.raises(RuntimeError, match="仅允许在 production"):
        Settings.from_env(environment="test", live_upstream=True)


def test_environment_allowlist_rejects_typos_and_staging_is_quarantined():
    with pytest.raises(RuntimeError, match="KUNLUN_ENV"):
        Settings.from_env(environment="prod")
    with pytest.raises(RuntimeError, match="staging 环境禁止"):
        Settings.from_env(environment="staging", public_signup=True)
    with pytest.raises(RuntimeError, match="staging 环境禁止"):
        Settings.from_env(environment="staging", live_upstream=True)
    with pytest.raises(RuntimeError, match="staging 环境禁止"):
        Settings.from_env(environment="staging", live_payments=True)
    with pytest.raises(RuntimeError, match="staging 环境禁止"):
        Settings.from_env(environment="staging", enable_test_payments=True)
    with pytest.raises(RuntimeError, match="legacy_test"):
        Settings.from_env(environment="staging")


def test_vercel_production_requires_explicit_matching_kunlun_environment(monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "production")
    with pytest.raises(RuntimeError, match="KUNLUN_ENV=production"):
        Settings.from_env()
    settings = Settings.from_env(
        environment="production",
        gateway_mode="disabled",
        database_url=_production_database_url(),
        api_key_pepper="a" * 32,
        session_pepper="b" * 32,
        api_key_pepper_persisted=True,
        session_pepper_persisted=True,
        trusted_proxy_cidrs={"127.0.0.1/32"},
    )
    assert settings.environment == "production"


def test_vercel_preview_cannot_be_treated_as_a_real_byok_deployment(monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "preview")
    with pytest.raises(RuntimeError, match="Vercel Preview/Development"):
        Settings.from_env(
            environment="test",
            gateway_mode="byok",
            vault_backend="disabled",
            providers=[{"name": "openai", "base_url": "https://api.openai.example/v1"}],
            provider_host_allowlist={"api.openai.example"},
        )


def test_vercel_ingress_requires_distinct_persisted_secrets(monkeypatch, tmp_path):
    proxy_file = tmp_path / "proxy"
    cron_file = tmp_path / "cron"
    ops_file = tmp_path / "ops"
    operator_file = tmp_path / "operator"
    proxy_file.write_text("p" * 32, encoding="utf-8")
    cron_file.write_text("c" * 32, encoding="utf-8")
    ops_file.write_text("i" * 32, encoding="utf-8")
    operator_file.write_text("o" * 32, encoding="utf-8")
    monkeypatch.setenv("KUNLUN_INGRESS_PROVIDER", "vercel")
    monkeypatch.setenv("KUNLUN_TRUSTED_PROXY_SECRET_FILE", str(proxy_file))
    monkeypatch.setenv("CRON_SECRET_FILE", str(cron_file))
    monkeypatch.setenv("KUNLUN_OPS_INGRESS_SECRET_FILE", str(ops_file))
    monkeypatch.setenv("KUNLUN_OPERATOR_SIGNING_SECRET_FILE", str(operator_file))
    settings = Settings.from_env()
    assert settings.ops_ingress_secret_persisted is True
    monkeypatch.setenv("KUNLUN_OPS_INGRESS_SECRET", "p" * 32)
    monkeypatch.delenv("KUNLUN_OPS_INGRESS_SECRET_FILE")
    with pytest.raises(RuntimeError, match="必须与其他 Vercel/运维密钥不同"):
        Settings.from_env()


def test_legacy_operator_token_is_local_test_only():
    with pytest.raises(RuntimeError, match="仅允许在 development/test"):
        Settings.from_env(environment="staging", operator_token="o" * 32)
    with pytest.raises(RuntimeError, match="仅允许在 development/test"):
        Settings.from_env(
            environment="production", gateway_mode="disabled", operator_token="o" * 32,
        )


def test_production_public_signup_is_blocked_until_identity_and_content_gates_exist():
    provider = {
        "name": "primary",
        "base_url": "https://provider.example/v1",
        "api_key_env": "PROVIDER_KEY",
    }
    with pytest.raises(RuntimeError, match="production 不提供公共注册"):
        Settings.from_env(
            environment="production",
            gateway_mode="disabled",
            database_url=_production_database_url(),
            api_key_pepper="a" * 32,
            session_pepper="b" * 32,
            api_key_pepper_persisted=True,
            session_pepper_persisted=True,
            public_signup=True,
            live_upstream=True,
            providers=[provider],
            provider_host_allowlist={"provider.example"},
            operator_token="o" * 32,
            terms_url="https://gateway.example/terms",
            privacy_url="https://gateway.example/privacy",
            complaint_email="complaints@example.com",
            compliance_acknowledged=True,
        )


def test_production_flags_can_pass_only_with_all_adapter_and_private_ops_gates():
    provider = {
        "name": "primary",
        "base_url": "https://provider.example/v1",
        "api_key_env": "PROVIDER_KEY",
        "models": ["model-a"],
        "pricing": {"model-a": {
            "input_microusd_per_million": 1_200_000,
            "output_microusd_per_million": 3_600_000,
        }},
    }
    settings = Settings.from_env(
        environment="production",
        gateway_mode="disabled",
        database_url=_production_database_url("kunlun_runtime"),
        api_key_pepper="a" * 32,
        session_pepper="b" * 32,
        identity_token_pepper="c" * 32,
        api_key_pepper_persisted=True,
        session_pepper_persisted=True,
        identity_token_pepper_persisted=True,
        public_signup=False,
        require_email_verification=True,
        smtp_url="smtps://mailer:password@smtp.example:465",
        email_from="gateway@example.com",
        public_base_url="https://gateway.example",
        captcha_required=True,
        captcha_provider="turnstile",
        captcha_site_key="public-site-key",
        captcha_endpoint="https://challenges.cloudflare.com/turnstile/v0/siteverify",
        captcha_secret="captcha-secret",
        captcha_host_allowlist={"challenges.cloudflare.com"},
        captcha_expected_hostname="gateway.example",
        trusted_proxy_cidrs={"172.30.50.2/32"},
        live_upstream=True,
        models={"model-a": {
            "input_microusd_per_million": 1_500_000,
            "output_microusd_per_million": 4_000_000,
            "max_output_tokens": 4096,
        }},
        model_catalog_explicit=True,
        providers=[provider],
        provider_host_allowlist={"provider.example"},
        content_safety_required=True,
        content_safety_endpoint="https://safety.example/check",
        content_safety_api_key="safety-secret",
        content_safety_host_allowlist={"safety.example"},
        operator_signing_secret="o" * 32,
        operator_signing_secret_persisted=True,
        ops_private_access_acknowledged=True,
        terms_url="https://gateway.example/terms",
        privacy_url="https://gateway.example/privacy",
        complaint_email="complaints@example.com",
        compliance_acknowledged=True,
    )
    assert settings.public_signup is False and settings.live_upstream


def test_production_live_upstream_requires_explicit_catalog_routes_and_costs():
    base = dict(
        environment="production",
        gateway_mode="disabled",
        database_url=_production_database_url("kunlun_runtime"),
        api_key_pepper="a" * 32,
        session_pepper="b" * 32,
        api_key_pepper_persisted=True,
        session_pepper_persisted=True,
        trusted_proxy_cidrs={"127.0.0.1/32"},
        live_upstream=True,
        provider_host_allowlist={"provider.example"},
        operator_signing_secret="o" * 32,
        operator_signing_secret_persisted=True,
        ops_private_access_acknowledged=True,
    )
    provider = {
        "name": "primary",
        "base_url": "https://provider.example/v1",
        "api_key_env": "PROVIDER_KEY",
    }
    with pytest.raises(RuntimeError, match="KUNLUN_MODELS_JSON"):
        Settings.from_env(**base, providers=[provider])

    catalog = {"model-a": {
        "input_microusd_per_million": 1_500_000,
        "output_microusd_per_million": 4_000_000,
        "max_output_tokens": 4096,
    }}
    with pytest.raises(RuntimeError, match="显式模型列表"):
        Settings.from_env(
            **base, providers=[provider], models=catalog, model_catalog_explicit=True,
        )

    routed = {**provider, "models": ["model-a"]}
    with pytest.raises(RuntimeError, match="完整上游价格"):
        Settings.from_env(
            **base, providers=[routed], models=catalog, model_catalog_explicit=True,
        )

    complete = {**routed, "pricing": {"model-a": {
        "input_microusd_per_million": 1_200_000,
        "output_microusd_per_million": 3_600_000,
    }}}
    settings = Settings.from_env(
        **base, providers=[complete], models=catalog, model_catalog_explicit=True,
    )
    assert settings.models == catalog


def test_production_byok_requires_explicit_catalog_costs_and_private_ops():
    base = dict(
        environment="production",
        database_url=_production_database_url("kunlun_runtime"),
        vault_executor_database_url=_production_database_url("kunlun_vault_executor", "executor-secret"),
        api_key_pepper="a" * 32,
        session_pepper="b" * 32,
        api_key_pepper_persisted=True,
        session_pepper_persisted=True,
        trusted_proxy_cidrs={"127.0.0.1/32"},
        gateway_mode="byok",
        vault_backend="supabase_vault",
        provider_host_allowlist={"api.openai.com"},
    )
    provider = {
        "name": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["model-a"],
        "pricing": {"model-a": {
            "input_microusd_per_million": 1_200_000,
            "output_microusd_per_million": 3_600_000,
        }},
    }
    catalog = {"model-a": {
        "input_microusd_per_million": 1_500_000,
        "output_microusd_per_million": 4_000_000,
        "max_output_tokens": 4096,
    }}

    with pytest.raises(RuntimeError, match="KUNLUN_MODELS_JSON"):
        Settings.from_env(**base, providers=[provider])

    with pytest.raises(RuntimeError, match="KUNLUN_OPERATOR_SIGNING_SECRET"):
        Settings.from_env(
            **base,
            providers=[provider],
            models=catalog,
            model_catalog_explicit=True,
        )

    with pytest.raises(RuntimeError, match="api_key_env"):
        Settings.from_env(
            **base,
            providers=[{**provider, "api_key_env": "SHARED_PROVIDER_KEY"}],
            models=catalog,
            model_catalog_explicit=True,
            operator_signing_secret="o" * 32,
            operator_signing_secret_persisted=True,
            ops_private_access_acknowledged=True,
        )

    settings = Settings.from_env(
        **base,
        providers=[provider],
        models=catalog,
        model_catalog_explicit=True,
        operator_signing_secret="o" * 32,
        operator_signing_secret_persisted=True,
        ops_private_access_acknowledged=True,
    )
    assert settings.gateway_mode == "byok"
    assert settings.live_upstream is False

    with pytest.raises(RuntimeError, match="预授权上限"):
        Settings.from_env(
            **base,
            providers=[{**provider, "pricing": {"model-a": {
                "input_microusd_per_million": 1_500_001,
                "output_microusd_per_million": 4_000_000,
            }}}],
            models=catalog,
            model_catalog_explicit=True,
            operator_signing_secret="o" * 32,
            operator_signing_secret_persisted=True,
            ops_private_access_acknowledged=True,
        )

    safe = dict(
        **base,
        providers=[provider],
        models=catalog,
        model_catalog_explicit=True,
        operator_signing_secret="o" * 32,
        operator_signing_secret_persisted=True,
        ops_private_access_acknowledged=True,
    )
    with pytest.raises(RuntimeError, match="production 不提供公共注册"):
        Settings.from_env(**safe, public_signup=True)
    with pytest.raises(RuntimeError, match="余额或充值套餐"):
        Settings.from_env(**safe, topup_packages={"forbidden": {
            "payment_amount_minor": 100,
            "payment_currency": "USD",
            "credit_amount_microusd": 1_000,
        }})


def test_production_byok_rejects_runtime_and_executor_shared_database_credential():
    with pytest.raises(RuntimeError, match="数据库凭据不得重复"):
        Settings.from_env(
            environment="production", gateway_mode="byok", vault_backend="supabase_vault",
            database_url=_production_database_url("kunlun_runtime", "shared-secret"),
            vault_executor_database_url=_production_database_url("kunlun_vault_executor", "shared-secret"),
            api_key_pepper="a" * 32, session_pepper="b" * 32,
            api_key_pepper_persisted=True, session_pepper_persisted=True,
            trusted_proxy_cidrs={"127.0.0.1/32"}, provider_host_allowlist={"api.openai.com"},
        )


def test_production_byok_rejects_query_password_override_even_when_authority_passwords_differ():
    runtime = _production_database_url("kunlun_runtime", "runtime-secret").replace(
        "?sslmode=", "?password=shared-query-secret&sslmode=",
    )
    executor = _production_database_url("kunlun_vault_executor", "executor-secret").replace(
        "?sslmode=", "?password=shared-query-secret&sslmode=",
    )
    with pytest.raises(RuntimeError, match="verify-full"):
        Settings.from_env(
            environment="production", gateway_mode="byok", vault_backend="supabase_vault",
            database_url=runtime, vault_executor_database_url=executor,
            api_key_pepper="a" * 32, session_pepper="b" * 32,
            api_key_pepper_persisted=True, session_pepper_persisted=True,
            trusted_proxy_cidrs={"127.0.0.1/32"}, provider_host_allowlist={"api.openai.com"},
        )


def test_production_byok_database_roles_only_normalize_valid_supavisor_usernames():
    base = dict(
        environment="production", gateway_mode="byok", vault_backend="supabase_vault",
        api_key_pepper="a" * 32, session_pepper="b" * 32,
        api_key_pepper_persisted=True, session_pepper_persisted=True,
        trusted_proxy_cidrs={"127.0.0.1/32"}, provider_host_allowlist={"api.openai.com"},
        providers=[{
            "name": "openai", "base_url": "https://api.openai.com/v1",
            "models": ["model-a"],
            "pricing": {
                "model-a": {
                    "input_microusd_per_million": 1_200_000,
                    "output_microusd_per_million": 3_600_000,
                },
            },
        }],
        models={
            "model-a": {
                "input_microusd_per_million": 1_500_000,
                "output_microusd_per_million": 4_000_000,
                "max_output_tokens": 4096,
            },
        },
        model_catalog_explicit=True,
        operator_signing_secret="o" * 32,
        operator_signing_secret_persisted=True,
        ops_private_access_acknowledged=True,
    )
    with pytest.raises(RuntimeError, match="必须使用 kunlun_vault_executor"):
        Settings.from_env(
            **base,
            database_url=_production_database_url("kunlun_runtime.untrusted", "runtime-secret"),
            vault_executor_database_url=_production_database_url(
                "kunlun_vault_executor.untrusted", "executor-secret",
            ),
        )

    project_ref = "oyhavtaalkidrllxfigw"
    runtime = _production_database_url(
        f"kunlun_runtime.{project_ref}", "runtime-secret",
    ).replace("@db.oyhavtaalkidrllxfigw.supabase.co/", "@aws-0-ca-central-1.pooler.supabase.com/")
    executor = _production_database_url(
        f"kunlun_vault_executor.{project_ref}", "executor-secret",
    ).replace("@db.oyhavtaalkidrllxfigw.supabase.co/", "@aws-0-ca-central-1.pooler.supabase.com/")
    settings = Settings.from_env(
        **base,
        database_url=runtime,
        vault_executor_database_url=executor,
    )
    assert settings.gateway_mode == "byok"

    other_project = executor.replace(project_ref, "abcdefghijklmnopqrst")
    with pytest.raises(RuntimeError, match="Supabase project/database"):
        Settings.from_env(
            **base,
            database_url=runtime,
            vault_executor_database_url=other_project,
        )


def test_byok_is_rejected_outside_production_or_explicit_test_vault(tmp_path, monkeypatch):
    provider = [{
        "name": "openai", "base_url": "https://api.openai.example/v1", "models": ["test-model"],
    }]
    with pytest.raises(RuntimeError, match="production 或 test"):
        Settings.from_env(
            environment="development", gateway_mode="byok", providers=provider,
            provider_host_allowlist={"api.openai.example"},
        )
    monkeypatch.setenv("KUNLUN_PROVIDERS_JSON", json.dumps(provider))
    monkeypatch.setenv("KUNLUN_PROVIDER_HOST_ALLOWLIST", "api.openai.example")
    with pytest.raises(RuntimeError, match="显式注入 CredentialVault"):
        create_app(
            database_url=f"sqlite:///{tmp_path / 'byok-test-vault.sqlite'}",
            environment="test", gateway_mode="byok",
        )


def test_production_public_signup_is_unconditionally_rejected():
    base = dict(
        environment="production",
        gateway_mode="disabled",
        database_url=_production_database_url("kunlun_runtime"),
        api_key_pepper="a" * 32,
        session_pepper="b" * 32,
        identity_token_pepper="c" * 32,
        api_key_pepper_persisted=True,
        session_pepper_persisted=True,
        identity_token_pepper_persisted=True,
        public_signup=True,
        require_email_verification=True,
        smtp_url="smtps://mailer:password@smtp.example:465",
        email_from="gateway@example.com",
        public_base_url="https://gateway.example",
        captcha_required=True,
        captcha_secret="captcha-secret",
        captcha_expected_hostname="gateway.example",
        trusted_proxy_cidrs={"172.30.50.2/32"},
        terms_url="https://gateway.example/terms",
        privacy_url="https://gateway.example/privacy",
        complaint_email="complaints@example.com",
        compliance_acknowledged=True,
    )
    with pytest.raises(RuntimeError, match="production 不提供公共注册"):
        Settings.from_env(
            **base,
            captcha_endpoint="https://captcha.example/verify",
            captcha_host_allowlist={"captcha.example"},
        )
    with pytest.raises(RuntimeError, match="production 不提供公共注册"):
        Settings.from_env(
            **base,
            captcha_provider="turnstile",
            captcha_site_key="public-site-key",
            captcha_endpoint="https://evil.example/siteverify",
            captcha_host_allowlist={"evil.example"},
        )
    with pytest.raises(RuntimeError, match="production 不提供公共注册"):
        Settings.from_env(
            **{**base, "captcha_expected_hostname": "other.example"},
            captcha_provider="turnstile",
            captcha_site_key="public-site-key",
            captcha_endpoint="https://challenges.cloudflare.com/turnstile/v0/siteverify",
            captcha_host_allowlist={"challenges.cloudflare.com"},
        )


def test_production_rejects_live_payment_and_topup_configuration():
    with pytest.raises(RuntimeError, match="关闭 KUNLUN_LIVE_PAYMENTS"):
        Settings.from_env(
        environment="production",
        database_url=_production_database_url("kunlun_runtime"),
        api_key_pepper="a" * 32,
        session_pepper="b" * 32,
        api_key_pepper_persisted=True,
        session_pepper_persisted=True,
        trusted_proxy_cidrs={"172.30.50.2/32"},
        live_payments=True,
        payment_bridge_endpoint="https://payment-bridge.example/api",
        payment_bridge_merchant_id="merchant-1",
        payment_bridge_secret="p" * 32,
        payment_bridge_host_allowlist={"payment-bridge.example"},
        payment_provider="wechatpay",
        payment_bridge_official_sdk_acknowledged=True,
        operator_signing_secret="o" * 32,
        operator_signing_secret_persisted=True,
        ops_private_access_acknowledged=True,
        topup_packages={"starter": {
            "payment_amount_minor": 1999,
            "payment_currency": "CNY",
            "credit_amount_microusd": 250000,
        }},
        gateway_mode="disabled",
        )


def test_invalid_provider_json_is_rejected(monkeypatch):
    monkeypatch.setenv("KUNLUN_PROVIDERS_JSON", "not-json")
    with pytest.raises(RuntimeError, match="不是有效 JSON"):
        Settings.from_env()


def test_openai_provider_success_and_status_error_are_sanitized():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["Authorization"]
        payload = json.loads(request.content)
        if payload["model"] == "rate-limited":
            return httpx.Response(429, json={"upstream": "must not escape"})
        return httpx.Response(200, json={"id": "ok", "choices": []})

    provider = OpenAICompatibleProvider(
        provider_name="primary",
        base_url="https://provider.example/v1",
        api_key="provider-secret",
        models={"test-model", "rate-limited"},
        transport=httpx.MockTransport(handler),
    )
    assert asyncio.run(provider({"model": "test-model", "messages": []}))["id"] == "ok"
    assert seen["authorization"] == "Bearer provider-secret"
    with pytest.raises(ProviderError) as exc_info:
        asyncio.run(provider({"model": "rate-limited", "messages": []}))
    assert exc_info.value.status_code == 429
    assert str(exc_info.value) == "provider_http_429"
    assert supports_model(provider, "test-model") is True
    assert supports_model(provider, "unknown") is False


@pytest.mark.parametrize(
    "raised,category,safe,billable",
    [
        (httpx.ConnectError("connect"), "provider_connect_failure", True, False),
        (httpx.ReadTimeout("read"), "provider_ambiguous_timeout", False, True),
    ],
)
def test_provider_transport_failures_have_explicit_routing_semantics(raised, category, safe, billable):
    def handler(request: httpx.Request) -> httpx.Response:
        raise raised

    provider = OpenAICompatibleProvider(
        provider_name="primary",
        base_url="https://provider.example/v1",
        api_key="secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderError) as exc_info:
        asyncio.run(provider({"model": "test-model", "messages": []}))
    assert exc_info.value.category == category
    assert exc_info.value.safe_to_failover is safe
    assert exc_info.value.request_may_be_billable is billable


@pytest.mark.parametrize(
    "status,safe,billable",
    [(429, True, False), (500, False, True), (502, False, True), (503, False, True)],
)
def test_provider_http_statuses_have_conservative_billing_semantics(status, safe, billable):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"private": "must not escape"})

    provider = OpenAICompatibleProvider(
        provider_name="primary",
        base_url="https://provider.example/v1",
        api_key="secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderError) as exc_info:
        asyncio.run(provider({"model": "test-model", "messages": []}))
    assert exc_info.value.safe_to_failover is safe
    assert exc_info.value.request_may_be_billable is billable


def test_custom_5xx_category_remains_billable_by_default():
    error = ProviderError(504, category="provider_custom_timeout")
    assert error.safe_to_failover is False
    assert error.request_may_be_billable is True


def test_provider_response_limit_is_enforced_while_streaming():
    provider = OpenAICompatibleProvider(
        provider_name="primary",
        base_url="https://provider.example/v1",
        api_key="secret",
        max_response_bytes=32,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"x" * 33),
        ),
    )
    with pytest.raises(ProviderError) as exc_info:
        asyncio.run(provider({"model": "test-model", "messages": []}))
    assert exc_info.value.category == "provider_response_too_large"
    assert exc_info.value.request_may_be_billable is True


def test_provider_config_references_environment_secret(monkeypatch):
    config = [{
        "name": "primary",
        "base_url": "https://provider.example/v1",
        "api_key_env": "TEST_PROVIDER_KEY",
        "models": ["test-model"],
    }]
    with pytest.raises(RuntimeError, match="环境变量未设置"):
        build_provider_clients(config)
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret-from-env")
    clients = build_provider_clients(config)
    assert len(clients) == 1
    assert isinstance(clients[0], OpenAICompatibleProvider)
    assert clients[0].api_key == "secret-from-env"


def test_provider_config_rejects_insecure_public_endpoint(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    with pytest.raises(RuntimeError, match="HTTPS"):
        build_provider_clients([{
            "name": "unsafe",
            "base_url": "http://public.example/v1",
            "api_key_env": "TEST_PROVIDER_KEY",
        }])


def test_provider_config_enforces_exact_egress_allowlist(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    config = [{
        "name": "primary",
        "base_url": "https://provider.example/v1",
        "api_key_env": "TEST_PROVIDER_KEY",
    }]
    with pytest.raises(RuntimeError, match="允许列表"):
        build_provider_clients(config, allowed_hosts={"other.example"})
    assert len(build_provider_clients(config, allowed_hosts={"provider.example"})) == 1


@pytest.mark.parametrize(
    "name,base_url,host",
    [
        ("openai", "https://api.openai.com/v1", "api.openai.com"),
        ("deepseek", "https://api.deepseek.com/v1", "api.deepseek.com"),
        (
            "google",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
            "generativelanguage.googleapis.com",
        ),
    ],
)
def test_byok_provider_is_bound_to_reviewed_official_endpoint(name, base_url, host):
    client = build_byok_provider_client(
        {"name": name, "base_url": base_url, "models": ["model-a"]},
        api_key="customer-key",
        allowed_hosts={host},
    )
    assert client.provider_name == name


@pytest.mark.parametrize(
    "name,base_url,hosts",
    [
        ("openai", "https://evil.example/v1", {"evil.example"}),
        ("openai", "https://api.openai.com/compatible", {"api.openai.com"}),
        ("anthropic", "https://api.anthropic.com/v1", {"api.anthropic.com"}),
        ("google", "https://generativelanguage.googleapis.com/v1", {"generativelanguage.googleapis.com"}),
    ],
)
def test_byok_provider_rejects_unbound_or_unreleased_endpoint(name, base_url, hosts):
    with pytest.raises(RuntimeError, match="BYOK Provider 目录无效"):
        build_byok_provider_client(
            {"name": name, "base_url": base_url, "models": ["model-a"]},
            api_key="customer-key",
            allowed_hosts=hosts,
        )


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("connect_timeout_seconds", 0, "连接超时"),
        ("read_timeout_seconds", 601, "读取超时"),
        ("pricing", {"test-model": {"input_microusd_per_million": -1}}, "价格"),
    ],
)
def test_provider_config_rejects_unsafe_numeric_ranges(monkeypatch, field, value, match):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "secret")
    config = {
        "name": "primary",
        "base_url": "https://provider.example/v1",
        "api_key_env": "TEST_PROVIDER_KEY",
    }
    config[field] = value
    with pytest.raises(RuntimeError, match=match):
        build_provider_clients([config])


def test_price_changes_create_versions_instead_of_rewriting_history(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'prices.sqlite3'}"
    first_prices = {
        "versioned-model": {
            "input_microusd_per_million": 100,
            "output_microusd_per_million": 200,
            "max_output_tokens": 1024,
        }
    }
    monkeypatch.setenv("KUNLUN_MODELS_JSON", json.dumps(first_prices))
    first_app = create_app(database_url=database_url, provider_clients=[])
    first_app.state.engine.dispose()
    first_prices["versioned-model"]["output_microusd_per_million"] = 300
    monkeypatch.setenv("KUNLUN_MODELS_JSON", json.dumps(first_prices))
    second_app = create_app(database_url=database_url, provider_clients=[])
    with second_app.state.SessionLocal() as session:
        versions = session.scalars(select(ModelPrice).where(
            ModelPrice.model == "versioned-model",
        ).order_by(ModelPrice.version)).all()
        assert [(item.version, item.output_microusd_per_million, item.active) for item in versions] == [
            (1, 200, False),
            (2, 300, True),
        ]
    second_app.state.engine.dispose()
