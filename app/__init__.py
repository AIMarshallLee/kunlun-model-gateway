"""Kunlun public model gateway application factory."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
import logging
from pathlib import Path
import re
import secrets
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from gateway import ProviderError

from . import providers
from .auth import Principal, enforce_auth_rate_limit, require_api_key, require_operator_scope, require_session
from .config import Settings
from .customer_delivery import router as delivery_router, recorded_request_response
from .managed_gateway import router as managed_router
from .public_site import router as public_router, public_https_url
from .services import request_limits
from .services.platform_credentials import SupabasePlatformVault
from .client_ip import TrustedProxyClientIPMiddleware
from .db import Base, build_engine, build_session_factory, install_ledger_guards
from .db_guards import SCHEMA_HEAD, assert_schema_revision
from .models import (
    AccessSession,
    ApiKey,
    Budget,
    CredentialActionAudit,
    LedgerEntry,
    LedgerTransaction,
    ModelPrice,
    ModelRequest,
    OperatorAction,
    PaymentChargeback,
    PaymentOrder,
    PaymentRefund,
    ProviderConnection,
    SafetyAudit,
    User,
    Wallet,
)
from .middleware import RequestBodyLimitMiddleware
from .observability import MetricsRegistry, readiness_report
from .schemas import (
    AccountStatusRequest,
    BudgetAmountRequest,
    ChatCompletionRequest,
    EmailAddressRequest,
    KeyCreateRequest,
    KeyRevokeRequest,
    LoginRequest,
    LiveCheckoutRequest,
    PaymentRefundRequest,
    PaymentReconcileRequest,
    RegisterRequest,
    ReconciliationRequest,
    RefundRiskDispositionRequest,
    ResetPasswordRequest,
    TopupRequest,
    ProviderConnectionPutRequest,
    VerifyEmailRequest,
)
from .security import (
    DUMMY_PASSWORD_HASH,
    as_utc,
    hash_password,
    issue_api_key,
    issue_session_token,
    parse_api_key,
    token_digest,
    utcnow,
    verify_password,
)
from .services import budget as budget_service
from .services.gateway_billing import (
    BillingError,
    enforce_key_policy,
    key_usage,
    mark_pending_reconciliation,
    record_attempt,
    release_model_request,
    reserve_byok_model_request,
    reserve_model_request,
    settle_model_request,
)
from .services.credentials import CredentialVault, DisabledCredentialVault, SecretUnavailable, build_credential_vault
from .services.ledger import CUSTOMER_AVAILABLE
from .services.captcha import CaptchaError, CaptchaVerifier
from .services.content_safety import ContentSafetyError, HttpContentSafetyAdapter, SafetyDecision
from .services.identity import (
    EmailSender,
    DisabledEmailSender,
    IdentityError,
    apply_user_freeze,
    build_email_sender,
    consume_email_verification,
    enforce_key_limit,
    issue_email_verification,
    request_password_reset,
    reset_password,
)
from .services.payments import PaymentError, create_test_order, process_test_webhook
from .services.live_payments import LivePaymentBridge, PaymentBridgeError
from .services.payment_domain import PaymentDomainError, PaymentDomainService
from .services.ops_tokens import OperatorClaims
from .streaming import SSEUsageTracker, StreamProtocolError, synthesize_sse
from .vercel import VercelIngressMiddleware
from scripts.maintenance import run_once as run_maintenance_once


logger = logging.getLogger("kunlun_gateway")


def _seed_prices(app: FastAPI) -> None:
    with app.state.SessionLocal() as session:
        for model, config in app.state.settings.models.items():
            latest = session.scalar(select(ModelPrice).where(
                ModelPrice.model == model,
            ).order_by(ModelPrice.version.desc()))
            if latest is not None and app.state.settings.gateway_mode == "managed_gateway":
                # Environment prices bootstrap new models only. Restarting or
                # rolling back an instance must not undo an operator price or
                # re-list a deliberately unpublished model.
                continue
            desired_input = int(config["input_microusd_per_million"])
            desired_output = int(config["output_microusd_per_million"])
            desired_limit = int(config.get("max_output_tokens", app.state.settings.max_output_tokens))
            unchanged = (
                latest is not None
                and latest.active
                and latest.input_microusd_per_million == desired_input
                and latest.output_microusd_per_million == desired_output
                and latest.max_output_tokens == desired_limit
            )
            if not unchanged:
                session.execute(update(ModelPrice).where(
                    ModelPrice.model == model,
                    ModelPrice.active.is_(True),
                ).values(active=False))
                session.add(ModelPrice(
                    id=str(uuid.uuid4()),
                    model=model,
                    version=(latest.version + 1) if latest else 1,
                    input_microusd_per_million=desired_input,
                    output_microusd_per_million=desired_output,
                    max_output_tokens=desired_limit,
                ))
        try:
            session.commit()
        except IntegrityError as exc:
            # Another process may have installed the exact same price version
            # between our SELECT and INSERT. Managed catalogs thereafter use
            # DB versions; legacy modes require an identical active catalog.
            session.rollback()
            for model, config in app.state.settings.models.items():
                if app.state.settings.gateway_mode == "managed_gateway" and session.scalar(
                    select(ModelPrice.id).where(ModelPrice.model == model).limit(1)
                ) is not None:
                    continue
                latest = session.scalar(select(ModelPrice).where(
                    ModelPrice.model == model,
                    ModelPrice.active.is_(True),
                ).order_by(ModelPrice.version.desc()))
                expected = (
                    int(config["input_microusd_per_million"]),
                    int(config["output_microusd_per_million"]),
                    int(config.get("max_output_tokens", app.state.settings.max_output_tokens)),
                )
                actual = None if latest is None else (
                    latest.input_microusd_per_million,
                    latest.output_microusd_per_million,
                    latest.max_output_tokens,
                )
                if actual != expected:
                    raise RuntimeError("模型价格目录并发初始化冲突") from exc


def _openai_error(status_code: int, message: str, code: str, *, request_id: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {
            "message": message,
            "type": "kunlun_gateway_error",
            "code": code,
        }
    }
    headers = {}
    if request_id:
        body["error"]["request_id"] = request_id
        headers["X-Request-Id"] = request_id
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def create_app(
    *,
    database_url: str | None = None,
    payment_webhook_secret: str | None = None,
    enable_test_payments: bool | None = None,
    public_signup: bool | None = None,
    rate_limit_per_minute: int | None = None,
    checkout_rate_limit_per_minute: int | None = None,
    max_open_checkout_orders: int | None = None,
    operator_token: str | None = None,
    operator_signing_secret: str | None = None,
    require_email_verification: bool | None = None,
    identity_token_pepper: str | None = None,
    public_base_url: str | None = None,
    identity_sender: EmailSender | None = None,
    max_active_api_keys: int | None = None,
    captcha_required: bool | None = None,
    captcha_adapter: CaptchaVerifier | None = None,
    content_safety_required: bool | None = None,
    content_safety_adapter: HttpContentSafetyAdapter | Any | None = None,
    live_payment_bridge: LivePaymentBridge | Any | None = None,
    payment_provider: str | None = None,
    topup_packages: dict[str, dict[str, Any]] | None = None,
    provider_clients: list[Any] | None = None,
    credential_vault: CredentialVault | None = None,
    platform_vault: Any | None = None,
    gateway_mode: str | None = None,
    vault_backend: str | None = None,
    environment: str | None = None,
) -> FastAPI:
    settings = Settings.from_env(
        database_url=database_url,
        payment_webhook_secret=payment_webhook_secret,
        enable_test_payments=enable_test_payments,
        public_signup=public_signup,
        rate_limit_per_minute=rate_limit_per_minute,
        checkout_rate_limit_per_minute=checkout_rate_limit_per_minute,
        max_open_checkout_orders=max_open_checkout_orders,
        operator_token=operator_token,
        operator_signing_secret=operator_signing_secret,
        require_email_verification=require_email_verification,
        identity_token_pepper=identity_token_pepper,
        public_base_url=public_base_url,
        max_active_api_keys=max_active_api_keys,
        captcha_required=captcha_required,
        content_safety_required=content_safety_required,
        payment_provider=payment_provider,
        topup_packages=topup_packages,
        gateway_mode=gateway_mode,
        vault_backend=vault_backend,
        environment=environment,
    )
    if settings.gateway_mode == "byok" and settings.environment == "test" and credential_vault is None:
        raise RuntimeError("test BYOK 必须显式注入 CredentialVault")
    if settings.gateway_mode == "managed_gateway" and settings.environment == "test" and platform_vault is None:
        raise RuntimeError("test 平台模式必须显式注入 platform Vault")
    if settings.gateway_mode == "managed_gateway" and (provider_clients is not None or credential_vault is not None):
        raise RuntimeError("平台模式禁止全局 Provider 和客户 BYOK 凭据注入")
    if settings.is_production:
        injected_adapters = (
            identity_sender,
            captcha_adapter,
            content_safety_adapter,
            live_payment_bridge,
        )
        if any(adapter is not None for adapter in injected_adapters) or provider_clients is not None or credential_vault is not None or platform_vault is not None:
            # The supported production entrypoint builds every live adapter
            # from validated Settings. Dependency injection is reserved for
            # local/test processes so a custom ASGI bootstrap cannot bypass
            # production capability gates or host allow-lists.
            raise RuntimeError("生产环境禁止注入测试或自定义适配器")
    app = FastAPI(
        title="Kunlun Model Gateway",
        version="0.1.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
    )
    app.include_router(delivery_router)
    app.include_router(managed_router)
    app.include_router(public_router)
    from .chargeback_routes import router as chargeback_router
    app.include_router(chargeback_router)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default 422 representation includes Pydantic's ``input``
        # and sometimes ``ctx`` fields. Both may contain API keys, prompts or
        # customer content, so never reflect them back to a caller or logs.
        safe_errors = [
            {
                key: error[key]
                # ``loc`` may include an unknown user-provided field name.
                # Keep only framework-owned classification/message fields.
                for key in ("type", "msg", "url")
                if key in error
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": safe_errors})

    app.state.settings = settings
    app.state.metrics = MetricsRegistry()
    if identity_sender is not None:
        app.state.identity_sender = identity_sender
    elif not settings.is_production or settings.smtp_url:
        app.state.identity_sender = build_email_sender(
            environment=settings.environment,
            smtp_url=settings.smtp_url,
            from_address=settings.email_from,
            public_base_url=settings.public_base_url,
        )
    else:
        app.state.identity_sender = DisabledEmailSender()
    app.state.captcha = captcha_adapter
    if app.state.captcha is None and settings.captcha_required:
        app.state.captcha = CaptchaVerifier(
            endpoint=settings.captcha_endpoint,
            secret=settings.captcha_secret,
            allowed_hosts=settings.captcha_host_allowlist,
            expected_hostname=settings.captcha_expected_hostname,
        )
    app.state.content_safety = content_safety_adapter
    if app.state.content_safety is None and settings.content_safety_required:
        app.state.content_safety = HttpContentSafetyAdapter(
            endpoint=settings.content_safety_endpoint,
            api_key=settings.content_safety_api_key,
            allowed_hosts=settings.content_safety_host_allowlist,
        )
    app.state.live_payment_bridge = live_payment_bridge
    if app.state.live_payment_bridge is None and settings.live_payments:
        app.state.live_payment_bridge = LivePaymentBridge(
            endpoint=settings.payment_bridge_endpoint,
            merchant_id=settings.payment_bridge_merchant_id,
            secret=settings.payment_bridge_secret,
            allowed_hosts=settings.payment_bridge_host_allowlist,
        )
    app.state.engine = build_engine(settings.database_url)
    app.state.SessionLocal = build_session_factory(app.state.engine)
    # The only production constructor is the Settings-bound Vault adapter.
    # Test injection remains available outside production, after the engine
    # exists so a real adapter can probe its actual database role.
    app.state.credential_vault = (DisabledCredentialVault() if settings.gateway_mode == "managed_gateway"
                                  else credential_vault or build_credential_vault(settings, app.state.engine))
    app.state.platform_vault = platform_vault
    if settings.gateway_mode == "managed_gateway" and settings.is_production:
        from .services.platform_credentials import platform_contract_errors
        app.state.platform_vault = SupabasePlatformVault(build_engine(settings.vault_executor_database_url))
        if platform_contract_errors(app.state.engine, app.state.platform_vault.engine):
            raise RuntimeError("平台 Vault 隔离契约未通过，拒绝启动")
    if settings.is_production:
        # The runtime database role is intentionally not a migrator. A stale
        # or empty schema fails before any business endpoint is exposed.
        assert_schema_revision(app.state.engine, SCHEMA_HEAD)
    else:
        Base.metadata.create_all(app.state.engine)
        install_ledger_guards(app.state.engine)
    _seed_prices(app)
    if provider_clients is not None:
        providers.ordered_clients = list(provider_clients)
    elif settings.live_upstream and settings.gateway_mode != "managed_gateway":
        providers.ordered_clients = providers.build_provider_clients(
            settings.providers,
            allowed_hosts=settings.provider_host_allowlist,
        )
    else:
        providers.ordered_clients = []
    static_dir = Path(__file__).resolve().parent / "static"
    from .ops_console import router as ops_console_router
    app.include_router(ops_console_router)
    from .model_catalog import router as model_catalog_router
    app.include_router(model_catalog_router)
    from .ops_alerts import router as ops_alerts_router
    app.include_router(ops_alerts_router)
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")
    app.add_middleware(RequestBodyLimitMiddleware)
    if settings.captcha_required and settings.captcha_provider == "turnstile":
        content_security_policy = (
            "default-src 'self'; script-src 'self' https://challenges.cloudflare.com; "
            "frame-src https://challenges.cloudflare.com; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        )
    else:
        content_security_policy = (
            "default-src 'self'; script-src 'self'; frame-src 'none'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'none'"
        )

    @app.middleware("http")
    async def body_limit_and_security_headers(request: Request, call_next):
        request_id = str(uuid.uuid4())
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            app.state.metrics.inc(
                "gateway_http_requests_total",
                labels={"method": request.method, "status": "500"},
            )
            app.state.metrics.observe(
                "gateway_http_request_duration_seconds",
                time.perf_counter() - started,
                labels={"method": request.method},
            )
            raise
        app.state.metrics.inc(
            "gateway_http_requests_total",
            labels={"method": request.method, "status": str(response.status_code)},
        )
        app.state.metrics.observe(
            "gateway_http_request_duration_seconds",
            time.perf_counter() - started,
            labels={"method": request.method},
        )
        response.headers.setdefault("X-Request-Id", request_id)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault(
            "Content-Security-Policy",
            content_security_policy,
        )
        if settings.is_production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.get("/", include_in_schema=False)
    def homepage() -> FileResponse:
        page = "home.html" if settings.gateway_mode == "managed_gateway" else "index.html"
        return FileResponse(static_dir / page, media_type="text/html")

    @app.get("/console", include_in_schema=False)
    def developer_console() -> FileResponse:
        return FileResponse(static_dir / "index.html", media_type="text/html")

    app.add_api_route("/verify-email", developer_console, methods=["GET"], include_in_schema=False)
    app.add_api_route("/reset-password", developer_console, methods=["GET"], include_in_schema=False)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        checks: dict[str, bool] = {}
        try:
            with app.state.SessionLocal() as session:
                session.execute(text("SELECT 1"))
            checks["database"] = True
        except Exception:
            checks["database"] = False
        if settings.is_production:
            try:
                assert_schema_revision(app.state.engine, SCHEMA_HEAD)
                checks["schema_revision"] = True
            except RuntimeError:
                checks["schema_revision"] = False
        else:
            checks["schema_revision"] = True
        checks["providers"] = not settings.live_upstream or bool(providers.ordered_clients)
        if settings.gateway_mode == "managed_gateway":
            checks["providers"] = bool(settings.providers)
        checks["captcha"] = not settings.captcha_required or app.state.captcha is not None
        checks["content_safety"] = not settings.content_safety_required or app.state.content_safety is not None
        checks["payment_bridge"] = not settings.live_payments or app.state.live_payment_bridge is not None
        if settings.gateway_mode != "byok":
            checks["credential_vault"] = True
        else:
            try:
                checks["credential_vault"] = bool(app.state.credential_vault.probe())
            except Exception:
                checks["credential_vault"] = False
        if settings.gateway_mode == "managed_gateway":
            checks["credential_vault"] = bool(app.state.platform_vault.probe())
        report = readiness_report(checks)
        report.update({
            "environment": settings.environment,
            "public_signup": settings.public_signup,
            "test_payments": settings.enable_test_payments,
            "live_payments": settings.live_payments or app.state.live_payment_bridge is not None,
            "live_upstream": settings.live_upstream,
            "gateway_mode": settings.gateway_mode,
            "providers": len(providers.ordered_clients),
            "email_verification": settings.require_email_verification,
            "captcha_required": settings.captcha_required,
            "captcha_provider": settings.captcha_provider if settings.captcha_required else "",
            "captcha_site_key": settings.captcha_site_key if settings.captcha_required else "",
        })
        return JSONResponse(
            status_code=200 if report["status"] == "ready" else 503,
            content=report,
        )

    @app.get("/api/cron/maintenance", include_in_schema=False)
    def maintenance_cron(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if not settings.cron_secret:
            raise HTTPException(status_code=404, detail="Not Found")
        expected = f"Bearer {settings.cron_secret}"
        if authorization is None or not secrets.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")
        counts = run_maintenance_once(settings, app.state.SessionLocal)
        if counts is None:
            logger.info("Vercel maintenance cron skipped because the database lock is held")
            return {
                "status": "skipped",
                "deleted_auth_rate_limit_counters": 0,
                "deleted_rate_limit_counters": 0,
                "stale_model_reservations": 0,
            }
        logger.info("Vercel maintenance cron completed: %s", counts)
        return {
            "status": "ok",
            "deleted_auth_rate_limit_counters": counts["auth_rate_limit_counters"],
            "deleted_rate_limit_counters": counts["rate_limit_counters"],
            "stale_model_reservations": counts["stale_model_reservations"],
        }

    @app.get("/metrics", include_in_schema=False)
    def metrics(
        _operator: OperatorClaims = Depends(require_operator_scope("metrics:read")),
    ) -> PlainTextResponse:
        return PlainTextResponse(
            app.state.metrics.scrape(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    def _register_identity_sync(email: str, password: str) -> JSONResponse:
        """Run hashing, database work and synchronous SMTP off the event loop."""
        try:
            password_hash = hash_password(password)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        user_id = str(uuid.uuid4())
        now = utcnow()
        status = "pending_email" if settings.require_email_verification else "active"
        with app.state.SessionLocal() as session:
            session.add(User(
                id=user_id,
                email=email,
                password_hash=password_hash,
                status=status,
                email_verified_at=None if settings.require_email_verification else now,
            ))
            session.add(Wallet(user_id=user_id))
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if settings.require_email_verification:
                    # Production registration uses the same response for a
                    # new or existing address. A pending owner may receive a
                    # fresh verification link, but callers cannot enumerate
                    # accounts from status/body or delivery errors.
                    existing_user = session.scalar(select(User).where(User.email == email))
                    if existing_user is not None and existing_user.status == "pending_email":
                        try:
                            issue_email_verification(
                                session,
                                existing_user.id,
                                settings.identity_token_pepper,
                                app.state.identity_sender,
                            )
                        except IdentityError:
                            app.state.metrics.inc(
                                "gateway_identity_email_total",
                                labels={"kind": "verification", "result": "failed"},
                            )
                    return JSONResponse(status_code=202, content={"accepted": True})
                raise HTTPException(status_code=409, detail="邮箱已注册") from exc
            if settings.require_email_verification:
                try:
                    issue_email_verification(
                        session,
                        user_id,
                        settings.identity_token_pepper,
                        app.state.identity_sender,
                    )
                except IdentityError:
                    # The account remains pending and can use the generic
                    # resend flow. Do not turn SMTP state into an existence
                    # oracle.
                    app.state.metrics.inc(
                        "gateway_identity_email_total",
                        labels={"kind": "verification", "result": "failed"},
                    )
                return JSONResponse(status_code=202, content={"accepted": True})
        return JSONResponse(status_code=201, content={
            "id": user_id,
            "email": email,
            "status": status,
        })

    async def register(payload: RegisterRequest, request: Request) -> JSONResponse:
        if not settings.public_signup:
            raise HTTPException(status_code=403, detail="公开注册未开启")
        await asyncio.to_thread(enforce_auth_rate_limit, request, "register", payload.email)
        if settings.captcha_required:
            if app.state.captcha is None or not payload.captcha_token:
                raise HTTPException(status_code=403, detail="验证码校验失败")
            try:
                allowed = await app.state.captcha.verify(
                    payload.captcha_token,
                    remote_ip=request.client.host if request.client else None,
                    expected_action="register",
                )
            except CaptchaError as exc:
                raise HTTPException(status_code=503, detail="验证码服务不可用，请稍后重试") from exc
            if not allowed:
                raise HTTPException(status_code=403, detail="验证码校验失败")
        return await asyncio.to_thread(_register_identity_sync, payload.email, payload.password)

    app.add_api_route("/auth/register", register, methods=["POST"], status_code=201)
    app.add_api_route("/v1/auth/register", register, methods=["POST"], status_code=201, include_in_schema=False)

    def verify_email(payload: VerifyEmailRequest, request: Request) -> dict[str, bool]:
        enforce_auth_rate_limit(request, "verify_email", payload.token[-32:])
        with app.state.SessionLocal() as session:
            if not consume_email_verification(session, payload.token, settings.identity_token_pepper):
                raise HTTPException(status_code=400, detail="验证链接无效或已过期")
        return {"verified": True}

    app.add_api_route("/auth/verify-email", verify_email, methods=["POST"])
    app.add_api_route("/v1/auth/verify-email", verify_email, methods=["POST"], include_in_schema=False)

    def _resend_verification_sync(email: str) -> None:
        with app.state.SessionLocal() as session:
            user = session.scalar(select(User).where(User.email == email))
            if user is not None and user.status == "pending_email":
                try:
                    issue_email_verification(
                        session,
                        user.id,
                        settings.identity_token_pepper,
                        app.state.identity_sender,
                    )
                except IdentityError:
                    # Generic response prevents using delivery state as an
                    # account-existence oracle. Operators receive metrics.
                    pass

    async def resend_verification(payload: EmailAddressRequest, request: Request) -> JSONResponse:
        await asyncio.to_thread(
            enforce_auth_rate_limit, request, "resend_verification", payload.email,
        )
        if settings.captcha_required:
            if app.state.captcha is None or not payload.captcha_token:
                raise HTTPException(status_code=403, detail="验证码校验失败")
            try:
                captcha_ok = await app.state.captcha.verify(
                    payload.captcha_token,
                    remote_ip=request.client.host if request.client else None,
                    expected_action="resend_verification",
                )
            except CaptchaError as exc:
                raise HTTPException(status_code=503, detail="验证码服务不可用，请稍后重试") from exc
            if not captcha_ok:
                raise HTTPException(status_code=403, detail="验证码校验失败")
        await asyncio.to_thread(_resend_verification_sync, payload.email)
        return JSONResponse(status_code=202, content={"accepted": True})

    app.add_api_route("/auth/resend-verification", resend_verification, methods=["POST"], status_code=202)

    def _forgot_password_sync(email: str) -> None:
        with app.state.SessionLocal() as session:
            request_password_reset(
                session,
                email,
                settings.identity_token_pepper,
                app.state.identity_sender,
            )

    async def forgot_password(payload: EmailAddressRequest, request: Request) -> JSONResponse:
        await asyncio.to_thread(enforce_auth_rate_limit, request, "forgot_password", payload.email)
        if settings.captcha_required:
            if app.state.captcha is None or not payload.captcha_token:
                raise HTTPException(status_code=403, detail="验证码校验失败")
            try:
                captcha_ok = await app.state.captcha.verify(
                    payload.captcha_token,
                    remote_ip=request.client.host if request.client else None,
                    expected_action="password_reset",
                )
            except CaptchaError as exc:
                raise HTTPException(status_code=503, detail="验证码服务不可用，请稍后重试") from exc
            if not captcha_ok:
                raise HTTPException(status_code=403, detail="验证码校验失败")
        await asyncio.to_thread(_forgot_password_sync, payload.email)
        return JSONResponse(status_code=202, content={"accepted": True})

    app.add_api_route("/auth/forgot-password", forgot_password, methods=["POST"], status_code=202)

    def perform_password_reset(payload: ResetPasswordRequest, request: Request) -> dict[str, bool]:
        enforce_auth_rate_limit(request, "reset_password", payload.token[-32:])
        with app.state.SessionLocal() as session:
            try:
                changed = reset_password(
                    session,
                    payload.token,
                    payload.new_password,
                    settings.identity_token_pepper,
                    settings.session_pepper,
                )
            except IdentityError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if not changed:
                raise HTTPException(status_code=400, detail="重置链接无效或已过期")
        return {"reset": True}

    app.add_api_route("/auth/reset-password", perform_password_reset, methods=["POST"])

    def login(payload: LoginRequest, request: Request) -> dict[str, Any]:
        enforce_auth_rate_limit(request, "login", payload.email)
        with app.state.SessionLocal() as session:
            user = session.scalar(select(User).where(User.email == payload.email))
            encoded_password = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
            password_valid = verify_password(payload.password, encoded_password)
            if (
                user is None
                or user.status != "active"
                or (settings.require_email_verification and user.email_verified_at is None)
                or not password_valid
            ):
                raise HTTPException(status_code=401, detail="邮箱或密码错误")
            raw_token = issue_session_token()
            session.add(AccessSession(
                id=str(uuid.uuid4()),
                user_id=user.id,
                token_digest=token_digest(raw_token, settings.session_pepper),
                expires_at=utcnow() + timedelta(hours=12),
            ))
            session.commit()
        return {"access_token": raw_token, "token_type": "bearer", "expires_in": 43_200}

    app.add_api_route("/auth/login", login, methods=["POST"])
    app.add_api_route("/v1/auth/login", login, methods=["POST"], include_in_schema=False)

    @app.post("/auth/logout-all", status_code=204)
    def logout_all(principal: Principal = Depends(require_session)) -> Response:
        with app.state.SessionLocal() as session:
            session.execute(update(AccessSession).where(
                AccessSession.user_id == principal.user_id,
                AccessSession.revoked.is_(False),
            ).values(revoked=True))
            session.commit()
        return Response(status_code=204)

    @app.post("/v1/keys", status_code=201)
    def create_key(payload: KeyCreateRequest, principal: Principal = Depends(require_session)) -> dict[str, Any]:
        raw, parsed = issue_api_key()
        with app.state.SessionLocal() as session:
            if payload.allowed_models is not None:
                active_models = set(session.scalars(select(ModelPrice.model).where(ModelPrice.active.is_(True))))
                if not set(payload.allowed_models).issubset(active_models):
                    raise HTTPException(422, "API Key 模型范围包含未上架模型")
            try:
                enforce_key_limit(session, principal.user_id, settings.max_active_api_keys)
            except IdentityError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            session.add(ApiKey(
                id=parsed.key_id,
                user_id=principal.user_id,
                name=payload.name,
                secret_digest=token_digest(parsed.secret, settings.api_key_pepper),
                last_four=parsed.secret[-4:],
                allowed_models_json=json.dumps(payload.allowed_models) if payload.allowed_models is not None else None,
                max_output_tokens=payload.max_output_tokens,
                spend_limit_microusd=payload.spend_limit_microusd,
            ))
            session.commit()
        return {"id": parsed.key_id, "name": payload.name, "key": raw, "warning": "仅显示一次",
                "allowed_models": payload.allowed_models, "max_output_tokens": payload.max_output_tokens,
                "spend_limit_microusd": payload.spend_limit_microusd}

    @app.get("/v1/keys")
    def list_keys(principal: Principal = Depends(require_session)) -> dict[str, Any]:
        with app.state.SessionLocal() as session:
            records = session.scalars(select(ApiKey).where(ApiKey.user_id == principal.user_id).order_by(ApiKey.created_at)).all()
            usage = key_usage(session, principal.user_id)
        return {"keys": [{
            "id": item.id,
            "name": item.name,
            "status": item.status,
            "last_four": item.last_four,
            "created_at": item.created_at.isoformat(),
            "last_used_at": item.last_used_at.isoformat() if item.last_used_at else None,
            "allowed_models": json.loads(item.allowed_models_json) if item.allowed_models_json is not None else None,
            "max_output_tokens": item.max_output_tokens,
            "spend_limit_microusd": item.spend_limit_microusd,
            "spent_microusd": usage.get(item.id, {}).get("spent_microusd", 0),
            "reserved_microusd": usage.get(item.id, {}).get("reserved_microusd", 0),
            "available_microusd": max(0, item.spend_limit_microusd - sum(usage.get(item.id, {}).values())) if item.spend_limit_microusd is not None else None,
        } for item in records]}

    @app.post("/v1/keys/revoke", status_code=204)
    def revoke_key(payload: KeyRevokeRequest, principal: Principal = Depends(require_session)) -> Response:
        key_id = payload.key_id
        if payload.key:
            parsed = parse_api_key(payload.key)
            if parsed is None:
                raise HTTPException(status_code=404, detail="API Key 不存在")
            key_id = parsed.key_id
        with app.state.SessionLocal() as session:
            result = session.execute(update(ApiKey).where(
                ApiKey.id == key_id,
                ApiKey.user_id == principal.user_id,
                ApiKey.status.in_(("active", "frozen")),
            ).values(status="revoked", revoked_at=utcnow()))
            if result.rowcount != 1:
                session.rollback()
                raise HTTPException(status_code=404, detail="API Key 不存在或已吊销")
            session.commit()
        return Response(status_code=204)

    def _connection_dict(item: ProviderConnection) -> dict[str, Any]:
        # Never add vault_ref or a secret-derived fingerprint to this boundary.
        return {
            "id": item.id,
            "provider": item.provider,
            "label": item.label,
            "status": item.status,
            "credential_version": item.credential_version,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
            "revoked_at": item.revoked_at.isoformat() if item.revoked_at else None,
        }

    @app.get("/v1/provider-connections")
    def list_provider_connections(principal: Principal = Depends(require_session)) -> dict[str, Any]:
        if settings.gateway_mode == "managed_gateway":
            raise HTTPException(403, "平台模式不接收客户上游密钥")
        with app.state.SessionLocal() as session:
            rows = session.scalars(select(ProviderConnection).where(
                ProviderConnection.user_id == principal.user_id,
            ).order_by(ProviderConnection.created_at)).all()
        return {"data": [_connection_dict(row) for row in rows]}

    @app.put("/v1/provider-connections/{provider}")
    def put_provider_connection(
        provider: str,
        payload: ProviderConnectionPutRequest,
        principal: Principal = Depends(require_session),
    ) -> JSONResponse:
        provider = provider.strip().casefold()
        if settings.gateway_mode == "managed_gateway":
            raise HTTPException(403, "平台模式不接收客户上游密钥")
        if provider not in providers.BYOK_PROVIDER_CATALOG:
            raise HTTPException(status_code=404, detail="Provider 不在服务端允许目录中")
        secret = payload.secret.get_secret_value()
        if app.state.credential_vault.manages_metadata:
            try:
                result = app.state.credential_vault.provision(
                    user_id=principal.user_id, provider=provider, label=payload.label, secret=secret,
                )
            except SecretUnavailable as exc:
                raise HTTPException(status_code=503, detail="凭据 Vault 不可用") from exc
            finally:
                del secret
            return JSONResponse(
                status_code=201 if result.credential_version == 1 else 200,
                content={"id": result.id, "provider": result.provider, "label": result.label,
                         "status": result.status, "credential_version": result.credential_version,
                         "created_at": result.created_at, "updated_at": result.updated_at,
                         "revoked_at": result.revoked_at},
            )
        with app.state.SessionLocal() as session:
            item = session.scalar(select(ProviderConnection).where(
                ProviderConnection.user_id == principal.user_id,
                ProviderConnection.provider == provider,
            ).with_for_update())
            if item is None:
                connection_id = str(uuid.uuid4())
                credential_version = 1
                item = ProviderConnection(
                    id=connection_id, user_id=principal.user_id, provider=provider,
                    label=payload.label, status="provisioning", credential_version=credential_version,
                )
                session.add(item)
                action = "created"
                status_code = 201
            else:
                if item.status == "revoked_pending_destroy":
                    raise HTTPException(status_code=409, detail="凭据连接正在等待 Vault 清理，请稍后重试")
                connection_id = item.id
                credential_version = item.credential_version + 1
                if item.status == "revoked":
                    # The Vault definer accepts a reconnect only when the
                    # provisioning row already bears the *new* version.  Keep
                    # this mutation in the same transaction as ``put``: a
                    # Vault failure rolls the row back to its revoked state.
                    item.status = "provisioning"
                    item.credential_version = credential_version
                    item.label = payload.label
                    item.revoked_at = None
                    action = "reconnected"
                elif item.status == "active":
                    old_connection = item.credential_version
                    action = "rotated"
                else:
                    raise HTTPException(status_code=409, detail="凭据连接状态不可操作")
                status_code = 200
            session.flush()
            try:
                app.state.credential_vault.put(
                    user_id=principal.user_id, connection_id=connection_id, provider=provider,
                    credential_version=credential_version, secret=secret,
                )
            except SecretUnavailable as exc:
                session.rollback()
                raise HTTPException(status_code=503, detail="凭据 Vault 不可用") from exc
            finally:
                del secret
            if action in ("created", "reconnected"):
                item.status = "active"
                if action == "reconnected":
                    item.updated_at = utcnow()
            else:
                item.label = payload.label
                item.revoked_at = None
                item.credential_version = credential_version
                item.updated_at = utcnow()
            if action == "rotated":
                app.state.credential_vault.destroy(
                    user_id=principal.user_id, connection_id=connection_id, provider=provider,
                    credential_version=old_connection,
                )
            session.add(CredentialActionAudit(
                id=str(uuid.uuid4()), user_id=principal.user_id, connection_id=item.id,
                action=action, credential_version=item.credential_version,
            ))
            response = _connection_dict(item)
            session.commit()
        return JSONResponse(status_code=status_code, content=response)

    @app.delete("/v1/provider-connections/{provider}", status_code=204)
    def revoke_provider_connection(provider: str, principal: Principal = Depends(require_session)) -> Response:
        if settings.gateway_mode == "managed_gateway":
            raise HTTPException(403, "平台模式不使用客户上游连接")
        provider = provider.strip().casefold()
        if app.state.credential_vault.manages_metadata:
            try:
                app.state.credential_vault.revoke(user_id=principal.user_id, provider=provider)
            except SecretUnavailable as exc:
                raise HTTPException(status_code=503, detail="凭据 Vault 不可用") from exc
            return Response(status_code=204)
        with app.state.SessionLocal() as session:
            item = session.scalar(select(ProviderConnection).where(
                ProviderConnection.user_id == principal.user_id,
                ProviderConnection.provider == provider,
                ProviderConnection.status.in_(("active", "revoked_pending_destroy")),
            ).with_for_update())
            if item is None:
                raise HTTPException(status_code=404, detail="Provider connection 不存在")
            was_pending = item.status == "revoked_pending_destroy"
            item.status = "revoked_pending_destroy"
            item.revoked_at = item.revoked_at or utcnow()
            item.updated_at = item.revoked_at
            connection_id = item.id
            credential_version = item.credential_version
            session.commit()
        try:
            app.state.credential_vault.destroy(
                user_id=principal.user_id, connection_id=connection_id, provider=provider,
                credential_version=credential_version,
            )
        except SecretUnavailable:
            # Keep the opaque reference for maintenance retry; never claim
            # physical deletion when the Vault operation failed.
            return JSONResponse(status_code=202, content={"status": "revoked_pending_destroy"})
        with app.state.SessionLocal() as session:
            item = session.get(ProviderConnection, connection_id, with_for_update=True)
            if item is not None and item.status == "revoked_pending_destroy":
                item.status = "revoked"
                session.add(CredentialActionAudit(
                    id=str(uuid.uuid4()), user_id=principal.user_id,
                    connection_id=connection_id, action="revoked",
                    credential_version=credential_version,
                ))
                session.commit()
        return Response(status_code=204)

    @app.post("/billing/topups", status_code=201)
    def create_topup(payload: TopupRequest, principal: Principal = Depends(require_session)) -> dict[str, Any]:
        if not settings.enable_test_payments:
            raise HTTPException(status_code=403, detail="测试充值未开启；正式支付适配器尚未配置")
        with app.state.SessionLocal() as session:
            try:
                order = create_test_order(session, principal.user_id, payload.amount)
            except PaymentError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {
            "id": order.id,
            "amount": order.amount_microusd,
            "currency": order.currency,
            "status": order.status,
            "payment_mode": "test_hmac",
        }

    @app.get("/billing/packages")
    def live_payment_packages() -> dict[str, Any]:
        return {"packages": [{
            "sku": sku,
            "payment_amount_minor": int(package["payment_amount_minor"]),
            "payment_currency": str(package["payment_currency"]),
            "credit_amount_microusd": int(package["credit_amount_microusd"]),
        } for sku, package in sorted(settings.topup_packages.items())]}

    @app.post("/billing/checkout", status_code=201)
    async def create_live_checkout(
        payload: LiveCheckoutRequest,
        request: Request,
        principal: Principal = Depends(require_session),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        if app.state.live_payment_bridge is None or not settings.payment_provider:
            raise HTTPException(status_code=403, detail="正式支付未启用")
        if (
            not idempotency_key
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", idempotency_key)
        ):
            raise HTTPException(status_code=422, detail="必须提供有效的 Idempotency-Key")
        if payload.return_url is not None:
            requested = urlparse(payload.return_url)
            configured = urlparse(settings.public_base_url)
            if (
                requested.scheme != "https"
                or not requested.hostname
                or requested.username
                or requested.password
                or (requested.scheme, requested.hostname, requested.port)
                != (configured.scheme, configured.hostname, configured.port)
            ):
                raise HTTPException(status_code=422, detail="支付回跳地址必须属于已配置公开站点")
        package = settings.topup_packages.get(payload.sku)
        if package is None:
            raise HTTPException(status_code=404, detail="充值套餐不存在")
        await asyncio.to_thread(
            enforce_auth_rate_limit,
            request,
            "checkout",
            principal.user_id,
            settings.checkout_rate_limit_per_minute,
        )
        with app.state.SessionLocal() as session:
            try:
                # Registration in development also marks email as verified, so
                # this invariant is shared by test/staging/production.
                from .services.identity import enforce_verified_user
                enforce_verified_user(session, principal.user_id)
                payment_service = PaymentDomainService(session)
                order = payment_service.create_order(
                    user_id=principal.user_id,
                    provider=settings.payment_provider,
                    payment_amount_minor=int(package["payment_amount_minor"]),
                    payment_currency=str(package["payment_currency"]),
                    credit_amount_microusd=int(package["credit_amount_microusd"]),
                    quote_id=str(package.get("quote_id") or payload.sku),
                    quote_numerator=int(package["credit_amount_microusd"]),
                    quote_denominator=int(package["payment_amount_minor"]),
                    idempotency_key=idempotency_key,
                    max_open_orders=settings.max_open_checkout_orders,
                )
                order, claimed = payment_service.prepare_checkout(order_id=order.id)
            except IdentityError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            except PaymentDomainError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            order_id = order.id
            if not claimed:
                return {
                    "id": order.id,
                    "status": order.status,
                    "checkout_url": order.checkout_url,
                    "payment_amount_minor": order.payment_amount_minor,
                    "payment_currency": order.payment_currency,
                    "credit_amount_microusd": order.credit_amount_microusd,
                }
            cash_amount = int(order.payment_amount_minor or 0)
            cash_currency = str(order.payment_currency or "")
        # Provider I/O is deliberately outside every database transaction.
        try:
            checkout = await app.state.live_payment_bridge.create_checkout(
                order_id=order_id,
                payment_amount_minor=cash_amount,
                currency=cash_currency,
                return_url=payload.return_url,
                idempotency_key=idempotency_key,
            )
        except PaymentBridgeError as exc:
            with app.state.SessionLocal() as session:
                session.execute(update(PaymentOrder).where(
                    PaymentOrder.id == order_id,
                    PaymentOrder.status == "checkout_requesting",
                ).values(
                    status="pending_reconciliation",
                    risk_reason=exc.code,
                    checkout_claim_started_at=None,
                ))
                session.commit()
            raise HTTPException(status_code=503, detail="支付创建状态不确定，已转人工对账") from exc
        # The checkout response is not a settlement confirmation.  A provider
        # returning anything other than ``pending`` (for example ``paid`` or
        # ``closed``) must be reconciled before we expose a result to the
        # customer; otherwise a provider-side state could be mistaken for a
        # locally accepted checkout and later be credited twice.
        if checkout.status != "pending":
            with app.state.SessionLocal() as session:
                changed = session.execute(update(PaymentOrder).where(
                    PaymentOrder.id == order_id,
                    PaymentOrder.status == "checkout_requesting",
                    PaymentOrder.provider_transaction_id.is_(None),
                ).values(
                    status="pending_reconciliation",
                    provider_transaction_id=checkout.provider_transaction_id,
                    checkout_url=checkout.checkout_url,
                    checkout_claim_started_at=None,
                    risk_reason=f"checkout_provider_status:{checkout.status}",
                ))
                if changed.rowcount != 1:
                    session.rollback()
                    raise HTTPException(status_code=409, detail="订单状态已变化，请查询订单")
                session.commit()
            raise HTTPException(status_code=503, detail="支付创建状态需对账，暂不可确认")
        with app.state.SessionLocal() as session:
            changed = session.execute(update(PaymentOrder).where(
                PaymentOrder.id == order_id,
                PaymentOrder.status == "checkout_requesting",
                PaymentOrder.provider_transaction_id.is_(None),
            ).values(
                status="pending",
                provider_transaction_id=checkout.provider_transaction_id,
                checkout_url=checkout.checkout_url,
                checkout_claim_started_at=None,
                risk_reason=None,
            ))
            if changed.rowcount != 1:
                session.rollback()
                raise HTTPException(status_code=409, detail="订单状态已变化，请查询订单")
            session.commit()
        return {
            "id": order_id,
            "status": checkout.status,
            "checkout_url": checkout.checkout_url,
            "payment_amount_minor": cash_amount,
            "payment_currency": cash_currency,
            "credit_amount_microusd": int(package["credit_amount_microusd"]),
        }

    @app.post("/billing/live/webhook")
    async def live_payment_webhook(request: Request) -> dict[str, Any]:
        if app.state.live_payment_bridge is None or not settings.payment_provider:
            raise HTTPException(status_code=404, detail="正式支付回调未启用")
        raw_body = await request.body()
        try:
            event = app.state.live_payment_bridge.verify_webhook(raw_body, request.headers)
        except PaymentBridgeError as exc:
            raise HTTPException(status_code=401, detail="支付回调验证失败") from exc
        with app.state.SessionLocal() as session:
            try:
                duplicate = PaymentDomainService(session).apply_webhook(
                    provider=settings.payment_provider,
                    event_id=event.event_id,
                    nonce=event.nonce,
                    raw_digest=hashlib.sha256(raw_body).hexdigest(),
                    order_id=event.order_id,
                    event_type=event.event_type,
                    status=event.status,
                    payment_amount_minor=event.payment_amount_minor,
                    payment_currency=event.currency,
                    provider_transaction_id=event.provider_transaction_id,
                    provider_refund_id=event.provider_refund_id,
                    provider_dispute_id=event.provider_dispute_id,
                    provider_return_id=event.provider_return_id,
                )
            except PaymentDomainError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {"received": True, "duplicate": duplicate}

    @app.post("/billing/webhook")
    async def payment_webhook(request: Request, x_webhook_signature: str = Header(default="")) -> dict[str, Any]:
        if not settings.enable_test_payments:
            raise HTTPException(status_code=404, detail="支付回调未启用")
        raw_body = await request.body()
        with app.state.SessionLocal() as session:
            try:
                duplicate = process_test_webhook(
                    session,
                    raw_body=raw_body,
                    signature=x_webhook_signature,
                    secret=settings.payment_webhook_secret,
                )
            except PaymentError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return {"received": True, "duplicate": duplicate}

    @app.get("/billing/balance")
    def balance(principal: Principal = Depends(require_session)) -> dict[str, Any]:
        with app.state.SessionLocal() as session:
            wallet = session.get(Wallet, principal.user_id)
            if wallet is None:
                raise HTTPException(status_code=404, detail="钱包不存在")
            return {
                "balance": wallet.balance_microusd,
                "reserved": wallet.reserved_microusd,
                "currency": wallet.currency,
            }

    @app.get("/billing/topups")
    def topup_orders(principal: Principal = Depends(require_session)) -> dict[str, Any]:
        with app.state.SessionLocal() as session:
            records = session.scalars(select(PaymentOrder).where(
                PaymentOrder.user_id == principal.user_id,
            ).order_by(PaymentOrder.created_at.desc()).limit(50)).all()
        return {"orders": [{
            "id": item.id,
            "amount": item.amount_microusd,
            "currency": item.currency,
            "credit_amount_microusd": item.credit_amount_microusd,
            "payment_amount_minor": item.payment_amount_minor,
            "payment_currency": item.payment_currency,
            "status": item.status,
            "payment_mode": item.provider,
            "created_at": item.created_at.isoformat(),
        } for item in records]}

    def customer_order_snapshot(user_id: str, criterion) -> JSONResponse:
        # Recovery only reads our order snapshot. It never creates a payment
        # intent, calls a supplier, or interprets a browser redirect as payment.
        with app.state.SessionLocal() as session:
            order = session.scalar(select(PaymentOrder).where(
                PaymentOrder.user_id == user_id, criterion,
            ))
            if order is None:
                raise HTTPException(404, "订单未找到；原请求可能仍在途中，请勿改用新编号自动重试")
            checkout_url = public_https_url(order.checkout_url) if order.status == "pending" else None
            action = "contact_support"
            if checkout_url:
                action = "resume_checkout"
            elif order.status == "checkout_requesting":
                action = "wait_and_query"
            elif order.status == "paid":
                action = "check_balance"
            elif order.status in ("closed", "refunded", "expired", "failed"):
                action = "review_order"
            return JSONResponse({
                "id": order.id, "status": order.status, "next_action": action,
                "checkout_url": checkout_url,
                "payment_amount_minor": order.payment_amount_minor,
                "payment_currency": order.payment_currency,
                "credit_amount_microusd": order.credit_amount_microusd,
                "created_at": order.created_at.isoformat(),
            }, headers={"Cache-Control": "no-store"})

    @app.post("/billing/checkout/lookup")
    def lookup_customer_checkout(
        principal: Principal = Depends(require_session),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        if not idempotency_key or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", idempotency_key):
            raise HTTPException(422, "必须提供有效的 Idempotency-Key")
        return customer_order_snapshot(principal.user_id, PaymentOrder.client_idempotency_key == idempotency_key)

    @app.get("/billing/topups/{order_id}")
    def customer_order_detail(order_id: str, principal: Principal = Depends(require_session)) -> JSONResponse:
        return customer_order_snapshot(principal.user_id, PaymentOrder.id == order_id)

    @app.get("/billing/ledger")
    def ledger(principal: Principal = Depends(require_session)) -> dict[str, Any]:
        with app.state.SessionLocal() as session:
            rows = session.execute(select(LedgerEntry, LedgerTransaction).join(
                LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id,
            ).where(
                LedgerEntry.user_id == principal.user_id,
                LedgerEntry.account == CUSTOMER_AVAILABLE,
            ).order_by(LedgerEntry.created_at)).all()
        return {"entries": [{
            "id": entry.id,
            "transaction_id": transaction.id,
            "kind": transaction.kind,
            "reference": transaction.reference,
            "amount": entry.amount_microusd,
            "created_at": entry.created_at.isoformat(),
        } for entry, transaction in rows]}

    @app.post("/budgets", status_code=201)
    def create_budget(payload: BudgetAmountRequest, principal: Principal = Depends(require_session)) -> dict[str, Any]:
        with app.state.SessionLocal() as session:
            try:
                budget = budget_service.create_budget(
                    session, principal.user_id, payload.amount, kind=payload.kind,
                )
            except budget_service.BudgetError as exc:
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return _budget_dict(budget)

    @app.get("/budgets")
    def list_budgets(principal: Principal = Depends(require_session)) -> dict[str, Any]:
        with app.state.SessionLocal() as session:
            records = session.scalars(select(Budget).where(
                Budget.user_id == principal.user_id,
            ).order_by(Budget.created_at.desc()).limit(24)).all()
        return {"budgets": [_budget_dict(item) for item in records]}

    def _owned_budget(principal: Principal, budget_id: str):
        session = app.state.SessionLocal()
        budget = budget_service.get_owned_budget(session, principal.user_id, budget_id)
        if budget is None:
            session.close()
            raise HTTPException(status_code=404, detail="预算不存在")
        return session, budget

    def _budget_dict(budget: Budget) -> dict[str, Any]:
        return {
            "id": budget.id,
            "kind": budget.kind,
            "amount": budget.limit_microusd,
            "reserved": budget.reserved_microusd,
            "spent": budget.spent_microusd,
            "available": budget.limit_microusd - budget.spent_microusd - budget.reserved_microusd,
            "status": budget.status,
            "period_start": budget.period_start.isoformat(),
            "period_end": budget.period_end.isoformat(),
        }

    @app.get("/budgets/{budget_id}")
    def get_budget(budget_id: str, principal: Principal = Depends(require_session)) -> dict[str, Any]:
        session, budget = _owned_budget(principal, budget_id)
        try:
            return _budget_dict(budget)
        finally:
            session.close()

    @app.get("/v1/models")
    def models(_principal: Principal = Depends(require_api_key)) -> dict[str, Any]:
        with app.state.SessionLocal() as session:
            key = session.get(ApiKey, _principal.api_key_id)
            if key is None or key.status != "active":
                raise HTTPException(401, "API Key 已失效")
            query = select(ModelPrice).where(ModelPrice.active.is_(True))
            if key.allowed_models_json is not None:
                query = query.where(ModelPrice.model.in_(json.loads(key.allowed_models_json)))
            records = session.scalars(query.order_by(ModelPrice.model)).all()
        return {"object": "list", "data": [{
            "id": item.model,
            "object": "model",
            "created": int(item.effective_at.timestamp()),
            "owned_by": "kunlun-gateway",
        } for item in records]}

    @app.post("/v1/chat/completions")
    async def chat_completions(
        payload: ChatCompletionRequest,
        principal: Principal = Depends(require_api_key),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        # Keep the key in the same bounded ASCII namespace as payment commands.
        # Reject before provider selection or reservation so malformed retries
        # cannot create billing state or reach an upstream.
        if ((settings.is_production and settings.gateway_mode == "byok") or settings.gateway_mode == "managed_gateway") and not idempotency_key:
            return _openai_error(428, "此网关模式要求提供 Idempotency-Key", "idempotency_key_required")
        if idempotency_key is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", idempotency_key,
        ):
            return _openai_error(422, "Idempotency-Key 格式无效", "invalid_idempotency_key")
        max_output = payload.max_completion_tokens or payload.max_tokens or settings.default_output_tokens
        if max_output > settings.max_output_tokens:
            return _openai_error(422, "输出 Token 上限超过平台策略", "max_output_tokens_exceeded")
        if settings.gateway_mode == "disabled":
            return _openai_error(503, "网关当前已禁用", "gateway_disabled")
        with app.state.SessionLocal() as session:
            key = session.get(ApiKey, principal.api_key_id)
            try:
                enforce_key_policy(key, payload.model, max_output)
            except BillingError as exc:
                return _openai_error(exc.status_code, str(exc), "key_policy_rejected")
        if idempotency_key:
            with app.state.SessionLocal() as session:
                recorded = recorded_request_response(session, principal.user_id, idempotency_key)
            if recorded is not None:
                return recorded
        byok_candidates: list[tuple[ProviderConnection, dict[str, Any]]] = []
        if settings.gateway_mode == "byok":
            # Select tenant-owned metadata by authenticated principal only.
            # Secret resolution happens only after the durable budget hold,
            # immediately before each bounded outbound attempt.
            with app.state.SessionLocal() as session:
                connections = session.scalars(select(ProviderConnection).where(
                    ProviderConnection.user_id == principal.user_id,
                    ProviderConnection.status == "active",
                ).order_by(ProviderConnection.updated_at.desc())).all()
            if not connections:
                return _openai_error(503, "未配置可用的 BYOK Provider connection", "byok_credential_unavailable")
            for connection in connections:
                catalog = next((item for item in settings.providers if isinstance(item, dict) and str(item.get("name", "")).casefold() == connection.provider), None)
                if catalog is not None and payload.model in set(catalog.get("models") or []):
                    byok_candidates.append((connection, catalog))
            if not byok_candidates:
                return _openai_error(503, "当前模型没有可用的 BYOK Provider", "no_provider")
            # The DB price is the durable pre-authorisation ceiling. Its value
            # must cover every route this process could use, including a route
            # from an older rolling-deploy instance.
            candidate_input_floor = max(
                int(catalog["pricing"][payload.model]["input_microusd_per_million"])
                for _connection, catalog in byok_candidates
            )
            candidate_output_floor = max(
                int(catalog["pricing"][payload.model]["output_microusd_per_million"])
                for _connection, catalog in byok_candidates
            )
            eligible: list[Any] = byok_candidates
        elif settings.gateway_mode == "managed_gateway":
            eligible = [item for item in settings.providers if payload.model in item.get("models", [])]
            candidate_input_floor = max((item["pricing"][payload.model]["input_microusd_per_million"] for item in eligible), default=0)
            candidate_output_floor = max((item["pricing"][payload.model]["output_microusd_per_million"] for item in eligible), default=0)
        else:
            candidate_input_floor = None
            candidate_output_floor = None
            eligible = [client for client in providers.ordered_clients if providers.supports_model(client, payload.model)]
        if not eligible:
            return _openai_error(503, "当前没有可用且符合策略的模型供应商", "no_provider")
        messages = [message.model_dump(exclude_none=True) for message in payload.messages]
        upstream_payload = payload.model_dump(exclude_none=True)
        upstream_payload["messages"] = messages
        direct_upstream_stream = bool(payload.stream and app.state.content_safety is None)
        upstream_payload["stream"] = direct_upstream_stream
        upstream_payload.pop("max_completion_tokens", None)
        upstream_payload["max_tokens"] = max_output
        if direct_upstream_stream:
            upstream_payload["stream_options"] = {"include_usage": True}
        else:
            upstream_payload.pop("stream_options", None)
        billable_payload = {
            key: value for key, value in upstream_payload.items()
            if key not in {"stream", "stream_options"}
        }
        # Reserve before moderation too: a rejected budget must not trigger
        # any paid/network service. A safety rejection releases the hold.
        with app.state.SessionLocal() as session:
            try:
                reservation = (
                    reserve_byok_model_request(
                        session, user_id=principal.user_id, api_key_id=principal.api_key_id or "",
                        model=payload.model, billable_payload=billable_payload,
                        max_output_tokens=max_output, idempotency_key=idempotency_key,
                        minimum_input_price=candidate_input_floor,
                        minimum_output_price=candidate_output_floor,
                    ) if settings.gateway_mode == "byok" else reserve_model_request(
                        session, user_id=principal.user_id, api_key_id=principal.api_key_id or "",
                        model=payload.model, billable_payload=billable_payload,
                        max_output_tokens=max_output, idempotency_key=idempotency_key,
                        managed_cost_prices=(candidate_input_floor, candidate_output_floor) if settings.gateway_mode == "managed_gateway" else None,
                        platform_daily_limit=settings.platform_daily_budget_microusd,
                    )
                )
            except BillingError as exc:
                if exc.status_code == 409 and idempotency_key:
                    session.rollback()
                    recorded = recorded_request_response(session, principal.user_id, idempotency_key)
                    if recorded is not None:
                        return recorded
                return _openai_error(exc.status_code, str(exc), "billing_rejected")
        if app.state.content_safety is not None:
            try:
                input_decision: SafetyDecision = await app.state.content_safety.check(
                    kind="input",
                    model=payload.model,
                    # Inspect the exact normalized object that will be sent
                    # upstream, including tool descriptions and output schema.
                    content=upstream_payload,
                )
                input_outcome = "allowed" if input_decision.allowed else "blocked"
                input_reason = input_decision.reason_code
                input_decision_id = input_decision.decision_id
            except ContentSafetyError:
                input_decision = SafetyDecision(False, "service_unavailable")
                input_outcome = "unavailable"
                input_reason = "service_unavailable"
                input_decision_id = None
            input_audit_id = str(uuid.uuid4())
            with app.state.SessionLocal() as session:
                session.add(SafetyAudit(
                    id=input_audit_id,
                    user_id=principal.user_id,
                    api_key_id=principal.api_key_id or "",
                    request_id=reservation.request_id,
                    phase="input",
                    outcome=input_outcome,
                    reason_code=input_reason,
                    decision_id=input_decision_id,
                    policy_version=settings.content_safety_policy_version or None,
                ))
                session.commit()
                if not input_decision.allowed:
                    release_model_request(session, reservation.request_id, "input_safety_" + input_outcome)
            if input_outcome == "unavailable":
                return _openai_error(503, "内容安全服务不可用，已拒绝本次请求", "safety_unavailable", request_id=reservation.request_id)
            if not input_decision.allowed:
                return _openai_error(403, "请求未通过内容安全策略", "content_policy_rejected", request_id=reservation.request_id)
        last_error: ProviderError | None = None
        route_deadline = time.monotonic() + request_limits.MANAGED_REQUEST_SECONDS if settings.gateway_mode == "managed_gateway" else None
        for index, candidate in enumerate(eligible):
            if route_deadline is not None and time.monotonic() >= route_deadline:
                # Reached only before the first attempt or after an explicitly
                # non-billable failure. No uncertain request reaches this branch.
                with app.state.SessionLocal() as session:
                    release_model_request(session, reservation.request_id, "routing_deadline_exceeded")
                return _openai_error(504, "路由总时限已耗尽，未继续外呼", "routing_deadline_exceeded", request_id=reservation.request_id)
            attempt_started_at = utcnow()
            connection: ProviderConnection | None = None
            attempt_metadata: dict[str, Any] = {}
            if settings.gateway_mode == "byok":
                connection, catalog = candidate
                try:
                    transient_secret = app.state.credential_vault.get(
                        user_id=principal.user_id, connection_id=connection.id, provider=connection.provider,
                        credential_version=connection.credential_version,
                    )
                    client = providers.build_byok_provider_client(
                        catalog, api_key=transient_secret, allowed_hosts=settings.provider_host_allowlist,
                    )
                except (SecretUnavailable, RuntimeError):
                    with app.state.SessionLocal() as session:
                        attempt_id = record_attempt(
                            session, request_id=reservation.request_id, ordinal=index + 1,
                            provider=connection.provider, model=payload.model, status="failed",
                            failure_category="byok_credential_unavailable",
                            credential_connection_id=connection.id, credential_version=connection.credential_version,
                            pricing_snapshot={"source": "provider_catalog"}, started_at=attempt_started_at,
                            billing_status="not_billed",
                        )
                    if index + 1 < len(eligible):
                        continue
                    with app.state.SessionLocal() as session:
                        release_model_request(
                            session, reservation.request_id, "byok_credential_unavailable", attempt_id=attempt_id,
                        )
                    return _openai_error(503, "凭据 Vault 或 Provider 目录不可用", "byok_credential_unavailable", request_id=reservation.request_id)
                finally:
                    if "transient_secret" in locals():
                        del transient_secret
                attempt_metadata = {
                    "credential_connection_id": connection.id,
                    "credential_version": connection.credential_version,
                    "pricing_snapshot": {"source": "provider_catalog"},
                }
            elif settings.gateway_mode == "managed_gateway":
                catalog = candidate
                try:
                    transient_secret, channel_id, channel_version = app.state.platform_vault.resolve(catalog["name"])
                    client = providers.build_managed_provider_client(catalog, api_key=transient_secret, allowed_hosts=settings.provider_host_allowlist)
                    attempt_metadata = {"credential_connection_id": channel_id, "credential_version": channel_version}
                except (SecretUnavailable, RuntimeError):
                    with app.state.SessionLocal() as session:
                        attempt_id = record_attempt(session, request_id=reservation.request_id, ordinal=index + 1,
                            provider=catalog["name"], model=payload.model, status="failed", billing_status="not_billed",
                            failure_category="platform_credential_unavailable")
                    if index + 1 < len(eligible):
                        continue
                    with app.state.SessionLocal() as session:
                        release_model_request(session, reservation.request_id, "platform_credential_unavailable", attempt_id=attempt_id)
                    return _openai_error(503, "平台供应渠道不可用", "platform_credential_unavailable", request_id=reservation.request_id)
                finally:
                    if "transient_secret" in locals():
                        del transient_secret
            else:
                client = candidate
            name = providers.provider_name(client, index)
            upstream_input_price, upstream_output_price = providers.upstream_prices(
                client,
                payload.model,
                reservation.input_price,
                reservation.output_price,
            )
            attempt_metadata["pricing_snapshot"] = {
                "input_microusd_per_million": upstream_input_price,
                "output_microusd_per_million": upstream_output_price,
            }
            attempt_id: str | None = None
            try:
                open_stream = getattr(type(client), "open_stream", None)
                if direct_upstream_stream and callable(open_stream):
                    upstream_stream = await request_limits.await_with_deadline(open_stream(client, upstream_payload), route_deadline)
                    try:
                        with app.state.SessionLocal() as session:
                            attempt_id = record_attempt(
                                session,
                                request_id=reservation.request_id,
                                ordinal=index + 1,
                                provider=name,
                                model=payload.model,
                                status="stream_opened",
                                status_code=200,
                                started_at=attempt_started_at,
                                **attempt_metadata,
                            )
                    except Exception:
                        try:
                            await upstream_stream.close()
                        except Exception:
                            logger.error(
                                "gateway stream close failure request_id=%s phase=attempt_record",
                                reservation.request_id,
                            )
                        raise
                    tracker = SSEUsageTracker()

                    async def forward_stream():
                        try:
                            async for chunk in request_limits.chunks_with_deadline(upstream_stream.chunks(), route_deadline):
                                tracker.feed(chunk)
                                yield chunk
                            tracker.finish()
                            if not tracker.done:
                                with app.state.SessionLocal() as session:
                                    mark_pending_reconciliation(
                                        session,
                                        reservation.request_id,
                                        "provider_stream_incomplete",
                                        provider=name,
                                        fallback_count=index,
                                        attempt_id=attempt_id,
                                    )
                                return
                            settlement_response, estimated = tracker.settlement_response(
                                payload.model,
                                reservation.estimated_input_tokens,
                            )
                            with app.state.SessionLocal() as session:
                                try:
                                    settle_model_request(
                                        session,
                                        request_id=reservation.request_id,
                                        response=settlement_response,
                                        provider=name,
                                        fallback_count=index,
                                        force_usage_estimated=estimated,
                                        upstream_input_price=upstream_input_price,
                                        upstream_output_price=upstream_output_price,
                                        attempt_id=attempt_id,
                                    )
                                except BillingError:
                                    mark_pending_reconciliation(
                                        session,
                                        reservation.request_id,
                                        "settlement_failed",
                                        provider=name,
                                        fallback_count=index,
                                        attempt_id=attempt_id,
                                    )
                        except asyncio.CancelledError:
                            with app.state.SessionLocal() as session:
                                mark_pending_reconciliation(
                                    session,
                                    reservation.request_id,
                                    "client_disconnected",
                                    provider=name,
                                    fallback_count=index,
                                    attempt_id=attempt_id,
                                )
                            raise
                        except (StreamProtocolError, ProviderError):
                            with app.state.SessionLocal() as session:
                                mark_pending_reconciliation(
                                    session,
                                    reservation.request_id,
                                    "provider_stream_failed",
                                    provider=name,
                                    fallback_count=index,
                                    attempt_id=attempt_id,
                                )
                        except Exception:
                            logger.error(
                                "gateway stream failure request_id=%s category=unexpected",
                                reservation.request_id,
                            )
                            with app.state.SessionLocal() as session:
                                mark_pending_reconciliation(
                                    session,
                                    reservation.request_id,
                                    "unexpected_stream_failure",
                                    provider=name,
                                    fallback_count=index,
                                    attempt_id=attempt_id,
                                )
                        finally:
                            try:
                                await upstream_stream.close()
                            except Exception:
                                logger.error(
                                    "gateway stream close failure request_id=%s phase=finalize",
                                    reservation.request_id,
                                )

                    return StreamingResponse(
                        forward_stream(),
                        media_type="text/event-stream",
                        headers={
                            "X-Request-Id": reservation.request_id,
                            "X-Kunlun-Provider": name,
                            "X-Accel-Buffering": "no",
                            "Cache-Control": "no-cache, no-store",
                        },
                    )
                result = await request_limits.await_with_deadline(client(upstream_payload), route_deadline)
                if not isinstance(result, dict):
                    raise ProviderError(502, category="provider_invalid_payload", safe_to_failover=False, request_may_be_billable=True)
                with app.state.SessionLocal() as session:
                    attempt_id = record_attempt(
                        session,
                        request_id=reservation.request_id,
                        ordinal=index + 1,
                        provider=name,
                        model=payload.model,
                        status="succeeded",
                        status_code=200,
                        started_at=attempt_started_at,
                        **attempt_metadata,
                    )
                output_outcome: str | None = None
                if app.state.content_safety is not None:
                    try:
                        output_decision: SafetyDecision = await app.state.content_safety.check(
                            kind="output",
                            model=payload.model,
                            content=result.get("choices", []),
                        )
                        output_outcome = "allowed" if output_decision.allowed else "blocked"
                        output_reason = output_decision.reason_code
                        output_decision_id = output_decision.decision_id
                    except ContentSafetyError:
                        output_decision = SafetyDecision(False, "service_unavailable")
                        output_outcome = "unavailable"
                        output_reason = "service_unavailable"
                        output_decision_id = None
                    with app.state.SessionLocal() as session:
                        session.add(SafetyAudit(
                            id=str(uuid.uuid4()),
                            user_id=principal.user_id,
                            api_key_id=principal.api_key_id or "",
                            request_id=reservation.request_id,
                            phase="output",
                            outcome=output_outcome,
                            reason_code=output_reason,
                            decision_id=output_decision_id,
                            policy_version=settings.content_safety_policy_version or None,
                        ))
                        session.commit()
                with app.state.SessionLocal() as session:
                    try:
                        settle_model_request(
                            session,
                            request_id=reservation.request_id,
                            response=result,
                            provider=name,
                            fallback_count=index,
                            upstream_input_price=upstream_input_price,
                            upstream_output_price=upstream_output_price,
                            attempt_id=attempt_id,
                        )
                    except BillingError:
                        mark_pending_reconciliation(
                            session,
                            reservation.request_id,
                            "settlement_failed",
                            provider=name,
                            fallback_count=index,
                            attempt_id=attempt_id,
                        )
                        return _openai_error(503, "调用成功但结算待人工对账", "settlement_pending", request_id=reservation.request_id)
                if output_outcome == "unavailable":
                    return _openai_error(
                        503,
                        "模型已完成并结算，但内容安全服务不可用，响应未返回",
                        "safety_unavailable",
                        request_id=reservation.request_id,
                    )
                if output_outcome == "blocked":
                    return _openai_error(
                        403,
                        "模型响应未通过内容安全策略",
                        "content_policy_rejected",
                        request_id=reservation.request_id,
                    )
                result.setdefault("id", "chatcmpl_" + reservation.request_id.replace("-", ""))
                result.setdefault("object", "chat.completion")
                result.setdefault("created", int(time.time()))
                result.setdefault("model", payload.model)
                if payload.stream:
                    return StreamingResponse(
                        synthesize_sse(result, reservation.request_id, payload.model),
                        media_type="text/event-stream",
                        headers={
                            "X-Request-Id": reservation.request_id,
                            "X-Kunlun-Provider": name,
                            "X-Accel-Buffering": "no",
                            "Cache-Control": "no-cache, no-store",
                        },
                    )
                return JSONResponse(
                    status_code=200,
                    content=result,
                    headers={"X-Request-Id": reservation.request_id, "X-Kunlun-Provider": name},
                )
            except ProviderError as exc:
                last_error = exc
                with app.state.SessionLocal() as session:
                    attempt_id = record_attempt(
                        session,
                        request_id=reservation.request_id,
                        ordinal=index + 1,
                        provider=name,
                        model=payload.model,
                        status="failed",
                        status_code=exc.status_code,
                        failure_category=exc.category,
                        started_at=attempt_started_at,
                        billing_status="unknown" if exc.request_may_be_billable else "not_billed",
                        **attempt_metadata,
                    )
                if exc.request_may_be_billable:
                    with app.state.SessionLocal() as session:
                        mark_pending_reconciliation(
                            session,
                            reservation.request_id,
                            exc.category,
                            provider=name,
                            fallback_count=index,
                            attempt_id=attempt_id,
                        )
                    return _openai_error(502, "上游状态不确定，已转人工对账且未自动切换", "reconciliation_pending", request_id=reservation.request_id)
                if exc.safe_to_failover and index + 1 < len(eligible):
                    continue
                with app.state.SessionLocal() as session:
                    release_model_request(session, reservation.request_id, exc.category, attempt_id=attempt_id)
                return _openai_error(exc.status_code, "模型供应商拒绝或不可用", exc.category, request_id=reservation.request_id)
            except asyncio.CancelledError:
                if attempt_id is None:
                    with app.state.SessionLocal() as session:
                        attempt_id = record_attempt(
                            session, request_id=reservation.request_id, ordinal=index + 1,
                            provider=name, model=payload.model, status="uncertain",
                            failure_category="client_disconnected", started_at=attempt_started_at,
                            billing_status="unknown", **attempt_metadata,
                        )
                with app.state.SessionLocal() as session:
                    mark_pending_reconciliation(
                        session,
                        reservation.request_id,
                        "client_disconnected",
                        provider=name,
                        fallback_count=index,
                        attempt_id=attempt_id,
                    )
                raise
            except Exception:
                # Never attach arbitrary exception text or traceback: provider SDK
                # exceptions can contain request bodies or Authorization headers.
                logger.error("gateway internal provider failure request_id=%s category=unexpected", reservation.request_id)
                if attempt_id is None:
                    with app.state.SessionLocal() as session:
                        attempt_id = record_attempt(
                            session, request_id=reservation.request_id, ordinal=index + 1,
                            provider=name, model=payload.model, status="uncertain",
                            failure_category="unexpected_provider_failure", started_at=attempt_started_at,
                            billing_status="unknown", **attempt_metadata,
                        )
                with app.state.SessionLocal() as session:
                    mark_pending_reconciliation(
                        session,
                        reservation.request_id,
                        "unexpected_provider_failure",
                        provider=name,
                        fallback_count=index,
                        attempt_id=attempt_id,
                    )
                return _openai_error(502, "上游状态不确定，已转人工对账", "reconciliation_pending", request_id=reservation.request_id)
        with app.state.SessionLocal() as session:
            release_model_request(session, reservation.request_id, last_error.category if last_error else "no_provider")
        return _openai_error(503, "所有允许的供应商均不可用", "all_providers_failed", request_id=reservation.request_id)

    def _cost_entries(user_id: str) -> list[dict[str, Any]]:
        with app.state.SessionLocal() as session:
            records = session.scalars(select(ModelRequest).where(
                ModelRequest.user_id == user_id,
            ).order_by(ModelRequest.created_at)).all()
        return [{
            "request_id": item.id,
            "model": item.requested_model,
            "provider": item.final_provider,
            "status": item.status,
            "amount": item.charged_microusd,
            "upstream_cost": item.upstream_cost_microusd,
            "reserved": item.reserved_microusd,
            "input_tokens": item.input_tokens,
            "output_tokens": item.output_tokens,
            "usage_estimated": item.usage_estimated,
            "fallback_count": item.fallback_count,
            "failure_category": item.failure_category,
            "created_at": item.created_at.isoformat(),
        } for item in records]

    @app.get("/billing/costs")
    def costs(principal: Principal = Depends(require_api_key)) -> dict[str, Any]:
        return {"entries": _cost_entries(principal.user_id)}

    @app.get("/billing/usage")
    def usage_for_console(principal: Principal = Depends(require_session)) -> dict[str, Any]:
        return {"entries": _cost_entries(principal.user_id)}

    @app.get("/ops/reconciliation")
    def reconciliation_queue(
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=1_000_000),
        _operator: OperatorClaims = Depends(require_operator_scope("reconciliation:read")),
    ) -> dict[str, Any]:
        with app.state.SessionLocal() as session:
            total = int(session.scalar(select(func.count(ModelRequest.id)).where(
                ModelRequest.status == "pending_reconciliation",
            )) or 0)
            records = session.scalars(select(ModelRequest).where(
                ModelRequest.status == "pending_reconciliation",
            ).order_by(ModelRequest.created_at, ModelRequest.id).offset(offset).limit(limit)).all()
        return {
            "requests": [{
                "request_id": item.id,
                "user_id": item.user_id,
                "model": item.requested_model,
                "provider": item.final_provider,
                "reserved": item.reserved_microusd,
                "failure_category": item.failure_category,
                "created_at": item.created_at.isoformat(),
            } for item in records],
            "pagination": {"limit": limit, "offset": offset, "total": total},
        }

    @app.get("/ops/payments/reconciliation")
    def payment_reconciliation_queue(
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=1_000_000),
        _operator: OperatorClaims = Depends(require_operator_scope("payments:read")),
    ) -> dict[str, Any]:
        """Discover durable payment and refund work without knowing IDs first."""
        order_statuses = ("checkout_requesting", "pending", "pending_reconciliation")
        refund_statuses = ("requesting", "retrying", "pending_reconciliation")
        now = utcnow()
        stale_cutoff = now - timedelta(minutes=5)
        with app.state.SessionLocal() as session:
            order_total = int(session.scalar(select(func.count(PaymentOrder.id)).where(
                PaymentOrder.status.in_(order_statuses),
            )) or 0)
            refund_total = int(session.scalar(select(func.count(PaymentRefund.id)).where(
                PaymentRefund.status.in_(refund_statuses),
            )) or 0)
            orders = session.scalars(select(PaymentOrder).where(
                PaymentOrder.status.in_(order_statuses),
            ).order_by(PaymentOrder.created_at, PaymentOrder.id).offset(offset).limit(limit)).all()
            refunds = session.scalars(select(PaymentRefund).where(
                PaymentRefund.status.in_(refund_statuses),
            ).order_by(PaymentRefund.created_at, PaymentRefund.id).offset(offset).limit(limit)).all()
        return {
            "orders": [{
                "order_id": item.id,
                "user_id": item.user_id,
                "status": item.status,
                "risk_reason": item.risk_reason,
                "provider_transaction_id": item.provider_transaction_id,
                "checkout_claim_stale": bool(
                    item.status == "checkout_requesting"
                    and item.checkout_claim_started_at is not None
                    and as_utc(item.checkout_claim_started_at) <= stale_cutoff
                ),
                "reconciliation_claim_active": bool(
                    item.reconciliation_claim_started_at is not None
                    and as_utc(item.reconciliation_claim_started_at) > stale_cutoff
                ),
                "created_at": item.created_at.isoformat(),
            } for item in orders],
            "refunds": [{
                "refund_id": item.id,
                "order_id": item.order_id,
                "user_id": item.user_id,
                "status": item.status,
                "risk_reason": item.risk_reason,
                # This private read scope may reveal the command key needed by
                # the existing leased retry route; it is never public or logged.
                "idempotency_key": item.idempotency_key,
                "claim_stale": as_utc(item.claim_started_at) <= stale_cutoff,
                "created_at": item.created_at.isoformat(),
            } for item in refunds],
            "pagination": {
                "limit": limit,
                "offset": offset,
                "order_total": order_total,
                "refund_total": refund_total,
            },
        }

    @app.post("/ops/accounts/{user_id}/status")
    def update_account_status(
        user_id: str,
        payload: AccountStatusRequest,
        request: Request,
        operator: OperatorClaims = Depends(require_operator_scope("accounts:write")),
    ) -> dict[str, Any]:
        with app.state.SessionLocal() as session:
            user = session.scalar(
                select(User)
                .where(User.id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if user is None:
                raise HTTPException(status_code=404, detail="账户不存在")
            before_status = user.status
            if payload.expected_status is not None and payload.expected_status != before_status:
                raise HTTPException(409, "账户状态已改变，请刷新后重新核查")
            revoked_keys = 0
            if payload.action == "freeze":
                if before_status != "frozen":
                    revoked_keys = apply_user_freeze(session, user_id)
                after_status = "frozen"
            else:
                if before_status != "frozen":
                    raise HTTPException(status_code=409, detail="只有冻结账户可以解除冻结")
                if user.email_verified_at is None:
                    raise HTTPException(status_code=409, detail="邮箱尚未验证，禁止解除为活动账户")
                unresolved_refund_risk = session.scalar(select(PaymentRefund.id).where(
                    PaymentRefund.user_id == user_id,
                    PaymentRefund.status == "risk",
                ).limit(1))
                if unresolved_refund_risk is not None:
                    raise HTTPException(status_code=409, detail="退款风险债务尚未处置，禁止解除冻结")
                unresolved_chargeback = session.scalar(select(PaymentChargeback.id).where(
                    PaymentChargeback.user_id == user_id,
                    PaymentChargeback.status.in_(("risk", "pending_reconciliation")),
                ).limit(1))
                if unresolved_chargeback is not None:
                    raise HTTPException(status_code=409, detail="拒付差额或待对账尚未处置，禁止解除冻结")
                from .models import PaymentChargebackReturn
                if session.scalar(select(PaymentChargebackReturn.id).where(
                    PaymentChargebackReturn.user_id == user_id,
                    PaymentChargebackReturn.status == "pending_reconciliation",
                ).limit(1)):
                    raise HTTPException(status_code=409, detail="拒付返还尚待对账，禁止解除冻结")
                user.status = "active"
                after_status = "active"
            # Unfreezing deliberately does not restore sessions or API keys;
            # the owner must authenticate again and explicitly create a key.
            session.add(OperatorAction(
                id=str(uuid.uuid4()), request_id=None,
                target_type="user", target_id=user_id,
                action=f"account_{payload.action}", reason=payload.reason,
                actor=operator.subject, scopes=" ".join(sorted(operator.scopes)),
                token_id=operator.token_id, operation_id=str(uuid.uuid4()),
                source_ip_digest=token_digest(
                    request.client.host if request.client else "unknown",
                    settings.session_pepper,
                ),
                before_status=before_status,
                after_status=after_status,
            ))
            # Status/credential mutation and operator audit are one transaction.
            session.commit()
        app.state.metrics.inc(
            "gateway_account_status_changes_total", labels={"action": payload.action},
        )
        return {
            "user_id": user_id,
            "status": after_status,
            "revoked_api_keys": revoked_keys,
            "credentials_restored": False,
        }

    @app.post("/ops/payments/{order_id}/reconcile")
    async def reconcile_live_payment(
        order_id: str,
        payload: PaymentReconcileRequest,
        request: Request,
        operator: OperatorClaims = Depends(require_operator_scope("payments:write")),
    ) -> dict[str, Any]:
        if app.state.live_payment_bridge is None or not settings.payment_provider:
            raise HTTPException(status_code=404, detail="正式支付未启用")
        with app.state.SessionLocal() as session:
            order = session.scalar(
                select(PaymentOrder)
                .where(PaymentOrder.id == order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                order is None
                or order.payment_amount_minor is None
                or order.payment_currency is None
            ):
                raise HTTPException(status_code=404, detail="支付订单不存在或报价不完整")
            before_status = order.status
            claim_now = utcnow()
            if order.status == "checkout_requesting":
                started_at = order.checkout_claim_started_at
                if (
                    started_at is not None
                    and as_utc(started_at) > utcnow() - timedelta(minutes=5)
                ):
                    session.rollback()
                    raise HTTPException(status_code=409, detail="收银台创建租约仍有效，请稍后再对账")
                # A provider intent may exist. Move the expired local claim to
                # reconciliation, never retry checkout creation.
                order.status = "pending_reconciliation"
                order.risk_reason = "checkout_claim_expired"
                order.checkout_claim_started_at = None
            elif order.status not in {"pending", "pending_reconciliation"}:
                # Completed, failed, or refund-state orders must never be
                # reopened by the payment reconciliation path. In particular,
                # paid -> pending_reconciliation would make double-crediting
                # possible on a later succeeded result.
                raise HTTPException(status_code=409, detail="订单状态不允许进入支付对账")
            reconciliation_claim = order.reconciliation_claim_started_at
            if (
                reconciliation_claim is not None
                and as_utc(reconciliation_claim) > claim_now - timedelta(minutes=5)
            ):
                session.rollback()
                raise HTTPException(status_code=409, detail="支付订单已由其他操作员取得对账租约")
            order.status = "pending_reconciliation"
            order.reconciliation_claim_started_at = claim_now
            order.risk_reason = order.risk_reason or "operator_reconciliation"
            session.add(OperatorAction(
                id=str(uuid.uuid4()), request_id=None,
                target_type="payment_order", target_id=order_id,
                action="payment_reconcile_claim", reason=payload.reason,
                actor=operator.subject, scopes=" ".join(sorted(operator.scopes)),
                token_id=operator.token_id, operation_id=str(uuid.uuid4()),
                source_ip_digest=token_digest(
                    request.client.host if request.client else "unknown",
                    settings.session_pepper,
                ),
                before_status=before_status,
                after_status="pending_reconciliation",
            ))
            # Persist the lease and its audit before the external query.
            session.commit()
            cash_amount = order.payment_amount_minor
            cash_currency = order.payment_currency
            provider_transaction_id = order.provider_transaction_id
        try:
            provider_status = await app.state.live_payment_bridge.reconcile_payment(
                order_id=order_id,
                payment_amount_minor=cash_amount,
                currency=cash_currency,
                provider_transaction_id=provider_transaction_id,
            )
        except PaymentBridgeError as exc:
            with app.state.SessionLocal() as session:
                session.execute(update(PaymentOrder).where(
                    PaymentOrder.id == order_id,
                    PaymentOrder.status.in_(("pending", "pending_reconciliation")),
                ).values(status="pending_reconciliation", risk_reason=f"reconcile:{exc.code}"))
                session.commit()
            app.state.metrics.inc(
                "gateway_payment_reconciliation_total", labels={"result": "bridge_error"},
            )
            raise HTTPException(status_code=503, detail="支付查询失败，订单继续保留人工对账") from exc

        event_types = {
            "pending": "payment.pending",
            "failed": "payment.failed",
            "closed": "payment.closed",
            "paid": "payment.succeeded",
        }
        event_type = event_types.get(provider_status.status)
        if event_type is None:
            with app.state.SessionLocal() as session:
                session.execute(update(PaymentOrder).where(
                    PaymentOrder.id == order_id,
                    PaymentOrder.status.in_(("pending", "pending_reconciliation")),
                ).values(
                    status="pending_reconciliation",
                    risk_reason=f"reconcile_status:{provider_status.status}",
                ))
                session.commit()
            app.state.metrics.inc(
                "gateway_payment_reconciliation_total",
                labels={"result": "manual", "status": provider_status.status},
            )
            raise HTTPException(status_code=409, detail="支付状态仍需人工核对，未变更服务额度")

        normalized = {
            "currency": provider_status.currency,
            "order_id": provider_status.order_id,
            "payment_amount_minor": provider_status.payment_amount_minor,
            "provider_transaction_id": provider_status.provider_transaction_id,
            "status": provider_status.status,
        }
        normalized_body = json.dumps(
            normalized, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(normalized_body).hexdigest()
        event_id = f"reconcile_{digest[:40]}"
        with app.state.SessionLocal() as session:
            action_kwargs = {
                "id": str(uuid.uuid4()), "request_id": None,
                "target_type": "payment_order", "target_id": order_id,
                "action": "payment_reconcile", "reason": payload.reason,
                "actor": operator.subject, "scopes": " ".join(sorted(operator.scopes)),
                "token_id": operator.token_id, "operation_id": str(uuid.uuid4()),
                "source_ip_digest": token_digest(
                    request.client.host if request.client else "unknown",
                    settings.session_pepper,
                ),
                "before_status": before_status, "after_status": provider_status.status,
            }
            # apply_webhook commits its payment/ledger transaction internally;
            # stage the audit first so that commit cannot land without it.
            session.add(OperatorAction(**action_kwargs))
            try:
                duplicate = PaymentDomainService(session).apply_webhook(
                    provider=settings.payment_provider,
                    event_id=event_id,
                    nonce=f"ops:{digest[:48]}",
                    raw_digest=digest,
                    order_id=provider_status.order_id,
                    event_type=event_type,
                    status=provider_status.status,
                    payment_amount_minor=provider_status.payment_amount_minor,
                    payment_currency=provider_status.currency,
                    provider_transaction_id=provider_status.provider_transaction_id,
                )
            except PaymentDomainError as exc:
                session.rollback()
                app.state.metrics.inc(
                    "gateway_payment_reconciliation_total", labels={"result": "domain_error"},
                )
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            if duplicate:
                # Duplicate paths deliberately roll back inside the domain
                # service; persist a fresh audit record for this operator call.
                action_kwargs["id"] = str(uuid.uuid4())
                action_kwargs["operation_id"] = str(uuid.uuid4())
                session.add(OperatorAction(**action_kwargs))
            session.execute(update(PaymentOrder).where(
                PaymentOrder.id == order_id,
            ).values(reconciliation_claim_started_at=None))
            session.commit()
        app.state.metrics.inc(
            "gateway_payment_reconciliation_total",
            labels={"result": "duplicate" if duplicate else "applied", "status": provider_status.status},
        )
        return {
            "order_id": order_id,
            "status": provider_status.status,
            "provider_transaction_id": provider_status.provider_transaction_id,
            "duplicate": duplicate,
        }

    @app.post("/ops/payments/{order_id}/refund")
    async def refund_live_payment(
        order_id: str,
        payload: PaymentRefundRequest,
        request: Request,
        operator: OperatorClaims = Depends(require_operator_scope("payments:write")),
    ) -> dict[str, Any]:
        if app.state.live_payment_bridge is None:
            raise HTTPException(status_code=404, detail="正式支付未启用")
        with app.state.SessionLocal() as session:
            order = session.get(PaymentOrder, order_id)
            if (
                order is None
                or order.payment_amount_minor is None
                or order.payment_currency is None
                or order.provider_transaction_id is None
            ):
                raise HTTPException(status_code=409, detail="订单当前不可退款")
            try:
                prepared_refund, claimed = PaymentDomainService(session).prepare_refund(
                    order_id=order_id,
                    idempotency_key=payload.idempotency_key,
                )
            except PaymentDomainError as exc:
                session.rollback()
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            if not claimed:
                return {
                    "order_id": order_id,
                    "refund_id": prepared_refund.id,
                    "provider_refund_id": prepared_refund.provider_refund_id,
                    "status": prepared_refund.status,
                    "credit_amount_microusd": prepared_refund.credit_amount_microusd,
                }
            cash_amount = prepared_refund.payment_amount_minor
            cash_currency = prepared_refund.payment_currency
            refund_id = prepared_refund.id
            provider_transaction_id = order.provider_transaction_id
        # The refund command is durably reserved before the external side
        # effect. The same idempotency key may be retried only after an
        # ambiguous result has explicitly entered reconciliation.
        try:
            provider_refund = await app.state.live_payment_bridge.refund_payment(
                order_id=order_id,
                payment_amount_minor=cash_amount,
                currency=cash_currency,
                provider_transaction_id=provider_transaction_id,
                idempotency_key=payload.idempotency_key,
            )
        except PaymentBridgeError as exc:
            with app.state.SessionLocal() as session:
                session.execute(update(PaymentOrder).where(
                    PaymentOrder.id == order_id,
                ).values(risk_reason=f"refund:{exc.code}"))
                session.execute(update(PaymentRefund).where(
                    PaymentRefund.id == refund_id,
                    PaymentRefund.status.in_(("requesting", "retrying")),
                ).values(status="pending_reconciliation", risk_reason=exc.code))
                session.commit()
            app.state.metrics.inc(
                "gateway_payment_refunds_total", labels={"status": "pending_reconciliation"},
            )
            raise HTTPException(status_code=503, detail="退款状态不确定，已转人工对账") from exc
        if provider_refund.status != "refunded":
            with app.state.SessionLocal() as session:
                session.execute(update(PaymentOrder).where(
                    PaymentOrder.id == order_id,
                ).values(risk_reason=f"refund_status:{provider_refund.status}"))
                session.execute(update(PaymentRefund).where(
                    PaymentRefund.id == refund_id,
                    PaymentRefund.status.in_(("requesting", "retrying")),
                ).values(
                    status="pending_reconciliation",
                    risk_reason=f"provider_status:{provider_refund.status}",
                ))
                session.commit()
            raise HTTPException(status_code=503, detail="退款尚未确认，未冲正服务额度")
        with app.state.SessionLocal() as session:
            session.add(OperatorAction(
                id=str(uuid.uuid4()),
                request_id=None,
                target_type="payment_refund",
                target_id=refund_id,
                action="payment_refund",
                reason=payload.reason,
                actor=operator.subject,
                scopes=" ".join(sorted(operator.scopes)),
                token_id=operator.token_id,
                operation_id=str(uuid.uuid4()),
                source_ip_digest=token_digest(
                    request.client.host if request.client else "unknown",
                    settings.session_pepper,
                ),
                before_status="refunding",
                after_status="refunded",
            ))
            try:
                refund = PaymentDomainService(session).apply_refund(
                    order_id=order_id,
                    idempotency_key=payload.idempotency_key,
                    provider_refund_id=provider_refund.provider_refund_id,
                )
                # apply_refund commits the normal state transition itself.
                # If an authenticated provider webhook won the race, it
                # returns an already-terminal refund; explicitly commit the
                # operator action added above instead of losing the audit on
                # context-manager rollback.
                session.commit()
            except PaymentDomainError as exc:
                session.rollback()
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        app.state.metrics.inc(
            "gateway_payment_refunds_total", labels={"status": refund.status},
        )
        return {
            "order_id": order_id,
            "refund_id": refund.id,
            "provider_refund_id": refund.provider_refund_id,
            "status": refund.status,
            "credit_amount_microusd": refund.credit_amount_microusd,
        }

    @app.post("/ops/refunds/{refund_id}/risk-disposition")
    def dispose_refund_risk(
        refund_id: str,
        payload: RefundRiskDispositionRequest,
        request: Request,
        operator: OperatorClaims = Depends(require_operator_scope("payments:risk:write")),
    ) -> dict[str, Any]:
        with app.state.SessionLocal() as session:
            try:
                refund, duplicate, outstanding, recovered, written_off = (
                    PaymentDomainService(session).resolve_refund_risk(
                        refund_id=refund_id,
                        action=payload.action,
                        idempotency_key=payload.idempotency_key,
                    )
                )
                session.add(OperatorAction(
                    id=str(uuid.uuid4()), request_id=None,
                    target_type="payment_refund", target_id=refund_id,
                    action=(
                        "refund_risk_recover"
                        if payload.action == "recover_available"
                        else "refund_risk_write_off"
                    ),
                    reason=payload.reason,
                    actor=operator.subject,
                    scopes=" ".join(sorted(operator.scopes)),
                    token_id=operator.token_id,
                    operation_id=str(uuid.uuid4()),
                    source_ip_digest=token_digest(
                        request.client.host if request.client else "unknown",
                        settings.session_pepper,
                    ),
                    before_status="resolved" if duplicate else "risk",
                    after_status="resolved",
                ))
                # Risk state, wallet recovery/write-off ledger and operator
                # audit become visible in exactly one commit.
                session.commit()
            except PaymentDomainError as exc:
                session.rollback()
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
            except IntegrityError as exc:
                session.rollback()
                raise HTTPException(status_code=409, detail="退款风险处置并发冲突") from exc
        app.state.metrics.inc(
            "gateway_refund_risk_dispositions_total",
            labels={"action": payload.action, "duplicate": str(duplicate).lower()},
        )
        return {
            "refund_id": refund_id,
            "status": refund.status,
            "action": payload.action,
            "outstanding_microusd": outstanding,
            "recovered_microusd": recovered,
            "written_off_microusd": written_off,
            "duplicate": duplicate,
            "account_unfrozen": False,
        }

    @app.post("/ops/reconciliation/{request_id}")
    def reconcile_request(
        request_id: str,
        payload: ReconciliationRequest,
        request: Request,
        operator: OperatorClaims = Depends(require_operator_scope("reconciliation:write")),
    ) -> dict[str, Any]:
        with app.state.SessionLocal() as session:
            request_record = session.get(ModelRequest, request_id)
            if request_record is None or request_record.status != "pending_reconciliation":
                raise HTTPException(status_code=404, detail="待对账请求不存在")
            session.add(OperatorAction(
                id=str(uuid.uuid4()),
                request_id=request_id,
                target_type="model_request",
                target_id=request_id,
                action=payload.action,
                reason=payload.reason,
                actor=operator.subject,
                scopes=" ".join(sorted(operator.scopes)),
                token_id=operator.token_id,
                operation_id=str(uuid.uuid4()),
                source_ip_digest=token_digest(
                    request.client.host if request.client else "unknown",
                    settings.session_pepper,
                ),
                before_status=request_record.status,
                after_status="reconciled_released" if payload.action == "release" else "settled",
            ))
            try:
                if payload.action == "release":
                    release_model_request(
                        session,
                        request_id,
                        f"operator_release:{payload.reason[:48]}",
                        allowed_statuses=("pending_reconciliation",),
                        final_status="reconciled_released",
                        ledger_kind="operator_release",
                        attempt_id=request_record.final_attempt_id,
                    )
                else:
                    settlement_response = {
                        "model": request_record.requested_model,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": payload.input_tokens,
                            "completion_tokens": payload.output_tokens,
                            "total_tokens": (payload.input_tokens or 0) + (payload.output_tokens or 0),
                        },
                    }
                    settle_model_request(
                        session,
                        request_id=request_id,
                        response=settlement_response,
                        provider=request_record.final_provider or "operator-reconciled",
                        fallback_count=request_record.fallback_count,
                        upstream_cost_override=payload.upstream_cost_microusd,
                        allowed_statuses=("pending_reconciliation",),
                        attempt_id=request_record.final_attempt_id,
                        allow_budget_overrun=True,
                    )
            except BillingError as exc:
                session.rollback()
                raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        with app.state.SessionLocal() as session:
            updated = session.get(ModelRequest, request_id)
            return {
                "request_id": request_id,
                "status": updated.status if updated else "unknown",
                "charged": updated.charged_microusd if updated else 0,
                "upstream_cost": updated.upstream_cost_microusd if updated else 0,
            }

    # This is intentionally the outermost middleware. Uvicorn's generic proxy
    # parsing is disabled in the production image; only this explicitly
    # configured private header may replace the direct TCP peer.
    app.add_middleware(
        TrustedProxyClientIPMiddleware,
        trusted_proxy_cidrs=settings.trusted_proxy_cidrs,
        trusted_proxy_secret=settings.trusted_proxy_secret,
    )
    if settings.ingress_provider == "vercel":
        app.add_middleware(
            VercelIngressMiddleware,
            proxy_secret=settings.trusted_proxy_secret,
            ops_ingress_secret=settings.ops_ingress_secret,
        )
    return app
