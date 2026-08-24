from datetime import timedelta
from urllib.parse import quote

import pytest
from sqlalchemy import select

from app.db import Base, build_engine, build_session_factory
from app.models import AccessSession, ApiKey, EmailVerificationToken, PasswordResetToken, User
from app.security import hash_password, issue_api_key, issue_session_token, token_digest, utcnow
from app.services.identity import (
    IdentityError,
    InMemoryEmailSender,
    build_email_sender,
    consume_email_verification,
    enforce_key_limit,
    freeze_user,
    issue_email_verification,
    request_password_reset,
    reset_password,
    SmtpEmailSender,
)


@pytest.fixture
def db(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'identity.sqlite3'}")
    Base.metadata.create_all(engine)
    return build_session_factory(engine)


def _user(session, *, status="pending_email"):
    user = User(id="u1", email="u@example.com", password_hash=hash_password("correct horse battery staple"), status=status)
    session.add(user)
    session.commit()
    return user


def test_email_verification_stores_only_digest_and_is_one_time(db):
    sender = InMemoryEmailSender()
    with db() as session:
        user = _user(session)
        raw = issue_email_verification(session, user.id, "pepper", sender, ttl=timedelta(minutes=30))
        token = session.scalar(select(EmailVerificationToken))
        assert token.token_digest == token_digest(raw, "pepper")
        assert raw not in token.token_digest
        assert sender.messages[0].kind == "email_verification"
        assert consume_email_verification(session, raw, "pepper") is True
        assert session.get(User, user.id).email_verified_at is not None
        assert session.get(User, user.id).status == "active"
        assert consume_email_verification(session, raw, "pepper") is False


def test_expired_verification_is_rejected(db):
    sender = InMemoryEmailSender()
    with db() as session:
        user = _user(session)
        raw = issue_email_verification(session, user.id, "pepper", sender, ttl=timedelta(seconds=-1))
        assert consume_email_verification(session, raw, "pepper") is False


def test_freeze_consumes_old_email_verification_and_cannot_reactivate_user(db):
    sender = InMemoryEmailSender()
    with db() as session:
        user = _user(session)
        raw = issue_email_verification(session, user.id, "pepper", sender)
        freeze_user(session, user.id, "risk review")
        assert session.get(User, user.id).status == "frozen"
        assert consume_email_verification(session, raw, "pepper") is False
        session.expire_all()
        assert session.get(User, user.id).status == "frozen"
        token = session.scalar(select(EmailVerificationToken))
        assert token is not None and token.consumed_at is not None


def test_freeze_consumes_old_password_reset_and_cannot_change_password(db):
    sender = InMemoryEmailSender()
    with db() as session:
        user = _user(session, status="active")
        old_password_hash = user.password_hash
        request_password_reset(session, user.email, "pepper", sender)
        raw = sender.messages[-1].token
        freeze_user(session, user.id, "account takeover review")
        assert reset_password(
            session,
            raw,
            "attacker selected replacement password",
            "pepper",
            "session",
        ) is False
        session.expire_all()
        frozen = session.get(User, user.id)
        assert frozen.status == "frozen"
        assert frozen.password_hash == old_password_hash
        token = session.scalar(select(PasswordResetToken))
        assert token is not None and token.consumed_at is not None


def test_verification_delivery_failure_consumes_token_and_hides_transport_error(db):
    class FailingSender:
        def send_verification(self, _recipient, _token):
            raise RuntimeError("smtp account=private@example.test password=top-secret")

        def send_password_reset(self, _recipient, _token):
            raise AssertionError("not used")

    with db() as session:
        user = _user(session)
        with pytest.raises(IdentityError) as exc_info:
            issue_email_verification(session, user.id, "pepper", FailingSender())
        assert str(exc_info.value) == "邮件发送失败"
        token = session.scalar(select(EmailVerificationToken))
        assert token is not None and token.consumed_at is not None


def test_password_reset_is_generic_and_revokes_sessions(db):
    sender = InMemoryEmailSender()
    with db() as session:
        user = _user(session, status="active")
        raw_session = issue_session_token()
        session.add(AccessSession(id="s1", user_id=user.id, token_digest=token_digest(raw_session, "session"), expires_at=utcnow() + timedelta(hours=1)))
        _raw_key, parsed_key = issue_api_key()
        session.add(ApiKey(id=parsed_key.key_id, user_id=user.id, name="before-reset", secret_digest=token_digest(parsed_key.secret, "key-pepper"), last_four=parsed_key.secret[-4:]))
        session.commit()
        assert request_password_reset(session, "missing@example.com", "pepper", sender) is None
        assert request_password_reset(session, user.email, "pepper", sender) is None
        token = session.scalar(select(PasswordResetToken))
        assert token is not None and sender.messages[-1].kind == "password_reset"
        assert reset_password(session, sender.messages[-1].token, "a new sufficiently long password", "pepper", "session") is True
        assert session.get(AccessSession, "s1").revoked is True
        assert session.get(ApiKey, parsed_key.key_id).status == "revoked"
        assert reset_password(session, sender.messages[-1].token, "another sufficiently long password", "pepper", "session") is False


def test_password_reset_delivery_failure_is_generic_and_does_not_leave_live_token(db):
    class FailingSender:
        def send_verification(self, _recipient, _token):
            raise AssertionError("not used")

        def send_password_reset(self, _recipient, _token):
            raise RuntimeError("smtp secret reset@example.test")

    with db() as session:
        user = _user(session, status="active")
        assert request_password_reset(session, user.email, "pepper", FailingSender()) is None
        token = session.scalar(select(PasswordResetToken))
        assert token is not None and token.consumed_at is not None
        assert reset_password(session, "reset_does_not_exist", "a new sufficiently long password", "pepper", "session") is False


def test_unverified_user_and_key_limit_and_freeze(db):
    with db() as session:
        user = _user(session)
        with pytest.raises(IdentityError):
            enforce_key_limit(session, user.id, 2)
        user.email_verified_at = utcnow()
        user.status = "active"
        for index in range(2):
            _, parsed = issue_api_key()
            session.add(ApiKey(id=parsed.key_id, user_id=user.id, name=str(index), secret_digest="x", last_four="xxxx"))
        session.commit()
        with pytest.raises(IdentityError):
            enforce_key_limit(session, user.id, 2)
        freeze_user(session, user.id, "fraud review")
        assert session.get(User, user.id).status == "frozen"
        assert all(key.status == "revoked" for key in session.scalars(select(ApiKey).where(ApiKey.user_id == user.id)).all())


def test_production_email_sender_fails_closed_and_test_sender_is_memory():
    with pytest.raises(IdentityError):
        build_email_sender(environment="production", smtp_url="")
    assert isinstance(build_email_sender(environment="test", smtp_url=""), InMemoryEmailSender)


class _FakeSmtp:
    def __init__(self, host, port, **kwargs):
        self.host = host
        self.port = port
        self.kwargs = kwargs
        self.calls = []
        self.message = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def ehlo(self):
        self.calls.append(("ehlo",))

    def starttls(self, *, context):
        self.calls.append(("starttls", context))

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, message):
        self.message = message
        self.calls.append(("send_message",))


def test_smtps_sender_uses_ssl_and_builds_verification_link():
    created = []

    def factory(host, port, **kwargs):
        client = _FakeSmtp(host, port, **kwargs)
        created.append(client)
        return client

    sender = SmtpEmailSender(
        "smtps://mailer:p%40ss@smtp.example.test:465",
        from_address="noreply@example.test",
        public_base_url="https://console.example.test/",
        smtp_ssl_factory=factory,
        smtp_factory=lambda *_args, **_kwargs: pytest.fail("STARTTLS factory must not be used"),
    )
    sender.send_verification("person@example.test", "verify_abc+/=")

    assert len(created) == 1
    client = created[0]
    assert (client.host, client.port) == ("smtp.example.test", 465)
    assert client.kwargs["timeout"] == 10
    assert any(call[:1] == ("login",) and call[1:] == ("mailer", "p@ss") for call in client.calls)
    assert ("ehlo",) not in client.calls
    content = client.message.get_content()
    assert "https://console.example.test/verify-email#token=" + quote("verify_abc+/=", safe="") in content
    assert "/verify-email?token=" not in content
    assert client.message["To"] == "person@example.test"


def test_smtp_sender_uses_starttls_and_builds_reset_link():
    created = []

    def factory(host, port, **kwargs):
        client = _FakeSmtp(host, port, **kwargs)
        created.append(client)
        return client

    sender = SmtpEmailSender(
        "smtp://smtp.example.test:587",
        from_address="noreply@example.test",
        public_base_url="https://console.example.test",
        smtp_factory=factory,
        smtp_ssl_factory=lambda *_args, **_kwargs: pytest.fail("SSL factory must not be used"),
    )
    sender.send_password_reset("person@example.test", "reset_token")

    calls = [call[0] for call in created[0].calls]
    assert calls == ["ehlo", "starttls", "ehlo", "send_message"]
    content = created[0].message.get_content()
    assert "https://console.example.test/reset-password#token=reset_token" in content
    assert "/reset-password?token=" not in content


def test_smtp_errors_are_sanitized_and_do_not_leak_credentials_or_token():
    class ExplodingSmtp(_FakeSmtp):
        def send_message(self, _message):
            raise RuntimeError("secret-password recipient@example.test token=reset_secret")

    def factory(host, port, **kwargs):
        return ExplodingSmtp(host, port, **kwargs)

    sender = SmtpEmailSender(
        "smtps://mailer:secret-password@smtp.example.test:465",
        from_address="noreply@example.test",
        public_base_url="https://console.example.test",
        smtp_ssl_factory=factory,
    )
    with pytest.raises(IdentityError) as exc_info:
        sender.send_password_reset("recipient@example.test", "reset_secret")
    assert str(exc_info.value) == "邮件发送失败"
    assert "secret-password" not in str(exc_info.value)
    assert "reset_secret" not in str(exc_info.value)
