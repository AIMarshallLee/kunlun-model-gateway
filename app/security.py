"""Password and opaque-token primitives with no plaintext persistence."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import re
import secrets


PASSWORD_ITERATIONS = 600_000
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DUMMY_PASSWORD_HASH = (
    "pbkdf2_sha256$600000$a3VubHVuLWR1bW15LXNhbHQ$"
    "fEfEU83qelz5YQcAAtmiZzWTBOpMqEvCEVk21S48COM"
)


def normalize_email(email: str) -> str:
    normalized = email.strip().casefold()
    if len(normalized) > 254 or not EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("邮箱格式不正确")
    return normalized


def hash_password(password: str) -> str:
    if len(password) < 12 or len(password) > 256:
        raise ValueError("密码长度必须为 12 到 256 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations_text))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_digest(secret: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), secret.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class ParsedApiKey:
    key_id: str
    secret: str


def issue_api_key() -> tuple[str, ParsedApiKey]:
    key_id = secrets.token_urlsafe(9).replace("-", "").replace("_", "")
    secret = secrets.token_urlsafe(32)
    raw = f"gw_{key_id}.{secret}"
    return raw, ParsedApiKey(key_id=key_id, secret=secret)


def parse_api_key(raw: str) -> ParsedApiKey | None:
    if not 20 <= len(raw) <= 256 or not raw.startswith("gw_") or "." not in raw:
        return None
    key_id, secret = raw[3:].split(".", 1)
    if not 4 <= len(key_id) <= 32 or not 32 <= len(secret) <= 128:
        return None
    return ParsedApiKey(key_id=key_id, secret=secret)


def issue_session_token() -> str:
    return "sess_" + secrets.token_urlsafe(32)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
