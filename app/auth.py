"""Session and API-key authentication dependencies."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import time
import uuid

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .models import AccessSession, ApiKey, AuthRateLimitCounter, RateLimitCounter, User
from .security import as_utc, parse_api_key, token_digest, utcnow
from .services.ops_tokens import OperatorClaims, OpsTokenError, verify_operator_token


bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    api_key_id: str | None = None


def _credentials(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(status_code=401, detail="缺少有效凭证")
    if not 1 <= len(credentials.credentials) <= 512:
        raise HTTPException(status_code=401, detail="凭证格式无效")
    return credentials.credentials


def require_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    raw = _credentials(credentials)
    if not raw.startswith("sess_"):
        raise HTTPException(status_code=401, detail="会话凭证无效")
    settings = request.app.state.settings
    with request.app.state.SessionLocal() as session:
        record = session.scalar(select(AccessSession).where(
            AccessSession.token_digest == token_digest(raw, settings.session_pepper),
            AccessSession.revoked.is_(False),
        ))
        if record is None or as_utc(record.expires_at) <= utcnow():
            raise HTTPException(status_code=401, detail="会话凭证无效或已过期")
        user = session.get(User, record.user_id)
        if user is None or user.status != "active" or (settings.gateway_mode == "managed_gateway" and user.email_verified_at is None):
            raise HTTPException(status_code=401, detail="账户不可用")
        return Principal(user_id=user.id)


def _enforce_rate_limit(request: Request, api_key_id: str) -> None:
    limit = request.app.state.settings.rate_limit_per_minute
    window = int(time.time() // 60)
    with request.app.state.SessionLocal() as session:
        current = session.scalar(select(RateLimitCounter).where(
            RateLimitCounter.api_key_id == api_key_id,
            RateLimitCounter.window_epoch == window,
        ))
        if current is None:
            try:
                session.add(RateLimitCounter(
                    id=str(uuid.uuid4()),
                    api_key_id=api_key_id,
                    window_epoch=window,
                    count=1,
                ))
                session.commit()
                return
            except IntegrityError:
                session.rollback()
        result = session.execute(update(RateLimitCounter).where(
            RateLimitCounter.api_key_id == api_key_id,
            RateLimitCounter.window_epoch == window,
            RateLimitCounter.count < limit,
        ).values(count=RateLimitCounter.count + 1))
        if result.rowcount != 1:
            session.rollback()
            raise HTTPException(status_code=429, detail="请求过于频繁", headers={"Retry-After": "60"})
        session.commit()


def enforce_auth_rate_limit(
    request: Request, action: str, subject: str, limit_override: int | None = None,
) -> None:
    """Limit both IP and account subjects without persisting either in plaintext."""
    limit = (
        request.app.state.settings.rate_limit_per_minute
        if limit_override is None else int(limit_override)
    )
    if limit < 1:
        raise RuntimeError("rate limit must be positive")
    host = request.client.host if request.client else "unknown"
    window = int(time.time() // 60)
    for raw_subject in (f"ip:{host}", f"account:{subject.casefold()}"):
        digest = token_digest(raw_subject, request.app.state.settings.session_pepper)
        with request.app.state.SessionLocal() as session:
            current = session.scalar(select(AuthRateLimitCounter).where(
                AuthRateLimitCounter.subject_digest == digest,
                AuthRateLimitCounter.action == action,
                AuthRateLimitCounter.window_epoch == window,
            ))
            if current is None:
                try:
                    session.add(AuthRateLimitCounter(
                        id=str(uuid.uuid4()),
                        subject_digest=digest,
                        action=action,
                        window_epoch=window,
                        count=1,
                    ))
                    session.commit()
                    continue
                except IntegrityError:
                    session.rollback()
            result = session.execute(update(AuthRateLimitCounter).where(
                AuthRateLimitCounter.subject_digest == digest,
                AuthRateLimitCounter.action == action,
                AuthRateLimitCounter.window_epoch == window,
                AuthRateLimitCounter.count < limit,
            ).values(count=AuthRateLimitCounter.count + 1))
            if result.rowcount != 1:
                session.rollback()
                raise HTTPException(status_code=429, detail="认证请求过于频繁", headers={"Retry-After": "60"})
            session.commit()


def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    raw = _credentials(credentials)
    parsed = parse_api_key(raw)
    if parsed is None:
        raise HTTPException(status_code=401, detail="API Key 无效")
    settings = request.app.state.settings
    with request.app.state.SessionLocal() as session:
        record = session.get(ApiKey, parsed.key_id)
        actual_digest = token_digest(parsed.secret, settings.api_key_pepper)
        if record is None:
            hmac.compare_digest(actual_digest, "0" * 64)
            raise HTTPException(status_code=401, detail="API Key 无效")
        if record.status != "active" or not hmac.compare_digest(actual_digest, record.secret_digest):
            raise HTTPException(status_code=401, detail="API Key 无效")
        user = session.get(User, record.user_id)
        if user is None or user.status != "active" or (settings.gateway_mode == "managed_gateway" and user.email_verified_at is None):
            raise HTTPException(status_code=401, detail="账户不可用")
        session.execute(update(ApiKey).where(ApiKey.id == record.id).values(last_used_at=utcnow()))
        session.commit()
        principal = Principal(user_id=user.id, api_key_id=record.id)
    _enforce_rate_limit(request, parsed.key_id)
    return principal


def require_operator(
    request: Request,
    x_kunlun_ops_token: str = Header(default="", alias="X-Kunlun-Ops-Token"),
) -> OperatorClaims:
    return _operator_claims(request, x_kunlun_ops_token, "reconciliation:read")


def _operator_claims(request: Request, supplied: str, required_scope: str) -> OperatorClaims:
    if len(supplied) > 4096:
        raise HTTPException(status_code=401, detail="运维凭证无效或权限不足")
    settings = request.app.state.settings
    signing_secret = settings.operator_signing_secret
    if len(signing_secret) >= 32:
        try:
            return verify_operator_token(
                supplied,
                signing_secret,
                required_scope=required_scope,
            )
        except OpsTokenError as exc:
            raise HTTPException(status_code=401, detail="运维凭证无效或权限不足") from exc
    # The shared long-lived token remains available only for local/test
    # compatibility. Production validation never accepts it as the ops plane.
    expected = request.app.state.settings.operator_token
    if settings.environment not in {"development", "test"} or len(expected) < 32:
        raise HTTPException(status_code=404, detail="运维接口未启用")
    if required_scope not in {"reconciliation:read", "reconciliation:write"}:
        # The legacy shared token is intentionally not a general-purpose ops
        # credential.  Keep compatibility only for the original reconciliation
        # endpoints and conceal unrelated operator resources.
        raise HTTPException(status_code=404, detail="运维接口未启用")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="运维凭证无效")
    now = int(time.time())
    return OperatorClaims(
        subject="legacy-local-operator",
        scopes=frozenset({"reconciliation:read", "reconciliation:write"}),
        issued_at=now,
        expires_at=now + 1,
        token_id="legacy-local",
    )


def require_operator_scope(required_scope: str):
    def dependency(
        request: Request,
        x_kunlun_ops_token: str = Header(default="", alias="X-Kunlun-Ops-Token"),
    ) -> OperatorClaims:
        return _operator_claims(request, x_kunlun_ops_token, required_scope)

    return dependency
