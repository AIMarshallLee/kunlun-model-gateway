from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app import create_app
from app.config import Settings
from app.services.ops_tokens import mint_operator_token
from app.vercel import VercelIngressMiddleware, public_route_allowed
from scripts.maintenance import run_once as run_maintenance_once


ROOT = Path(__file__).resolve().parents[1]


def test_vercel_container_and_cron_contract_are_explicit():
    dockerfile = (ROOT / "Dockerfile.vercel").read_text(encoding="utf-8")
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "app.main:app" in dockerfile
    assert "${PORT:-80}" in dockerfile
    assert "--no-proxy-headers" in dockerfile
    assert "COPY certs/supabase-prod-ca-2021.crt /app/certs/" in dockerfile
    assert config["regions"] == ["yul1"]
    assert config["crons"] == [{
        "path": "/api/cron/maintenance",
        "schedule": "*/5 * * * *",
    }]
    assert "docker build --file Dockerfile.vercel" in workflow
    assert "curl --fail --retry" in workflow


def test_vercel_ingress_requires_persisted_proxy_and_cron_secrets(monkeypatch):
    monkeypatch.setenv("KUNLUN_INGRESS_PROVIDER", "vercel")
    with pytest.raises(RuntimeError, match="CRON_SECRET"):
        Settings.from_env()

    monkeypatch.setenv("KUNLUN_TRUSTED_PROXY_SECRET", "p" * 32)
    monkeypatch.setenv("CRON_SECRET", "c" * 32)
    monkeypatch.setenv("KUNLUN_OPS_INGRESS_SECRET", "i" * 32)
    monkeypatch.setenv("KUNLUN_OPERATOR_SIGNING_SECRET", "o" * 32)
    settings = Settings.from_env()

    assert settings.ingress_provider == "vercel"
    assert settings.cron_secret == "c" * 32
    assert settings.ops_ingress_secret == "i" * 32


def test_vercel_ingress_overwrites_spoofed_proxy_headers_and_hides_ops(tmp_path, monkeypatch):
    monkeypatch.setenv("KUNLUN_INGRESS_PROVIDER", "vercel")
    monkeypatch.setenv("KUNLUN_TRUSTED_PROXY_SECRET", "p" * 32)
    monkeypatch.setenv("CRON_SECRET", "c" * 32)
    monkeypatch.setenv("KUNLUN_OPS_INGRESS_SECRET", "i" * 32)
    monkeypatch.setenv("KUNLUN_OPERATOR_SIGNING_SECRET", "o" * 32)
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'vercel-ingress.sqlite3'}",
        provider_clients=[],
    )

    with TestClient(app, client=("10.0.0.5", 50000)) as client:
        headers = {
            "X-Vercel-Id": "sfo1::yul1::request-id",
            "X-Vercel-Forwarded-For": "198.51.100.99",
            "X-Forwarded-For": "203.0.113.9",
            "X-Kunlun-Client-IP": "198.51.100.100",
            "X-Kunlun-Proxy-Secret": "attacker-secret",
        }
        ready = client.get("/readyz", headers=headers)
        private = client.get("/ops/reconciliation", headers=headers)
        encoded_private = client.get("/%256f%2570%2573/reconciliation", headers=headers)

    assert ready.status_code == 200
    assert private.status_code == 404
    assert encoded_private.status_code == 404


def test_vercel_ingress_injects_only_platform_client_ip():
    captured = {}

    async def downstream(scope, _receive, _send):
        captured.update(scope)

    middleware = VercelIngressMiddleware(
        downstream,
        proxy_secret="p" * 32,
        ops_ingress_secret="i" * 32,
    )
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/readyz",
        "raw_path": b"/readyz",
        "client": ("10.0.0.5", 50000),
        "headers": [
            (b"x-vercel-id", b"sfo1::yul1::request-id"),
            (b"x-vercel-forwarded-for", b"198.51.100.99"),
            (b"x-forwarded-for", b"203.0.113.9"),
            (b"x-kunlun-client-ip", b"198.51.100.100"),
            (b"x-kunlun-proxy-secret", b"attacker-secret"),
            (b"accept", b"application/json"),
        ],
    }

    asyncio.run(middleware(scope, None, None))

    assert captured["headers"] == [
        (b"x-vercel-id", b"sfo1::yul1::request-id"),
        (b"accept", b"application/json"),
        (b"x-kunlun-client-ip", b"203.0.113.9"),
        (b"x-kunlun-proxy-secret", b"p" * 32),
    ]


@pytest.mark.parametrize("headers", [
    [(b"x-vercel-id", b"request-id")],
    [(b"x-forwarded-for", b"203.0.113.9")],
    [
        (b"x-vercel-id", b"request-id"),
        (b"x-forwarded-for", b"203.0.113.9"),
        (b"x-forwarded-for", b"198.51.100.9"),
    ],
])
def test_vercel_ingress_does_not_authenticate_missing_or_ambiguous_platform_headers(headers):
    captured = {}

    async def downstream(scope, _receive, _send):
        captured.update(scope)

    middleware = VercelIngressMiddleware(
        downstream, proxy_secret="p" * 32, ops_ingress_secret="i" * 32,
    )
    asyncio.run(middleware({
        "type": "http",
        "method": "GET",
        "path": "/readyz",
        "raw_path": b"/readyz",
        "client": ("10.0.0.5", 50000),
        "headers": headers,
    }, None, None))

    names = {name.lower() for name, _value in captured["headers"]}
    assert b"x-kunlun-client-ip" not in names
    assert b"x-kunlun-proxy-secret" not in names


@pytest.mark.parametrize("path", [
    b"/ops",
    b"/%6f%70%73/reconciliation",
    b"/%256f%2570%2573/reconciliation",
    b"/OPS/reconciliation",
    b"/safe/../ops/reconciliation",
    b"/ops\\reconciliation",
    b"/%2525252525252525256f%25252525252525252570%25252525252525252573",
])
def test_vercel_private_paths_fail_closed_after_normalization(path):
    # Excessively nested escaping is not a valid public route either.
    assert public_route_allowed(path) is (b"%252525" not in path)


def test_vercel_ops_ingress_requires_one_correct_secret_and_strips_it():
    captured = {}

    async def downstream(scope, _receive, _send):
        captured.update(scope)

    middleware = VercelIngressMiddleware(
        downstream, proxy_secret="p" * 32, ops_ingress_secret="i" * 32,
    )
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/ops/reconciliation",
        "raw_path": b"/%6f%70%73/reconciliation",
        "headers": [
            (b"x-kunlun-ops-ingress-secret", b"i" * 32),
            (b"x-vercel-id", b"request-id"),
            (b"x-forwarded-for", b"203.0.113.9"),
        ],
    }
    asyncio.run(middleware(scope, None, None))
    assert not any(name.lower() == b"x-kunlun-ops-ingress-secret" for name, _ in captured["headers"])
    assert (b"x-kunlun-proxy-secret", b"p" * 32) in captured["headers"]


@pytest.mark.parametrize("headers", [
    [],
    [(b"x-kunlun-ops-ingress-secret", b"wrong")],
    [(b"x-kunlun-ops-ingress-secret", b"i" * 32), (b"x-kunlun-ops-ingress-secret", b"i" * 32)],
])
@pytest.mark.parametrize("path", [
    b"/ops", b"/%6f%70%73/reconciliation", b"/%256f%2570%2573/reconciliation",
    b"/%2525252525252525256f%25252525252525252570%25252525252525252573",
])
def test_vercel_ops_ingress_hides_missing_wrong_or_duplicate_secret(headers, path):
    sent = []

    async def downstream(_scope, _receive, _send):
        raise AssertionError("private route reached inner app")

    async def send(message):
        sent.append(message)

    middleware = VercelIngressMiddleware(
        downstream, proxy_secret="p" * 32, ops_ingress_secret="i" * 32,
    )
    asyncio.run(middleware({
        "type": "http", "method": "GET", "path": "/ops", "raw_path": path, "headers": headers,
    }, None, send))
    assert sent[0]["status"] == 404


def test_vercel_ops_ingress_also_requires_scoped_operator_token(tmp_path, monkeypatch):
    monkeypatch.setenv("KUNLUN_INGRESS_PROVIDER", "vercel")
    monkeypatch.setenv("KUNLUN_TRUSTED_PROXY_SECRET", "p" * 32)
    monkeypatch.setenv("CRON_SECRET", "c" * 32)
    monkeypatch.setenv("KUNLUN_OPS_INGRESS_SECRET", "i" * 32)
    operator_secret = "o" * 32
    monkeypatch.setenv("KUNLUN_OPERATOR_SIGNING_SECRET", operator_secret)
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'vercel-ops.sqlite3'}",
        operator_signing_secret=operator_secret,
        provider_clients=[],
    )
    ingress_headers = {"X-Kunlun-Ops-Ingress-Secret": "i" * 32}
    token = mint_operator_token(
        operator_secret, subject="oncall@example.com", scopes={"reconciliation:read"},
    )
    with TestClient(app) as client:
        hidden = client.get("/ops/reconciliation")
        no_operator_token = client.get("/ops/reconciliation", headers=ingress_headers)
        accepted = client.get("/ops/reconciliation", headers={
            **ingress_headers, "X-Kunlun-Ops-Token": token,
        })

    assert hidden.status_code == 404
    assert no_operator_token.status_code == 401
    assert accepted.status_code == 200


def test_postgres_maintenance_skips_when_another_invocation_holds_the_lock():
    class Dialect:
        name = "postgresql"

    class Bind:
        dialect = Dialect()

    class LockedSession:
        bind = Bind()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def scalar(self, _statement, _params):
            return False

        def rollback(self):
            return None

    settings = Settings.from_env()

    assert run_maintenance_once(settings, LockedSession) is None


def test_vercel_cron_requires_bearer_secret_and_runs_once(tmp_path, monkeypatch):
    monkeypatch.setenv("KUNLUN_INGRESS_PROVIDER", "vercel")
    monkeypatch.setenv("KUNLUN_TRUSTED_PROXY_SECRET", "p" * 32)
    monkeypatch.setenv("CRON_SECRET", "c" * 32)
    monkeypatch.setenv("KUNLUN_OPS_INGRESS_SECRET", "i" * 32)
    monkeypatch.setenv("KUNLUN_OPERATOR_SIGNING_SECRET", "o" * 32)
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'vercel-cron.sqlite3'}",
        provider_clients=[],
    )
    ingress_headers = {
        "X-Vercel-Id": "sfo1::yul1::cron-id",
        "X-Forwarded-For": "203.0.113.10",
    }

    with TestClient(app) as client:
        missing = client.get("/api/cron/maintenance", headers=ingress_headers)
        wrong = client.get("/api/cron/maintenance", headers={
            **ingress_headers,
            "Authorization": "Bearer wrong",
        })
        accepted = client.get("/api/cron/maintenance", headers={
            **ingress_headers,
            "Authorization": f"Bearer {'c' * 32}",
        })

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json() == {
        "status": "ok",
        "deleted_auth_rate_limit_counters": 0,
        "deleted_rate_limit_counters": 0,
        "stale_model_reservations": 0,
    }
