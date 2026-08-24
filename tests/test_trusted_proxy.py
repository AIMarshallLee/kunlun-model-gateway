from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app import create_app
from app.client_ip import TrustedProxyClientIPMiddleware, resolve_client_ip
from app.config import Settings


def test_invalid_trusted_proxy_cidr_is_rejected():
    with pytest.raises(RuntimeError, match="TRUSTED_PROXY_CIDRS"):
        Settings.from_env(trusted_proxy_cidrs={"not-a-network"})


def test_spoofed_forwarded_ip_is_ignored_from_untrusted_peer():
    assert resolve_client_ip(
        peer_host="198.51.100.10",
        forwarded_values=[b"203.0.113.9"],
        trusted_proxy_cidrs={"172.30.50.2/32"},
    ) == "198.51.100.10"


def test_exact_trusted_proxy_can_supply_one_valid_client_ip():
    assert resolve_client_ip(
        peer_host="172.30.50.2",
        forwarded_values=[b"203.0.113.9"],
        trusted_proxy_cidrs={"172.30.50.2/32"},
    ) == "203.0.113.9"


def test_authenticated_proxy_can_supply_client_ip_without_fixed_peer_address():
    assert resolve_client_ip(
        peer_host="198.51.100.10",
        forwarded_values=[b"203.0.113.9"],
        trusted_proxy_cidrs=set(),
        proxy_secret_values=[b"cloudflare-worker-shared-secret-1234567890"],
        trusted_proxy_secret="cloudflare-worker-shared-secret-1234567890",
    ) == "203.0.113.9"


def test_wrong_or_duplicate_proxy_secret_is_ignored():
    for values in (
        [b"wrong-secret-that-is-long-enough-123456"],
        [
            b"cloudflare-worker-shared-secret-1234567890",
            b"cloudflare-worker-shared-secret-1234567890",
        ],
    ):
        assert resolve_client_ip(
            peer_host="198.51.100.10",
            forwarded_values=[b"203.0.113.9"],
            trusted_proxy_cidrs=set(),
            proxy_secret_values=values,
            trusted_proxy_secret="cloudflare-worker-shared-secret-1234567890",
        ) == "198.51.100.10"


def test_duplicate_chain_or_invalid_forwarded_value_is_not_trusted():
    for values in (
        [b"203.0.113.9, 198.51.100.4"],
        [b"not-an-ip"],
        [b"203.0.113.9", b"198.51.100.4"],
    ):
        assert resolve_client_ip(
            peer_host="172.30.50.2",
            forwarded_values=values,
            trusted_proxy_cidrs={"172.30.50.2/32"},
        ) == "172.30.50.2"


def test_trusted_proxy_value_reaches_auth_and_captcha(tmp_path, monkeypatch):
    class Captcha:
        remote_ip = None

        async def verify(self, _token, *, remote_ip=None, expected_action=None):
            self.remote_ip = remote_ip
            return expected_action == "register"

    monkeypatch.setenv("KUNLUN_TRUSTED_PROXY_CIDRS", "172.30.50.2/32")
    captcha = Captcha()
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'proxy.sqlite3'}",
        public_signup=True,
        captcha_required=True,
        captcha_adapter=captcha,
        provider_clients=[],
    )
    with TestClient(app, client=("172.30.50.2", 50000)) as client:
        response = client.post(
            "/auth/register",
            headers={"X-Kunlun-Client-IP": "203.0.113.9"},
            json={
                "email": "proxy@example.com",
                "password": "correct horse battery staple",
                "captcha_token": "captcha-ok",
            },
        )
    assert response.status_code == 201
    assert captcha.remote_ip == "203.0.113.9"


def test_authenticated_proxy_value_reaches_application(tmp_path, monkeypatch):
    class Captcha:
        remote_ip = None

        async def verify(self, _token, *, remote_ip=None, expected_action=None):
            self.remote_ip = remote_ip
            return expected_action == "register"

    monkeypatch.setenv(
        "KUNLUN_TRUSTED_PROXY_SECRET",
        "cloudflare-worker-shared-secret-1234567890",
    )
    captcha = Captcha()
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'proxy-secret.sqlite3'}",
        public_signup=True,
        captcha_required=True,
        captcha_adapter=captcha,
        provider_clients=[],
    )
    with TestClient(app, client=("198.51.100.10", 50000)) as client:
        response = client.post(
            "/auth/register",
            headers={
                "X-Kunlun-Client-IP": "203.0.113.9",
                "X-Kunlun-Proxy-Secret": "cloudflare-worker-shared-secret-1234567890",
            },
            json={
                "email": "proxy-secret@example.com",
                "password": "correct horse battery staple",
                "captcha_token": "captcha-ok",
            },
        )
    assert response.status_code == 201
    assert captcha.remote_ip == "203.0.113.9"


def test_production_can_use_persisted_proxy_secret_instead_of_fixed_cidr(monkeypatch):
    monkeypatch.setenv("KUNLUN_ENV", "production")
    monkeypatch.setenv("KUNLUN_DATABASE_URL", "postgresql+psycopg://runtime:secret@db.example/gateway")
    monkeypatch.setenv("KUNLUN_API_KEY_PEPPER", "a" * 32)
    monkeypatch.setenv("KUNLUN_SESSION_PEPPER", "b" * 32)
    monkeypatch.setenv("KUNLUN_TRUSTED_PROXY_SECRET", "c" * 32)

    settings = Settings.from_env()

    assert settings.trusted_proxy_cidrs == set()
    assert settings.trusted_proxy_secret == "c" * 32


def test_short_proxy_secret_is_rejected(monkeypatch):
    monkeypatch.setenv("KUNLUN_TRUSTED_PROXY_SECRET", "too-short")
    with pytest.raises(RuntimeError, match="KUNLUN_TRUSTED_PROXY_SECRET"):
        Settings.from_env()


def test_proxy_internal_headers_are_removed_before_application():
    captured = {}

    async def downstream(scope, _receive, _send):
        captured.update(scope)

    middleware = TrustedProxyClientIPMiddleware(
        downstream,
        trusted_proxy_cidrs=set(),
        trusted_proxy_secret="cloudflare-worker-shared-secret-1234567890",
    )
    scope = {
        "type": "http",
        "client": ("198.51.100.10", 50000),
        "headers": [
            (b"x-kunlun-client-ip", b"203.0.113.9"),
            (
                b"x-kunlun-proxy-secret",
                b"cloudflare-worker-shared-secret-1234567890",
            ),
            (b"accept", b"application/json"),
        ],
    }

    asyncio.run(middleware(scope, None, None))

    assert captured["client"] == ("203.0.113.9", 50000)
    assert captured["headers"] == [(b"accept", b"application/json")]


def test_deployment_overwrites_header_and_disables_uvicorn_proxy_parsing():
    root = Path(__file__).resolve().parents[1]
    caddy = (root / "Caddyfile.production").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    compose = (root / "docker-compose.production.yml").read_text(encoding="utf-8")
    assert "header_up X-Kunlun-Client-IP {remote_host}" in caddy
    assert '"--no-proxy-headers"' in dockerfile
    assert "172.30.50.2" in compose


def test_maintenance_uses_module_packaged_in_runtime_wheel():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.production.yml").read_text(encoding="utf-8")
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert 'command: ["python", "-m", "scripts.maintenance"' in compose
    assert 'include = ["app*", "scripts*"]' in pyproject
    assert "COPY scripts ./scripts" in dockerfile
