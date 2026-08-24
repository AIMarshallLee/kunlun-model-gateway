from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import create_app
from app.models import ApiKey, User
from app.services.identity import InMemoryEmailSender


class FakeCaptcha:
    def __init__(self):
        self.calls = []

    async def verify(
        self, token: str, *, remote_ip: str | None = None,
        expected_action: str | None = None,
    ) -> bool:
        self.calls.append((token, remote_ip, expected_action))
        return token == "captcha-ok"


class BlockingEmailSender(InMemoryEmailSender):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def send_verification(self, recipient: str, token: str) -> None:
        self.started.set()
        if not self.release.wait(5):
            raise AssertionError("test email release timed out")
        super().send_verification(recipient, token)


def _verified_client(tmp_path, *, max_active_api_keys: int = 2):
    sender = InMemoryEmailSender()
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'verified.sqlite3'}",
        public_signup=True,
        require_email_verification=True,
        identity_token_pepper="identity-pepper-for-tests",
        identity_sender=sender,
        max_active_api_keys=max_active_api_keys,
        provider_clients=[],
    )
    return app, sender


def test_registration_requires_one_time_email_verification(tmp_path):
    app, sender = _verified_client(tmp_path)
    payload = {"email": "verify@example.com", "password": "correct horse battery staple"}
    with TestClient(app) as client:
        created = client.post("/auth/register", json=payload)
        assert created.status_code == 202
        assert created.json() == {"accepted": True}
        assert len(sender.messages) == 1
        assert sender.messages[0].token.startswith("verify_")
        assert client.post("/auth/login", json=payload).status_code == 401

        verified = client.post("/auth/verify-email", json={"token": sender.messages[0].token})
        assert verified.status_code == 200
        assert verified.json() == {"verified": True}
        assert client.post("/auth/verify-email", json={"token": sender.messages[0].token}).status_code == 400
        assert client.post("/auth/login", json=payload).status_code == 200


def test_slow_verification_email_does_not_block_health_endpoint(tmp_path):
    sender = BlockingEmailSender()
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'nonblocking-email.sqlite3'}",
        public_signup=True,
        require_email_verification=True,
        identity_token_pepper="identity-pepper-for-tests",
        identity_sender=sender,
        provider_clients=[],
    )
    payload = {"email": "slow-email@example.com", "password": "correct horse battery staple"}
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        registration = pool.submit(client.post, "/auth/register", json=payload)
        assert sender.started.wait(5)
        health = pool.submit(client.get, "/healthz")
        try:
            health_response = health.result(timeout=0.75)
        finally:
            sender.release.set()
        assert health_response.status_code == 200
        assert registration.result(timeout=5).status_code == 202


def test_password_reset_is_generic_and_rotates_all_credentials(tmp_path):
    app, sender = _verified_client(tmp_path)
    original = {"email": "reset@example.com", "password": "correct horse battery staple"}
    with TestClient(app) as client:
        assert client.post("/auth/register", json=original).status_code == 202
        token = sender.messages[-1].token
        assert client.post("/auth/verify-email", json={"token": token}).status_code == 200
        login = client.post("/auth/login", json=original).json()["access_token"]
        auth = {"Authorization": f"Bearer {login}"}
        created_key = client.post("/v1/keys", headers=auth, json={"name": "before-reset"})
        assert created_key.status_code == 201

        missing = client.post("/auth/forgot-password", json={"email": "missing@example.com"})
        existing = client.post("/auth/forgot-password", json={"email": original["email"]})
        assert missing.status_code == existing.status_code == 202
        assert missing.json() == existing.json()
        reset_token = sender.messages[-1].token
        reset = client.post("/auth/reset-password", json={
            "token": reset_token,
            "new_password": "an entirely new strong password",
        })
        assert reset.status_code == 200
        assert client.get("/v1/keys", headers=auth).status_code == 401
        assert client.get("/v1/models", headers={
            "Authorization": f"Bearer {created_key.json()['key']}"
        }).status_code == 401
        assert client.post("/auth/login", json=original).status_code == 401
        assert client.post("/auth/login", json={
            "email": original["email"],
            "password": "an entirely new strong password",
        }).status_code == 200


def test_resend_verification_is_enumeration_safe(tmp_path):
    app, sender = _verified_client(tmp_path)
    payload = {"email": "pending@example.com", "password": "correct horse battery staple"}
    with TestClient(app) as client:
        assert client.post("/auth/register", json=payload).status_code == 202
        sender.messages.clear()
        missing = client.post("/auth/resend-verification", json={"email": "missing@example.com"})
        existing = client.post("/auth/resend-verification", json={"email": payload["email"]})
        assert missing.status_code == existing.status_code == 202
        assert missing.json() == existing.json() == {"accepted": True}
        assert len(sender.messages) == 1


def test_verified_registration_duplicate_is_enumeration_safe(tmp_path):
    app, _sender = _verified_client(tmp_path)
    payload = {"email": "duplicate@example.com", "password": "correct horse battery staple"}
    with TestClient(app) as client:
        first = client.post("/auth/register", json=payload)
        duplicate = client.post("/auth/register", json=payload)
        assert first.status_code == duplicate.status_code == 202
        assert first.json() == duplicate.json() == {"accepted": True}


def test_key_limit_is_enforced_after_verification(tmp_path):
    app, sender = _verified_client(tmp_path, max_active_api_keys=1)
    payload = {"email": "limit@example.com", "password": "correct horse battery staple"}
    with TestClient(app) as client:
        client.post("/auth/register", json=payload)
        client.post("/auth/verify-email", json={"token": sender.messages[-1].token})
        session_token = client.post("/auth/login", json=payload).json()["access_token"]
        headers = {"Authorization": f"Bearer {session_token}"}
        assert client.post("/v1/keys", headers=headers, json={"name": "first"}).status_code == 201
        blocked = client.post("/v1/keys", headers=headers, json={"name": "second"})
        assert blocked.status_code == 409

    with app.state.SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == payload["email"]))
        assert user is not None and user.email_verified_at is not None
        keys = session.scalars(select(ApiKey).where(ApiKey.user_id == user.id)).all()
        assert len(keys) == 1


def test_public_registration_can_require_server_verified_captcha(tmp_path):
    captcha = FakeCaptcha()
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'captcha-route.sqlite3'}",
        public_signup=True,
        captcha_required=True,
        captcha_adapter=captcha,
        provider_clients=[],
    )
    base = {"email": "captcha@example.com", "password": "correct horse battery staple"}
    with TestClient(app) as client:
        assert client.post("/auth/register", json=base).status_code == 403
        assert client.post("/auth/register", json={**base, "captcha_token": "captcha-bad"}).status_code == 403
        assert client.post("/auth/register", json={**base, "captcha_token": "captcha-ok"}).status_code == 201
        assert client.post("/auth/resend-verification", json={
            "email": "missing@example.com", "captcha_token": "captcha-ok",
        }).status_code == 202
        assert client.post("/auth/forgot-password", json={
            "email": "missing@example.com", "captcha_token": "captcha-ok",
        }).status_code == 202
    assert [call[0] for call in captcha.calls] == [
        "captcha-bad", "captcha-ok", "captcha-ok", "captcha-ok",
    ]
    assert [call[2] for call in captcha.calls] == [
        "register", "register", "resend_verification", "password_reset",
    ]
