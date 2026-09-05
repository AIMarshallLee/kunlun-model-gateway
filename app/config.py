"""Configuration with fail-closed production validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import ip_network
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError


TURNSTILE_SITEVERIFY_ENDPOINT = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
SUPPORTED_ENVIRONMENTS = frozenset({"development", "test", "staging", "production"})
LEGACY_OPERATOR_ENVIRONMENTS = frozenset({"development", "test"})
BYOK_PROVIDER_ENDPOINTS: dict[str, tuple[str, frozenset[str]]] = {
    "openai": ("api.openai.com", frozenset({"/v1"})),
    "deepseek": ("api.deepseek.com", frozenset({"", "/v1"})),
    "google": (
        "generativelanguage.googleapis.com",
        frozenset({"/v1beta/openai"}),
    ),
}
BYOK_PROVIDER_CATALOG = frozenset(BYOK_PROVIDER_ENDPOINTS)


def validate_byok_provider_endpoint(name: str, base_url: str) -> None:
    """Bind every BYOK provider name to its reviewed official endpoint."""
    expected = BYOK_PROVIDER_ENDPOINTS.get(name.strip().casefold())
    try:
        parsed = urlparse(base_url.strip())
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("BYOK Provider 官方端点无效") from exc
    normalized_path = parsed.path.rstrip("/")
    if (
        expected is None
        or parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != expected[0]
        or port not in {None, 443}
        or normalized_path not in expected[1]
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("BYOK Provider 必须使用已审核的官方兼容端点")


def _production_database_url_is_safe(value: str) -> bool:
    """Require one unambiguous authenticated PostgreSQL TLS connection.

    psycopg accepts libpq connection parameters in a URL query string.  Some
    of them override the authority component, so validating only ``urlparse``
    would validate a different target than the driver connects to.  Production
    accepts exactly the two TLS parameters below and then compares the parsed
    authority to SQLAlchemy's real psycopg connect arguments.
    """
    try:
        parsed = urlparse(value)
        query = [
            (key.casefold(), item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        ]
        allowed_query_keys = {"sslmode", "sslrootcert"}
        query_keys = [key for key, _item in query]
        if (
            len(query_keys) != len(set(query_keys))
            or set(query_keys) != allowed_query_keys
        ):
            return False
        query_values = dict(query)
        certificate = Path(query_values["sslrootcert"])
        connect_args, connect_kwargs = PGDialect_psycopg().create_connect_args(make_url(value))
        expected_connect_values = {
            "user": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
            "host": parsed.hostname or "",
            "dbname": _database_name(value),
            "sslmode": "verify-full",
            "sslrootcert": query_values["sslrootcert"],
        }
        return bool(
            parsed.scheme == "postgresql+psycopg"
            and parsed.hostname
            and parsed.username
            and parsed.password
            and parsed.path not in {"", "/"}
            and not parsed.fragment
            and query_values["sslmode"].casefold() == "verify-full"
            and certificate.is_absolute()
            and certificate.is_file()
            and os.access(certificate, os.R_OK)
            and not connect_args
            and all(
                connect_kwargs.get(name) == expected
                for name, expected in expected_connect_values.items()
            )
            and connect_kwargs.get("port") == parsed.port
        )
    except (SQLAlchemyError, TypeError, ValueError, OSError):
        return False


def _database_password(value: str) -> str:
    """Return only a decoded password for equality checks; never log it."""
    try:
        return unquote(urlparse(value).password or "")
    except (TypeError, ValueError):
        return ""


def _database_user(value: str) -> str:
    """Normalize only Supavisor's documented role.project-ref usernames."""
    try:
        parsed = urlparse(value)
        username = unquote(parsed.username or "")
        hostname = (parsed.hostname or "").casefold()
    except (TypeError, ValueError):
        return ""
    if hostname.endswith(".pooler.supabase.com"):
        role, separator, project_ref = username.rpartition(".")
        if separator and role and re.fullmatch(r"[a-z0-9]{20}", project_ref):
            return role
    return username


def _database_name(value: str) -> str:
    """Return a single, decoded PostgreSQL database name."""
    try:
        path = urlparse(value).path
    except (TypeError, ValueError):
        return ""
    if not path.startswith("/") or "/" in path[1:]:
        return ""
    name = unquote(path[1:])
    return name if name and "/" not in name else ""


def _supabase_project_ref(value: str) -> str:
    """Identify one managed Supabase project through direct or pooler URLs."""
    try:
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").casefold()
        username = unquote(parsed.username or "")
    except (TypeError, ValueError):
        return ""
    direct = re.fullmatch(r"db\.([a-z0-9]{20})\.supabase\.co", hostname)
    if direct:
        return direct.group(1)
    if hostname.endswith(".pooler.supabase.com"):
        _role, separator, project_ref = username.rpartition(".")
        if separator and re.fullmatch(r"[a-z0-9]{20}", project_ref):
            return project_ref
    return ""


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_secret(name: str) -> tuple[str, bool]:
    """Load a secret directly or through NAME_FILE without printing it."""
    direct = os.getenv(name)
    file_name = os.getenv(f"{name}_FILE")
    if direct and file_name:
        raise RuntimeError(f"{name} 与 {name}_FILE 不能同时设置")
    if file_name:
        path = Path(file_name)
        if not path.is_absolute() or not path.is_file():
            raise RuntimeError(f"{name}_FILE 必须指向可读的绝对文件")
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"无法读取 {name}_FILE") from exc
        return value, bool(value)
    return direct or "", bool(direct)


@dataclass(slots=True)
class Settings:
    database_url: str = "sqlite:///./kunlun-gateway.sqlite3"
    environment: str = "development"
    public_signup: bool = False
    enable_test_payments: bool = False
    live_payments: bool = False
    live_upstream: bool = False
    gateway_mode: str = "legacy_test"
    platform_daily_budget_microusd: int = 0
    supplier_use_acknowledged: bool = False
    vault_backend: str = "disabled"
    vault_executor_database_url: str = ""
    payment_webhook_secret: str = ""
    api_key_pepper: str = field(default_factory=lambda: secrets.token_hex(32))
    session_pepper: str = field(default_factory=lambda: secrets.token_hex(32))
    identity_token_pepper: str = field(default_factory=lambda: secrets.token_hex(32))
    api_key_pepper_persisted: bool = False
    session_pepper_persisted: bool = False
    identity_token_pepper_persisted: bool = False
    require_email_verification: bool = False
    smtp_url: str = ""
    email_from: str = ""
    public_base_url: str = ""
    max_active_api_keys: int = 5
    captcha_required: bool = False
    captcha_provider: str = ""
    captcha_site_key: str = ""
    captcha_expected_hostname: str = ""
    captcha_endpoint: str = ""
    captcha_secret: str = ""
    captcha_host_allowlist: set[str] = field(default_factory=set)
    trusted_proxy_cidrs: set[str] = field(default_factory=set)
    trusted_proxy_secret: str = ""
    trusted_proxy_secret_persisted: bool = False
    ingress_provider: str = ""
    cron_secret: str = ""
    cron_secret_persisted: bool = False
    ops_ingress_secret: str = ""
    ops_ingress_secret_persisted: bool = False
    content_safety_required: bool = False
    content_safety_endpoint: str = ""
    content_safety_api_key: str = ""
    content_safety_host_allowlist: set[str] = field(default_factory=set)
    content_safety_policy_version: str = ""
    operator_token: str = ""
    operator_signing_secret: str = ""
    operator_signing_secret_persisted: bool = False
    ops_private_access_acknowledged: bool = False
    payment_bridge_endpoint: str = ""
    payment_bridge_merchant_id: str = ""
    payment_bridge_secret: str = ""
    payment_bridge_host_allowlist: set[str] = field(default_factory=set)
    payment_provider: str = ""
    payment_bridge_official_sdk_acknowledged: bool = False
    topup_packages: dict[str, dict[str, Any]] = field(default_factory=dict)
    rate_limit_per_minute: int = 60
    checkout_rate_limit_per_minute: int = 5
    max_open_checkout_orders: int = 3
    model_reservation_lease_seconds: int = 300
    max_output_tokens: int = 4096
    default_output_tokens: int = 256
    terms_url: str = ""
    privacy_url: str = ""
    complaint_email: str = ""
    compliance_acknowledged: bool = False
    providers: list[dict[str, Any]] = field(default_factory=list)
    provider_host_allowlist: set[str] = field(default_factory=set)
    model_catalog_explicit: bool = False
    models: dict[str, dict[str, Any]] = field(default_factory=lambda: {
        "test-model": {
            "input_microusd_per_million": 1_000_000,
            "output_microusd_per_million": 1_000_000,
            "max_output_tokens": 4096,
        }
    })

    @classmethod
    def from_env(cls, **overrides: Any) -> "Settings":
        provider_json = os.getenv("KUNLUN_PROVIDERS_JSON", "[]")
        model_json = os.getenv("KUNLUN_MODELS_JSON", "")
        try:
            providers = json.loads(provider_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("KUNLUN_PROVIDERS_JSON 不是有效 JSON") from exc
        models: dict[str, dict[str, Any]] | None = None
        if model_json:
            try:
                models = json.loads(model_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError("KUNLUN_MODELS_JSON 不是有效 JSON") from exc
        topup_json = os.getenv("KUNLUN_TOPUP_PACKAGES_JSON", "{}")
        try:
            topup_packages = json.loads(topup_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("KUNLUN_TOPUP_PACKAGES_JSON 不是有效 JSON") from exc
        api_key_pepper, api_key_persisted = _env_secret("KUNLUN_API_KEY_PEPPER")
        session_pepper, session_persisted = _env_secret("KUNLUN_SESSION_PEPPER")
        identity_pepper, identity_persisted = _env_secret("KUNLUN_IDENTITY_TOKEN_PEPPER")
        captcha_secret, _ = _env_secret("KUNLUN_CAPTCHA_SECRET")
        safety_key, _ = _env_secret("KUNLUN_CONTENT_SAFETY_API_KEY")
        operator_signing_secret, operator_secret_persisted = _env_secret("KUNLUN_OPERATOR_SIGNING_SECRET")
        payment_bridge_secret, _ = _env_secret("KUNLUN_PAYMENT_BRIDGE_SECRET")
        trusted_proxy_secret, trusted_proxy_secret_persisted = _env_secret(
            "KUNLUN_TRUSTED_PROXY_SECRET"
        )
        cron_secret, cron_secret_persisted = _env_secret("CRON_SECRET")
        ops_ingress_secret, ops_ingress_secret_persisted = _env_secret(
            "KUNLUN_OPS_INGRESS_SECRET"
        )
        values: dict[str, Any] = {
            "database_url": os.getenv("KUNLUN_DATABASE_URL", "sqlite:///./kunlun-gateway.sqlite3"),
            "environment": os.getenv("KUNLUN_ENV", "development"),
            "public_signup": _env_bool("KUNLUN_PUBLIC_SIGNUP"),
            "enable_test_payments": _env_bool("KUNLUN_ENABLE_TEST_PAYMENTS"),
            "live_payments": _env_bool("KUNLUN_LIVE_PAYMENTS"),
            "live_upstream": _env_bool("KUNLUN_LIVE_UPSTREAM"),
            "gateway_mode": os.getenv("KUNLUN_GATEWAY_MODE", "legacy_test"),
            "platform_daily_budget_microusd": int(os.getenv("KUNLUN_PLATFORM_DAILY_BUDGET_MICROUSD", "0")),
            "supplier_use_acknowledged": os.getenv("KUNLUN_SUPPLIER_USE_ACKNOWLEDGED", "false").lower() == "true",
            "vault_backend": os.getenv("KUNLUN_VAULT_BACKEND", "disabled"),
            "vault_executor_database_url": os.getenv("KUNLUN_VAULT_EXECUTOR_DATABASE_URL", ""),
            "payment_webhook_secret": os.getenv("KUNLUN_PAYMENT_WEBHOOK_SECRET", ""),
            "api_key_pepper": api_key_pepper or secrets.token_hex(32),
            "session_pepper": session_pepper or secrets.token_hex(32),
            "identity_token_pepper": identity_pepper or secrets.token_hex(32),
            "api_key_pepper_persisted": api_key_persisted,
            "session_pepper_persisted": session_persisted,
            "identity_token_pepper_persisted": identity_persisted,
            "require_email_verification": _env_bool("KUNLUN_REQUIRE_EMAIL_VERIFICATION"),
            "smtp_url": os.getenv("KUNLUN_SMTP_URL", ""),
            "email_from": os.getenv("KUNLUN_EMAIL_FROM", ""),
            "public_base_url": os.getenv("KUNLUN_PUBLIC_BASE_URL", ""),
            "max_active_api_keys": int(os.getenv("KUNLUN_MAX_ACTIVE_API_KEYS", "5")),
            "captcha_required": _env_bool("KUNLUN_CAPTCHA_REQUIRED"),
            "captcha_provider": os.getenv("KUNLUN_CAPTCHA_PROVIDER", ""),
            "captcha_site_key": os.getenv("KUNLUN_CAPTCHA_SITE_KEY", ""),
            "captcha_expected_hostname": os.getenv("KUNLUN_CAPTCHA_EXPECTED_HOSTNAME", ""),
            "captcha_endpoint": os.getenv("KUNLUN_CAPTCHA_ENDPOINT", ""),
            "captcha_secret": captcha_secret,
            "captcha_host_allowlist": {
                host.strip().casefold()
                for host in os.getenv("KUNLUN_CAPTCHA_HOST_ALLOWLIST", "").split(",")
                if host.strip()
            },
            "trusted_proxy_cidrs": {
                cidr.strip()
                for cidr in os.getenv("KUNLUN_TRUSTED_PROXY_CIDRS", "").split(",")
                if cidr.strip()
            },
            "trusted_proxy_secret": trusted_proxy_secret,
            "trusted_proxy_secret_persisted": trusted_proxy_secret_persisted,
            "ingress_provider": os.getenv("KUNLUN_INGRESS_PROVIDER", ""),
            "cron_secret": cron_secret,
            "cron_secret_persisted": cron_secret_persisted,
            "ops_ingress_secret": ops_ingress_secret,
            "ops_ingress_secret_persisted": ops_ingress_secret_persisted,
            "content_safety_required": _env_bool("KUNLUN_CONTENT_SAFETY_REQUIRED"),
            "content_safety_endpoint": os.getenv("KUNLUN_CONTENT_SAFETY_ENDPOINT", ""),
            "content_safety_api_key": safety_key,
            "content_safety_host_allowlist": {
                host.strip().casefold()
                for host in os.getenv("KUNLUN_CONTENT_SAFETY_HOST_ALLOWLIST", "").split(",")
                if host.strip()
            },
            "content_safety_policy_version": os.getenv("KUNLUN_CONTENT_SAFETY_POLICY_VERSION", ""),
            "operator_token": os.getenv("KUNLUN_OPERATOR_TOKEN", ""),
            "operator_signing_secret": operator_signing_secret,
            "operator_signing_secret_persisted": operator_secret_persisted,
            "ops_private_access_acknowledged": _env_bool("KUNLUN_OPS_PRIVATE_ACCESS_ACKNOWLEDGED"),
            "payment_bridge_endpoint": os.getenv("KUNLUN_PAYMENT_BRIDGE_ENDPOINT", ""),
            "payment_bridge_merchant_id": os.getenv("KUNLUN_PAYMENT_BRIDGE_MERCHANT_ID", ""),
            "payment_bridge_secret": payment_bridge_secret,
            "payment_bridge_host_allowlist": {
                host.strip().casefold()
                for host in os.getenv("KUNLUN_PAYMENT_BRIDGE_HOST_ALLOWLIST", "").split(",")
                if host.strip()
            },
            "payment_provider": os.getenv("KUNLUN_PAYMENT_PROVIDER", ""),
            "payment_bridge_official_sdk_acknowledged": _env_bool("KUNLUN_PAYMENT_BRIDGE_OFFICIAL_SDK_ACKNOWLEDGED"),
            "topup_packages": topup_packages,
            "rate_limit_per_minute": int(os.getenv("KUNLUN_RATE_LIMIT_PER_MINUTE", "60")),
            "checkout_rate_limit_per_minute": int(
                os.getenv("KUNLUN_CHECKOUT_RATE_LIMIT_PER_MINUTE", "5")
            ),
            "max_open_checkout_orders": int(
                os.getenv("KUNLUN_MAX_OPEN_CHECKOUT_ORDERS", "3")
            ),
            "model_reservation_lease_seconds": int(
                os.getenv("KUNLUN_MODEL_RESERVATION_LEASE_SECONDS", "300")
            ),
            "max_output_tokens": int(os.getenv("KUNLUN_MAX_OUTPUT_TOKENS", "4096")),
            "default_output_tokens": int(os.getenv("KUNLUN_DEFAULT_OUTPUT_TOKENS", "256")),
            "terms_url": os.getenv("KUNLUN_TERMS_URL", ""),
            "privacy_url": os.getenv("KUNLUN_PRIVACY_URL", ""),
            "complaint_email": os.getenv("KUNLUN_COMPLAINT_EMAIL", ""),
            "compliance_acknowledged": _env_bool("KUNLUN_COMPLIANCE_ACKNOWLEDGED"),
            "providers": providers,
            # This flag is derived from the actual environment value. It is
            # intentionally not a second boolean env switch that could assert
            # an explicit catalog while silently retaining the test default.
            "model_catalog_explicit": bool(model_json),
            "provider_host_allowlist": {
                host.strip().casefold()
                for host in os.getenv("KUNLUN_PROVIDER_HOST_ALLOWLIST", "").split(",")
                if host.strip()
            },
        }
        if models is not None:
            values["models"] = models
        values.update({key: value for key, value in overrides.items() if value is not None})
        settings = cls(**values)
        settings.validate()
        return settings

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    def validate(self) -> None:
        # Do not silently treat a typo such as ``prod`` or ``stagin`` as a
        # non-production environment.  Such a fallback would bypass every
        # production-only safety gate below.
        self.environment = str(self.environment).strip().casefold()
        if self.environment not in SUPPORTED_ENVIRONMENTS:
            raise RuntimeError(
                "KUNLUN_ENV 必须是 development、test、staging 或 production"
            )
        # A Vercel production deployment must not silently boot using the
        # development defaults when its project environment variables are
        # incomplete or mis-scoped.
        if os.getenv("VERCEL_ENV", "").strip().casefold() == "production" and self.environment != "production":
            raise RuntimeError("Vercel production deployment 必须显式设置 KUNLUN_ENV=production")
        if self.operator_token and self.environment == "staging":
            raise RuntimeError("KUNLUN_OPERATOR_TOKEN 仅允许在 development/test 环境兼容使用")
        if self.environment == "staging" and any(
            (self.public_signup, self.live_upstream, self.live_payments, self.enable_test_payments)
        ):
            raise RuntimeError(
                "staging 环境禁止开启 public_signup、live_upstream、live_payments 或 enable_test_payments"
            )
        if self.environment != "production" and (self.live_upstream or self.live_payments):
            raise RuntimeError("真实模型上游与真实支付仅允许在 production 环境开启")
        self.gateway_mode = str(self.gateway_mode).strip().casefold()
        self.vault_backend = str(self.vault_backend).strip().casefold()
        if self.gateway_mode not in {"disabled", "byok", "legacy_test", "managed_gateway"}:
            raise RuntimeError("KUNLUN_GATEWAY_MODE 仅支持 disabled、byok、managed_gateway 或 legacy_test")
        if self.gateway_mode == "managed_gateway":
            if not 1 <= self.platform_daily_budget_microusd <= 1_000_000_000_000:
                raise RuntimeError("平台模式必须配置正整数 KUNLUN_PLATFORM_DAILY_BUDGET_MICROUSD")
            if self.environment not in {"production", "test"}:
                raise RuntimeError("平台商业模式仅允许 production 或显式注入适配器的 test")
            if not self.require_email_verification:
                raise RuntimeError("平台商业模式必须启用邮箱验证")
            if self.is_production and (not self.live_upstream or not self.supplier_use_acknowledged):
                raise RuntimeError("平台商业模式必须明确启用上游并确认供应商商业用途依据")
            if not isinstance(self.providers, list) or not 1 <= len(self.providers) <= 3:
                raise RuntimeError("平台首发目录必须包含 1 到 3 个允许渠道")
            seen_providers = set()
            for provider in self.providers:
                if not isinstance(provider, dict) or set(provider) - {"name", "base_url", "models", "pricing"}:
                    raise RuntimeError("平台模型目录仅允许模型、端点和价格，不接受内联密钥或 api_key_env")
                name = provider.get("name")
                if not isinstance(name, str) or name not in BYOK_PROVIDER_CATALOG or name in seen_providers:
                    raise RuntimeError("平台渠道名称必须来自允许目录且不得重复")
                seen_providers.add(name)
                if not isinstance(provider.get("base_url"), str):
                    raise RuntimeError("平台渠道必须配置官方端点")
                validate_byok_provider_endpoint(name, provider["base_url"])
                models = provider.get("models")
                if not isinstance(models, list) or not models or any(not isinstance(m, str) or m not in self.models for m in models) or len(set(models)) != len(models):
                    raise RuntimeError("平台渠道模型必须来自售卖目录且不得重复")
                pricing = provider.get("pricing")
                fields = {"input_microusd_per_million", "output_microusd_per_million"}
                if not isinstance(pricing, dict) or set(pricing) != set(models):
                    raise RuntimeError("平台渠道必须为每个模型配置成本价格")
                for prices in pricing.values():
                    if not isinstance(prices, dict) or set(prices) != fields or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 1_000_000_000_000 for v in prices.values()):
                        raise RuntimeError("平台渠道成本价格必须为非负整数 microUSD")
        if self.environment in {"staging", "production"} and self.gateway_mode == "legacy_test":
            raise RuntimeError("staging/production 环境禁止 KUNLUN_GATEWAY_MODE=legacy_test")
        if (
            self.gateway_mode in {"byok", "managed_gateway"}
            and os.getenv("VERCEL_ENV", "").strip().casefold() not in {"", "production"}
        ):
            raise RuntimeError("Vercel Preview/Development deployment 禁止启用 BYOK")
        if self.vault_backend not in {"disabled", "supabase_vault"}:
            raise RuntimeError("KUNLUN_VAULT_BACKEND 仅支持 disabled 或 supabase_vault")
        if self.gateway_mode in {"byok", "managed_gateway"} and self.is_production and self.vault_backend != "supabase_vault":
            raise RuntimeError("production BYOK 必须配置 Supabase Vault")
        if self.gateway_mode in {"byok", "managed_gateway"} and self.is_production:
            runtime_user = _database_user(self.database_url)
            executor_user = _database_user(self.vault_executor_database_url)
            if not _production_database_url_is_safe(self.vault_executor_database_url):
                raise RuntimeError("production BYOK 必须配置 verify-full KUNLUN_VAULT_EXECUTOR_DATABASE_URL")
            if executor_user != "kunlun_vault_executor":
                raise RuntimeError("KUNLUN_VAULT_EXECUTOR_DATABASE_URL 必须使用 kunlun_vault_executor")
            if runtime_user != "kunlun_runtime" or runtime_user == executor_user:
                raise RuntimeError("runtime 与 Vault executor 必须是独立数据库角色")
            if _database_password(self.database_url) == _database_password(self.vault_executor_database_url):
                raise RuntimeError("KUNLUN_DATABASE_URL 与 KUNLUN_VAULT_EXECUTOR_DATABASE_URL 数据库凭据不得重复")
            runtime_project = _supabase_project_ref(self.database_url)
            executor_project = _supabase_project_ref(self.vault_executor_database_url)
            if (
                not runtime_project
                or not secrets.compare_digest(runtime_project, executor_project)
                or _database_name(self.database_url) != _database_name(self.vault_executor_database_url)
            ):
                raise RuntimeError(
                    "runtime 与 Vault executor 必须连接同一可识别的 Supabase project/database"
                )
        if self.gateway_mode not in {"byok", "managed_gateway"} and self.vault_backend != "disabled":
            raise RuntimeError("仅 BYOK 模式允许配置 Credential Vault")
        if self.gateway_mode == "byok" and self.environment not in {"production", "test"}:
            raise RuntimeError("BYOK 仅允许在 production 或 test 环境运行")
        if self.gateway_mode in {"byok", "managed_gateway"} and (not self.providers or not self.provider_host_allowlist):
            raise RuntimeError("BYOK 必须配置服务端 Provider 目录和主机允许列表")
        upstream_active = self.live_upstream or self.gateway_mode in {"byok", "managed_gateway"}
        if self.rate_limit_per_minute < 1:
            raise RuntimeError("rate_limit_per_minute 必须大于 0")
        if not 1 <= self.checkout_rate_limit_per_minute <= 60:
            raise RuntimeError("checkout_rate_limit_per_minute 必须位于 1 到 60")
        if not 1 <= self.max_open_checkout_orders <= 20:
            raise RuntimeError("max_open_checkout_orders 必须位于 1 到 20")
        if not 60 <= self.model_reservation_lease_seconds <= 86_400:
            raise RuntimeError("model_reservation_lease_seconds 必须位于 60 到 86400")
        if self.max_output_tokens < 1 or self.max_output_tokens > 1_000_000:
            raise RuntimeError("max_output_tokens 必须位于安全范围内")
        if self.default_output_tokens < 1 or self.default_output_tokens > self.max_output_tokens:
            raise RuntimeError("默认输出上限必须位于允许范围内")
        if not 1 <= self.max_active_api_keys <= 100:
            raise RuntimeError("max_active_api_keys 必须位于 1 到 100")
        if not isinstance(self.models, dict) or not self.models or len(self.models) > 256:
            raise RuntimeError("模型目录必须包含 1 到 256 个模型")
        for model, config in self.models.items():
            if not isinstance(model, str) or not model.strip() or len(model) > 120 or not isinstance(config, dict):
                raise RuntimeError("模型目录格式无效")
            for field_name in ("input_microusd_per_million", "output_microusd_per_million"):
                value = config.get(field_name)
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000_000_000:
                    raise RuntimeError(f"模型 {model} 的价格无效")
            model_limit = config.get("max_output_tokens", self.max_output_tokens)
            if isinstance(model_limit, bool) or not isinstance(model_limit, int) or not 1 <= model_limit <= self.max_output_tokens:
                raise RuntimeError(f"模型 {model} 的输出上限无效")
        if self.live_payments and self.enable_test_payments:
            raise RuntimeError("真实支付与测试支付不能同时开启")
        if self.enable_test_payments and len(self.payment_webhook_secret) < 16:
            raise RuntimeError("测试支付开启时必须设置足够长的回调密钥")
        if self.live_upstream and not self.providers:
            raise RuntimeError("真实上游开启时必须配置 KUNLUN_PROVIDERS_JSON")
        if self.live_upstream and not self.provider_host_allowlist:
            raise RuntimeError("真实上游开启时必须配置 KUNLUN_PROVIDER_HOST_ALLOWLIST")
        if self.captcha_provider not in {"", "turnstile"}:
            raise RuntimeError("不支持的 CAPTCHA 浏览器组件")
        if len(self.captcha_site_key) > 256 or any(ord(char) < 33 for char in self.captcha_site_key):
            raise RuntimeError("CAPTCHA 站点公钥格式无效")
        self.captcha_expected_hostname = self.captcha_expected_hostname.casefold().rstrip(".")
        if len(self.captcha_expected_hostname) > 253 or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789.-"
            for char in self.captcha_expected_hostname
        ):
            raise RuntimeError("CAPTCHA 预期主机名格式无效")
        try:
            trusted_networks = [
                ip_network(cidr, strict=False) for cidr in self.trusted_proxy_cidrs
            ]
        except ValueError as exc:
            raise RuntimeError("KUNLUN_TRUSTED_PROXY_CIDRS 包含无效网段") from exc
        if self.is_production and any(
            network.prefixlen != network.max_prefixlen for network in trusted_networks
        ):
            raise RuntimeError("生产环境可信代理必须配置为精确的 /32 或 /128 地址")
        if self.trusted_proxy_secret and (
            len(self.trusted_proxy_secret) < 32
            or any(ord(char) < 33 or ord(char) > 126 for char in self.trusted_proxy_secret)
        ):
            raise RuntimeError("KUNLUN_TRUSTED_PROXY_SECRET 必须是至少 32 字符的可打印 ASCII 密钥")
        self.ingress_provider = self.ingress_provider.strip().casefold()
        if self.ingress_provider not in {"", "vercel"}:
            raise RuntimeError("KUNLUN_INGRESS_PROVIDER 仅支持 vercel")
        if self.ingress_provider == "vercel":
            missing_vercel = []
            if not self.trusted_proxy_secret or not self.trusted_proxy_secret_persisted:
                missing_vercel.append("持久化 KUNLUN_TRUSTED_PROXY_SECRET")
            if (
                len(self.cron_secret) < 32
                or not self.cron_secret_persisted
                or any(ord(char) < 33 or ord(char) > 126 for char in self.cron_secret)
            ):
                missing_vercel.append("持久化 CRON_SECRET")
            if (
                len(self.ops_ingress_secret) < 32
                or not self.ops_ingress_secret_persisted
                or any(ord(char) < 33 or ord(char) > 126 for char in self.ops_ingress_secret)
            ):
                missing_vercel.append("持久化 KUNLUN_OPS_INGRESS_SECRET")
            if (
                len(self.operator_signing_secret) < 32
                or not self.operator_signing_secret_persisted
                or any(ord(char) < 33 or ord(char) > 126 for char in self.operator_signing_secret)
            ):
                missing_vercel.append("持久化 KUNLUN_OPERATOR_SIGNING_SECRET")
            configured_secrets = {
                "KUNLUN_TRUSTED_PROXY_SECRET": self.trusted_proxy_secret,
                "CRON_SECRET": self.cron_secret,
                "KUNLUN_OPS_INGRESS_SECRET": self.ops_ingress_secret,
            }
            configured_secrets["KUNLUN_OPERATOR_SIGNING_SECRET"] = self.operator_signing_secret
            values_by_name = list(configured_secrets.items())
            for index, (name, value) in enumerate(values_by_name):
                if value and any(value == other for _other_name, other in values_by_name[index + 1:]):
                    missing_vercel.append(f"{name} 必须与其他 Vercel/运维密钥不同")
            if missing_vercel:
                raise RuntimeError("Vercel ingress 配置不完整: " + ", ".join(missing_vercel))
        if upstream_active:
            configured_hosts = {
                (urlparse(str(provider.get("base_url") or "")).hostname or "").casefold()
                for provider in self.providers if isinstance(provider, dict)
            }
            unexpected = sorted(configured_hosts - self.provider_host_allowlist)
            if unexpected:
                raise RuntimeError("Provider 主机不在允许列表: " + ", ".join(unexpected))
        if not isinstance(self.topup_packages, dict) or len(self.topup_packages) > 100:
            raise RuntimeError("充值套餐配置无效")
        for sku, package in self.topup_packages.items():
            if not isinstance(sku, str) or not sku or len(sku) > 80 or not isinstance(package, dict):
                raise RuntimeError("充值套餐配置无效")
            cash = package.get("payment_amount_minor")
            credit = package.get("credit_amount_microusd")
            currency = package.get("payment_currency")
            if (
                isinstance(cash, bool) or not isinstance(cash, int) or not 1 <= cash <= 100_000_000_000
                or isinstance(credit, bool) or not isinstance(credit, int) or not 1 <= credit <= 100_000_000_000_000
                or not isinstance(currency, str) or len(currency) != 3 or not currency.isupper()
            ):
                raise RuntimeError(f"充值套餐 {sku} 的金额或币种无效")
        if self.live_payments:
            bridge_host = (urlparse(self.payment_bridge_endpoint).hostname or "").casefold()
            missing_bridge = (
                not self.payment_bridge_endpoint.startswith("https://")
                or not bridge_host
                or bridge_host not in self.payment_bridge_host_allowlist
                or not self.payment_bridge_merchant_id
                or len(self.payment_bridge_secret) < 32
                or not self.payment_provider
                or not self.topup_packages
                or not self.payment_bridge_official_sdk_acknowledged
            )
            if missing_bridge:
                raise RuntimeError("正式支付适配器配置或官方 SDK 确认不完整")
        if self.is_production:
            missing = []
            if self.gateway_mode not in {"disabled", "byok", "managed_gateway"}:
                missing.append("production 仅允许 KUNLUN_GATEWAY_MODE=byok 或 disabled")
            if self.public_signup and self.gateway_mode != "managed_gateway":
                missing.append("关闭 KUNLUN_PUBLIC_SIGNUP（production 不提供公共注册）")
            if self.enable_test_payments:
                missing.append("关闭 KUNLUN_ENABLE_TEST_PAYMENTS")
            if self.live_payments and self.gateway_mode != "managed_gateway":
                missing.append("关闭 KUNLUN_LIVE_PAYMENTS（BYOK 产品不提供充值）")
            if self.topup_packages and self.gateway_mode != "managed_gateway":
                missing.append("清空 KUNLUN_TOPUP_PACKAGES_JSON（BYOK 产品不提供余额或充值套餐）")
            if self.gateway_mode == "byok" and self.live_upstream:
                missing.append("BYOK 必须关闭 KUNLUN_LIVE_UPSTREAM（禁止服务端共享上游密钥）")
            if self.operator_token:
                missing.append("KUNLUN_OPERATOR_TOKEN 仅允许在 development/test 环境兼容使用")
            if not _production_database_url_is_safe(self.database_url):
                missing.append(
                    "PostgreSQL KUNLUN_DATABASE_URL（verify-full 与可读的绝对 sslrootcert）"
                )
            if len(self.api_key_pepper) < 32:
                missing.append("KUNLUN_API_KEY_PEPPER")
            elif not self.api_key_pepper_persisted:
                missing.append("持久化 KUNLUN_API_KEY_PEPPER")
            if len(self.session_pepper) < 32:
                missing.append("KUNLUN_SESSION_PEPPER")
            elif not self.session_pepper_persisted:
                missing.append("持久化 KUNLUN_SESSION_PEPPER")
            if (self.public_signup or self.require_email_verification) and len(self.identity_token_pepper) < 32:
                missing.append("KUNLUN_IDENTITY_TOKEN_PEPPER")
            elif (self.public_signup or self.require_email_verification) and not self.identity_token_pepper_persisted:
                missing.append("持久化 KUNLUN_IDENTITY_TOKEN_PEPPER")
            if not self.trusted_proxy_cidrs and not self.trusted_proxy_secret:
                missing.append(
                    "可信反向代理 KUNLUN_TRUSTED_PROXY_CIDRS/KUNLUN_TRUSTED_PROXY_SECRET"
                )
            elif self.trusted_proxy_secret and not self.trusted_proxy_secret_persisted:
                missing.append("持久化 KUNLUN_TRUSTED_PROXY_SECRET")
            if self.public_signup:
                if not self.require_email_verification:
                    missing.append("邮件验证 KUNLUN_REQUIRE_EMAIL_VERIFICATION")
                if not self.smtp_url:
                    missing.append("KUNLUN_SMTP_URL")
                if not self.email_from:
                    missing.append("KUNLUN_EMAIL_FROM")
                if not self.public_base_url.startswith("https://"):
                    missing.append("HTTPS KUNLUN_PUBLIC_BASE_URL")
                if not self.captcha_required:
                    missing.append("注册反滥用 KUNLUN_CAPTCHA_REQUIRED")
                if not self.captcha_endpoint or not self.captcha_secret or not self.captcha_host_allowlist:
                    missing.append("CAPTCHA 服务端二次校验配置")
                if self.captcha_provider != "turnstile" or not self.captcha_site_key:
                    missing.append("CAPTCHA 浏览器组件 KUNLUN_CAPTCHA_PROVIDER/KUNLUN_CAPTCHA_SITE_KEY")
                elif (
                    self.captcha_endpoint != TURNSTILE_SITEVERIFY_ENDPOINT
                    or self.captcha_host_allowlist != {"challenges.cloudflare.com"}
                ):
                    missing.append("官方 Turnstile Siteverify 地址与精确主机允许列表")
                public_hostname = (urlparse(self.public_base_url).hostname or "").casefold().rstrip(".")
                if not public_hostname or self.captcha_expected_hostname != public_hostname:
                    missing.append("与公开域名一致的 KUNLUN_CAPTCHA_EXPECTED_HOSTNAME")
                for label, value in (
                    ("KUNLUN_TERMS_URL", self.terms_url),
                    ("KUNLUN_PRIVACY_URL", self.privacy_url),
                    ("KUNLUN_COMPLAINT_EMAIL", self.complaint_email),
                ):
                    if not value:
                        missing.append(label)
                if not self.compliance_acknowledged:
                    missing.append("KUNLUN_COMPLIANCE_ACKNOWLEDGED")
            if self.public_signup and upstream_active:
                if not self.content_safety_required:
                    missing.append("KUNLUN_CONTENT_SAFETY_REQUIRED")
                if not self.content_safety_endpoint or not self.content_safety_api_key or not self.content_safety_host_allowlist:
                    missing.append("内容安全适配器配置")
            if self.operator_signing_secret:
                if len(self.operator_signing_secret) < 32 or not self.operator_signing_secret_persisted:
                    missing.append("持久化 KUNLUN_OPERATOR_SIGNING_SECRET")
                if not self.ops_private_access_acknowledged:
                    missing.append("KUNLUN_OPS_PRIVATE_ACCESS_ACKNOWLEDGED")
            elif upstream_active or self.live_payments:
                missing.append("KUNLUN_OPERATOR_SIGNING_SECRET")
            if (upstream_active or self.live_payments) and not self.ops_private_access_acknowledged:
                missing.append("KUNLUN_OPS_PRIVATE_ACCESS_ACKNOWLEDGED")
            if upstream_active:
                if not self.model_catalog_explicit:
                    missing.append("显式 KUNLUN_MODELS_JSON（禁止生产使用内置 test-model）")
                catalog_models = set(self.models)
                routed_models: set[str] = set()
                provider_names: set[str] = set()
                price_fields = {
                    "input_microusd_per_million",
                    "output_microusd_per_million",
                }
                for index, provider in enumerate(self.providers):
                    if not isinstance(provider, dict):
                        missing.append(f"Provider {index + 1} 显式模型列表与完整上游价格")
                        continue
                    provider_name = str(provider.get("name") or f"provider-{index + 1}")
                    normalized_provider_name = provider_name.strip().casefold()
                    if normalized_provider_name in provider_names:
                        missing.append(f"Provider 名称重复: {provider_name}")
                    provider_names.add(normalized_provider_name)
                    if self.gateway_mode in {"byok", "managed_gateway"}:
                        parsed_provider_url = urlparse(str(provider.get("base_url") or ""))
                        if normalized_provider_name not in BYOK_PROVIDER_CATALOG:
                            missing.append(f"BYOK Provider 不在允许目录: {provider_name}")
                        if provider.get("api_key_env"):
                            missing.append(f"BYOK Provider {provider_name} 禁止 api_key_env 或共享密钥")
                        if (
                            parsed_provider_url.scheme != "https"
                            or not parsed_provider_url.hostname
                            or parsed_provider_url.username
                            or parsed_provider_url.password
                            or parsed_provider_url.query
                            or parsed_provider_url.fragment
                        ):
                            missing.append(f"BYOK Provider {provider_name} 必须使用固定 HTTPS 地址")
                        else:
                            try:
                                validate_byok_provider_endpoint(
                                    normalized_provider_name,
                                    str(provider.get("base_url") or ""),
                                )
                            except RuntimeError:
                                missing.append(
                                    f"BYOK Provider {provider_name} 必须绑定已审核的官方兼容端点"
                                )
                    raw_models = provider.get("models")
                    if not isinstance(raw_models, list) or not raw_models:
                        missing.append(f"Provider {provider_name} 显式模型列表")
                        continue
                    normalized_models = {
                        model for model in raw_models
                        if isinstance(model, str) and model in catalog_models
                    }
                    if len(normalized_models) != len(raw_models):
                        missing.append(f"Provider {provider_name} 模型必须全部来自 KUNLUN_MODELS_JSON")
                    routed_models.update(normalized_models)
                    raw_pricing = provider.get("pricing")
                    for model in raw_models:
                        prices = raw_pricing.get(model) if isinstance(raw_pricing, dict) else None
                        if (
                            not isinstance(prices, dict)
                            or set(prices) != price_fields
                            or any(
                                isinstance(prices.get(field_name), bool)
                                or not isinstance(prices.get(field_name), int)
                                or not 0 <= prices[field_name] <= 1_000_000_000_000
                                for field_name in price_fields
                            )
                        ):
                            missing.append(f"Provider {provider_name} 模型 {model} 完整上游价格")
                        elif self.gateway_mode == "byok" and model in self.models:
                            # The catalog is a pre-authorisation ceiling, not
                            # an indicative retail price.  A provider route
                            # that can exceed it must never start in prod.
                            catalog_price = self.models[model]
                            if (
                                prices["input_microusd_per_million"]
                                > catalog_price["input_microusd_per_million"]
                                or prices["output_microusd_per_million"]
                                > catalog_price["output_microusd_per_million"]
                            ):
                                missing.append(
                                    f"BYOK Provider {provider_name} 模型 {model} 上游价格超过 KUNLUN_MODELS_JSON 预授权上限"
                                )
                unrouted = sorted(catalog_models - routed_models)
                if unrouted:
                    missing.append("模型目录存在无供应商路由: " + ", ".join(unrouted))
            if missing:
                raise RuntimeError("生产配置未通过安全门禁: " + ", ".join(missing))
