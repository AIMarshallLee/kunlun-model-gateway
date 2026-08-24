"""Production identity primitives: opaque one-time tokens, verification gates and freezes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage as MimeEmailMessage
import smtplib
import ssl
import secrets
from typing import Protocol
from urllib.parse import quote, unquote, urlparse
import uuid

from sqlalchemy import func, select, update

from ..models import AccessSession, ApiKey, EmailVerificationToken, PasswordResetToken, User
from ..security import as_utc, hash_password, token_digest, utcnow


class IdentityError(RuntimeError):
    """Safe, non-sensitive identity operation failure."""


@dataclass(frozen=True, slots=True)
class EmailMessage:
    kind: str
    recipient: str
    token: str


class EmailSender(Protocol):
    def send_verification(self, recipient: str, token: str) -> None: ...
    def send_password_reset(self, recipient: str, token: str) -> None: ...


class InMemoryEmailSender:
    """Test adapter; never use as a production transport."""

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def send_verification(self, recipient: str, token: str) -> None:
        self.messages.append(EmailMessage("email_verification", recipient, token))

    def send_password_reset(self, recipient: str, token: str) -> None:
        self.messages.append(EmailMessage("password_reset", recipient, token))


class DisabledEmailSender:
    """Fail-closed transport for private control planes without public email."""

    def send_verification(self, _recipient: str, _token: str) -> None:
        raise IdentityError("邮件发送未启用")

    def send_password_reset(self, _recipient: str, _token: str) -> None:
        raise IdentityError("邮件发送未启用")


class SmtpEmailSender:
    """Small synchronous SMTP transport used by the identity workflow.

    Delivery is intentionally part of the request in this first production
    candidate so a registration never claims that a message was queued when it
    was not accepted by SMTP. Operators must still verify SPF/DKIM/DMARC and
    real inbox delivery before enabling public signup.
    """

    def __init__(
        self,
        smtp_url: str,
        *,
        from_address: str,
        public_base_url: str,
        smtp_factory=None,
        smtp_ssl_factory=None,
    ) -> None:
        parsed = urlparse(smtp_url)
        if (
            parsed.scheme not in {"smtp", "smtps"}
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise IdentityError("生产邮件发送器配置无效")
        base = urlparse(public_base_url)
        if base.scheme != "https" or not base.hostname or base.username or base.password:
            raise IdentityError("公开站点地址必须使用 HTTPS")
        if not from_address or "\n" in from_address or "\r" in from_address or "@" not in from_address:
            raise IdentityError("发件人地址配置无效")
        self.smtp_url = smtp_url
        self.host = parsed.hostname
        self.port = parsed.port or (465 if parsed.scheme == "smtps" else 587)
        self.username = unquote(parsed.username) if parsed.username else None
        self.password = unquote(parsed.password) if parsed.password else None
        self.use_ssl = parsed.scheme == "smtps"
        self.from_address = from_address
        self.public_base_url = public_base_url.rstrip("/")
        self._smtp_factory = smtp_factory or smtplib.SMTP
        self._smtp_ssl_factory = smtp_ssl_factory or smtplib.SMTP_SSL

    def send_verification(self, recipient: str, token: str) -> None:
        self._send("email_verification", recipient, token)

    def send_password_reset(self, recipient: str, token: str) -> None:
        self._send("password_reset", recipient, token)

    def _send(self, kind: str, recipient: str, token: str) -> None:
        if not recipient or "\n" in recipient or "\r" in recipient:
            raise IdentityError("收件人地址无效")
        if kind == "email_verification":
            subject = "验证你的昆仑网关邮箱"
            path = "/verify-email"
            intro = "请使用下面的一次性链接完成邮箱验证："
        elif kind == "password_reset":
            subject = "重置你的昆仑网关密码"
            path = "/reset-password"
            intro = "请使用下面的一次性链接重置密码："
        else:
            raise IdentityError("邮件类型无效")
        # URL fragments are processed only by the browser and are not sent in
        # HTTP request targets, reverse-proxy access logs or Referer headers.
        link = f"{self.public_base_url}{path}#token={quote(token, safe='')}"
        message = MimeEmailMessage()
        message["From"] = self.from_address
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(f"{intro}\n\n{link}\n\n如果不是你本人操作，请忽略本邮件。")
        try:
            if self.use_ssl:
                client = self._smtp_ssl_factory(
                    self.host,
                    self.port,
                    timeout=10,
                    context=ssl.create_default_context(),
                )
            else:
                client = self._smtp_factory(self.host, self.port, timeout=10)
            with client:
                if not self.use_ssl:
                    client.ehlo()
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                if self.username:
                    client.login(self.username, self.password or "")
                client.send_message(message)
        except Exception as exc:
            # SMTP libraries frequently include server responses and account
            # names in exception strings; never pass them to callers or logs.
            raise IdentityError("邮件发送失败") from exc


def build_email_sender(
    *,
    environment: str,
    smtp_url: str,
    from_address: str = "",
    public_base_url: str = "",
) -> EmailSender:
    if environment.casefold() in {"test", "testing", "development"}:
        return InMemoryEmailSender()
    if not smtp_url:
        raise IdentityError("生产环境必须配置邮件发送器")
    return SmtpEmailSender(
        smtp_url,
        from_address=from_address,
        public_base_url=public_base_url,
    )


def _opaque_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def issue_email_verification(session, user_id: str, pepper: str, sender: EmailSender, *, ttl: timedelta = timedelta(hours=24)) -> str:
    user = session.get(User, user_id)
    if user is None:
        raise IdentityError("账户不存在")
    raw = _opaque_token("verify_")
    session.execute(update(EmailVerificationToken).where(
        EmailVerificationToken.user_id == user_id,
        EmailVerificationToken.consumed_at.is_(None),
    ).values(consumed_at=utcnow()))
    session.add(EmailVerificationToken(
        id=str(uuid.uuid4()), user_id=user_id,
        token_digest=token_digest(raw, pepper),
        expires_at=utcnow() + ttl,
    ))
    session.commit()
    try:
        sender.send_verification(user.email, raw)
    except Exception as exc:
        session.execute(update(EmailVerificationToken).where(EmailVerificationToken.token_digest == token_digest(raw, pepper)).values(consumed_at=utcnow()))
        session.commit()
        raise IdentityError("邮件发送失败") from exc
    return raw


def consume_email_verification(session, raw_token: str, pepper: str, *, now: datetime | None = None) -> bool:
    now = now or utcnow()
    record = session.scalar(select(EmailVerificationToken).where(
        EmailVerificationToken.token_digest == token_digest(raw_token, pepper),
        EmailVerificationToken.consumed_at.is_(None),
    ))
    if record is None or as_utc(record.expires_at) <= as_utc(now):
        return False
    user = session.scalar(select(User).where(User.id == record.user_id).with_for_update())
    if user is None or user.status != "pending_email":
        session.rollback()
        return False
    changed = session.execute(update(EmailVerificationToken).where(
        EmailVerificationToken.id == record.id,
        EmailVerificationToken.consumed_at.is_(None),
    ).values(consumed_at=now)).rowcount
    if changed != 1:
        session.rollback()
        return False
    user.status = "active"
    user.email_verified_at = now
    session.commit()
    session.expire_all()
    return True


def request_password_reset(session, email: str, pepper: str, sender: EmailSender, *, ttl: timedelta = timedelta(hours=1)) -> None:
    """Always returns None, whether or not the account exists."""
    user = session.scalar(select(User).where(User.email == email).with_for_update())
    if user is None or user.status != "active":
        session.rollback()
        return None
    raw = _opaque_token("reset_")
    session.execute(update(PasswordResetToken).where(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.consumed_at.is_(None),
    ).values(consumed_at=utcnow()))
    session.add(PasswordResetToken(
        id=str(uuid.uuid4()), user_id=user.id,
        token_digest=token_digest(raw, pepper), expires_at=utcnow() + ttl,
    ))
    session.commit()
    try:
        sender.send_password_reset(user.email, raw)
    except Exception:
        session.execute(update(PasswordResetToken).where(PasswordResetToken.token_digest == token_digest(raw, pepper)).values(consumed_at=utcnow()))
        session.commit()
        # Password-reset responses must be indistinguishable for existing and
        # missing accounts. Delivery failures are surfaced through metrics and
        # operator alerting, never through this public result.
        return None
    return None


def reset_password(session, raw_token: str, new_password: str, pepper: str, _session_pepper: str, *, now: datetime | None = None) -> bool:
    now = now or utcnow()
    record = session.scalar(select(PasswordResetToken).where(
        PasswordResetToken.token_digest == token_digest(raw_token, pepper),
        PasswordResetToken.consumed_at.is_(None),
    ))
    if record is None or as_utc(record.expires_at) <= as_utc(now):
        session.rollback()
        return False
    user = session.scalar(
        select(User)
        .where(User.id == record.user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None or user.status != "active":
        session.execute(update(PasswordResetToken).where(
            PasswordResetToken.id == record.id,
            PasswordResetToken.consumed_at.is_(None),
        ).values(consumed_at=now))
        session.commit()
        return False
    try:
        password_hash = hash_password(new_password)
    except ValueError as exc:
        raise IdentityError(str(exc)) from exc
    record = session.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.id == record.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        record is None
        or record.consumed_at is not None
        or as_utc(record.expires_at) <= as_utc(now)
    ):
        session.rollback()
        return False
    changed = session.execute(update(PasswordResetToken).where(
        PasswordResetToken.id == record.id,
        PasswordResetToken.consumed_at.is_(None),
    ).values(consumed_at=now)).rowcount
    if changed != 1:
        session.rollback()
        return False
    user.password_hash = password_hash
    # Password reset is a global credential rotation: every old session dies.
    session.execute(update(AccessSession).where(AccessSession.user_id == record.user_id).values(revoked=True))
    session.execute(update(ApiKey).where(
        ApiKey.user_id == record.user_id,
        ApiKey.status == "active",
    ).values(status="revoked", revoked_at=now))
    session.commit()
    session.expire_all()
    return True


def enforce_verified_user(session, user_id: str) -> User:
    user = session.get(User, user_id)
    if user is None or user.status != "active" or user.email_verified_at is None:
        raise IdentityError("邮箱尚未验证")
    return user


def enforce_key_limit(session, user_id: str, max_active_keys: int) -> None:
    if max_active_keys < 1:
        raise IdentityError("API Key 上限配置无效")
    # PostgreSQL turns this user row into the serialization point for key
    # creation, preventing concurrent requests from exceeding the cap.
    user = session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None or user.status != "active" or user.email_verified_at is None:
        raise IdentityError("邮箱尚未验证")
    count = session.scalar(select(func.count()).select_from(ApiKey).where(
        ApiKey.user_id == user_id,
        ApiKey.status == "active",
    )) or 0
    if count >= max_active_keys:
        raise IdentityError("API Key 数量已达到上限")


def apply_user_freeze(session, user_id: str, *, now: datetime | None = None) -> int:
    """Freeze an account and revoke every credential without committing.

    Callers that also mutate money or audit records can keep the freeze in the
    same transaction. All issuance/reset flows use the same User row lock, so
    no fresh recovery token can be created across the freeze boundary.
    """
    user = session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        return 0
    user.status = "frozen"
    frozen_at = now or utcnow()
    key_count = session.execute(update(ApiKey).where(
        ApiKey.user_id == user_id,
        ApiKey.status == "active",
    ).values(status="revoked", revoked_at=frozen_at)).rowcount
    session.execute(update(AccessSession).where(AccessSession.user_id == user_id, AccessSession.revoked.is_(False)).values(revoked=True))
    session.execute(update(EmailVerificationToken).where(
        EmailVerificationToken.user_id == user_id,
        EmailVerificationToken.consumed_at.is_(None),
    ).values(consumed_at=frozen_at))
    session.execute(update(PasswordResetToken).where(
        PasswordResetToken.user_id == user_id,
        PasswordResetToken.consumed_at.is_(None),
    ).values(consumed_at=frozen_at))
    return int(key_count or 0)


def freeze_user(session, user_id: str, _reason: str = "") -> int:
    key_count = apply_user_freeze(session, user_id)
    session.commit()
    return key_count
