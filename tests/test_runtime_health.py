from __future__ import annotations

from fastapi.testclient import TestClient

from app import create_app
from app.services.ops_tokens import mint_operator_token


OPS_SECRET = "runtime-health-operator-signing-secret-32-bytes-minimum"


def test_readiness_checks_database_and_metrics_are_private(tmp_path):
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'health.sqlite3'}",
        operator_signing_secret=OPS_SECRET,
        provider_clients=[],
    )
    metrics_token = mint_operator_token(
        OPS_SECRET,
        subject="prometheus",
        scopes={"metrics:read"},
    )
    with TestClient(app) as client:
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["checks"]["database"] == "ok"
        assert ready.headers["X-Request-Id"]
        assert client.get("/metrics").status_code in {401, 404}
        scraped = client.get("/metrics", headers={"X-Kunlun-Ops-Token": metrics_token})
        assert scraped.status_code == 200
        assert "gateway_http_requests_total" in scraped.text
        assert "prompt" not in scraped.text


def test_readiness_fails_closed_when_database_check_raises(tmp_path):
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'health-fail.sqlite3'}",
        provider_clients=[],
    )

    class BrokenSession:
        def __enter__(self):
            raise RuntimeError("postgresql://user:secret@private")

        def __exit__(self, *args):
            return False

    app.state.SessionLocal = BrokenSession
    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        assert "secret" not in response.text
