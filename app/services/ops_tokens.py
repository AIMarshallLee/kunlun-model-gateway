"""Short-lived scoped tokens for private operator endpoints."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import secrets
import time


ALLOWED_SCOPES = {
    "models:read",
    "models:write",
    "console:read",
    "audit:read",
    "channels:read",
    "channels:write",
    "reconciliation:read",
    "reconciliation:write",
    "accounts:read",
    "accounts:write",
    "accounts:invite",
    "payments:read",
    "payments:write",
    "payments:risk:write",
    "metrics:read",
}


class OpsTokenError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OperatorClaims:
    subject: str
    scopes: frozenset[str]
    issued_at: int
    expires_at: int
    token_id: str


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise OpsTokenError("运维凭证编码无效") from exc


def _validate_secret(secret: str) -> None:
    if len(secret.encode("utf-8")) < 32:
        raise OpsTokenError("运维签名密钥至少需要 32 字节")


def mint_operator_token(
    secret: str,
    *,
    subject: str,
    scopes: set[str],
    ttl_seconds: int = 300,
    now: int | None = None,
) -> str:
    _validate_secret(secret)
    if not subject or len(subject) > 200:
        raise OpsTokenError("运维主体无效")
    if not scopes or not scopes <= ALLOWED_SCOPES:
        raise OpsTokenError("运维权限范围无效")
    if ttl_seconds < 30 or ttl_seconds > 900:
        raise OpsTokenError("运维凭证有效期必须位于 30 到 900 秒")
    issued_at = int(time.time()) if now is None else int(now)
    payload = json.dumps(
        {
            "aud": "kunlun-gateway-ops",
            "exp": issued_at + ttl_seconds,
            "iat": issued_at,
            "jti": secrets.token_urlsafe(18),
            "scp": sorted(scopes),
            "sub": subject,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = _b64encode(payload)
    signing_input = f"ops1.{encoded}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"ops1.{encoded}.{_b64encode(signature)}"


def verify_operator_token(
    token: str,
    secret: str,
    *,
    required_scope: str,
    now: int | None = None,
) -> OperatorClaims:
    _validate_secret(secret)
    if not isinstance(token, str) or not 1 <= len(token) <= 4096:
        raise OpsTokenError("运维凭证格式无效")
    if required_scope not in ALLOWED_SCOPES:
        raise OpsTokenError("请求的运维权限无效")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "ops1":
        raise OpsTokenError("运维凭证格式无效")
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii", errors="strict")
    expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    supplied = _b64decode(parts[2])
    if not hmac.compare_digest(expected, supplied):
        raise OpsTokenError("运维凭证签名无效")
    try:
        data = json.loads(_b64decode(parts[1]))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OpsTokenError("运维凭证载荷无效") from exc
    if not isinstance(data, dict) or data.get("aud") != "kunlun-gateway-ops":
        raise OpsTokenError("运维凭证受众无效")
    subject = data.get("sub")
    scopes = data.get("scp")
    issued_at = data.get("iat")
    expires_at = data.get("exp")
    token_id = data.get("jti")
    if (
        not isinstance(subject, str)
        or not subject
        or not isinstance(scopes, list)
        or any(not isinstance(scope, str) for scope in scopes)
        or not isinstance(issued_at, int)
        or not isinstance(expires_at, int)
        or not isinstance(token_id, str)
        or not token_id
    ):
        raise OpsTokenError("运维凭证声明无效")
    scope_set = frozenset(scopes)
    if not scope_set <= ALLOWED_SCOPES or required_scope not in scope_set:
        raise OpsTokenError("运维凭证权限不足")
    current = int(time.time()) if now is None else int(now)
    if issued_at > current + 30 or expires_at <= issued_at or expires_at - issued_at > 900:
        raise OpsTokenError("运维凭证时间声明无效")
    if current >= expires_at:
        raise OpsTokenError("运维凭证已过期")
    return OperatorClaims(
        subject=subject,
        scopes=scope_set,
        issued_at=issued_at,
        expires_at=expires_at,
        token_id=token_id,
    )
