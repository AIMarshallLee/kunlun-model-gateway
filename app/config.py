"""Configuration with fail-closed production validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import ip_network
import json
import os
from pathlib import Path
import secrets
from typing import Any
from urllib.parse import parse_qsl, urlparse


TURNSTILE_SITEVERIFY_ENDPOINT = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
SUPPORTED_ENVIRONMENTS = frozenset({"development", "test", "staging", "production"})
LEGACY_OPERATOR_ENVIRONMENTS = frozenset({"development", "test"})


def _production_database_url_is_safe(value: str) -> bool:
    """Require authenticated PostgreSQL TLS for every production process."""
    try:
        parsed = urlparse(value)
        query = [(key.casefold(), item) for key, item in parse_qsl(
            parsed.query, keep_blank_values=True,
        )]
        ssl_modes = [item.casefold() for key, item in query if key == "sslmode"]
        root_certs = [item for key, item in query if key == "sslrootcert"]
        certificate = Path(root_certs[0]) if len(root_certs) == 1 else None
        return bool(
            parsed.scheme == "postgresql+psycopg"
            and parsed.hostname
            and parsed.username
            and parsed.password
            and parsed.path not in {"", "/"}
            and not parsed.fragment
            and ssl_modes == ["verify-full"]
            and certificate is not None
            and certificate.is_absolute()
            and certificate.is_file()
            and os.access(certificate, os.R_OK)
        )
    except (TypeError, ValueError, OSError):
        return False


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
        values: dict[str, Any] = {
            "database_url": os.getenv("KUNLUN_DATABASE_URL", "sqlite:///./kunlun-gateway.sqlite3"),
            "environment": os.getenv("KUNLUN_ENV", "development"),
            "public_signup": _env_bool("KUNLUN_PUBLIC_SIGNUP"),
            "enable_test_payments": _env_bool("KUNLUN_ENABLE_TEST_PAYMENTS"),
            "live_payments": _env_bool("KUNLUN_LIVE_PAYMENTS"),
            "live_upstream": _env_bool("KUNLUN_LIVE_UPSTREAM"),
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
            if missing_vercel:
                raise RuntimeError("Vercel ingress 配置不完整: " + ", ".join(missing_vercel))
        if self.live_upstream:
            configured_hosts = {
                (urlparse(str(provider.get("base_url") or "")).hostname or "").casefold()
                for provider in self.providers
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
            if self.enable_test_payments:
                missing.append("关闭 KUNLUN_ENABLE_TEST_PAYMENTS")
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
            if self.public_signup and self.live_upstream:
                if not self.content_safety_required:
                    missing.append("KUNLUN_CONTENT_SAFETY_REQUIRED")
                if not self.content_safety_endpoint or not self.content_safety_api_key or not self.content_safety_host_allowlist:
                    missing.append("内容安全适配器配置")
            if self.operator_signing_secret:
                if len(self.operator_signing_secret) < 32 or not self.operator_signing_secret_persisted:
                    missing.append("持久化 KUNLUN_OPERATOR_SIGNING_SECRET")
                if not self.ops_private_access_acknowledged:
                    missing.append("KUNLUN_OPS_PRIVATE_ACCESS_ACKNOWLEDGED")
            elif self.live_upstream or self.live_payments:
                missing.append("KUNLUN_OPERATOR_SIGNING_SECRET")
            if (self.live_upstream or self.live_payments) and not self.ops_private_access_acknowledged:
                missing.append("KUNLUN_OPS_PRIVATE_ACCESS_ACKNOWLEDGED")
            if self.live_upstream:
                if not self.model_catalog_explicit:
                    missing.append("显式 KUNLUN_MODELS_JSON（禁止生产使用内置 test-model）")
                catalog_models = set(self.models)
                routed_models: set[str] = set()
                price_fields = {
                    "input_microusd_per_million",
                    "output_microusd_per_million",
                }
                for index, provider in enumerate(self.providers):
                    if not isinstance(provider, dict):
                        missing.append(f"Provider {index + 1} 显式模型列表与完整上游价格")
                        continue
                    provider_name = str(provider.get("name") or f"provider-{index + 1}")
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
                unrouted = sorted(catalog_models - routed_models)
                if unrouted:
                    missing.append("模型目录存在无供应商路由: " + ", ".join(unrouted))
            if missing:
                raise RuntimeError("生产配置未通过安全门禁: " + ", ".join(missing))
