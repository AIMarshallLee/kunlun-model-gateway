from __future__ import annotations

from fastapi.testclient import TestClient

from app import create_app
from app.services.ops_tokens import mint_operator_token


SECRET = "operator-signing-secret-with-at-least-thirty-two-bytes"


def test_ops_routes_require_short_lived_scoped_tokens(tmp_path):
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'ops.sqlite3'}",
        operator_signing_secret=SECRET,
        provider_clients=[],
    )
    read_token = mint_operator_token(
        SECRET,
        subject="oncall@example.com",
        scopes={"reconciliation:read"},
    )
    write_token = mint_operator_token(
        SECRET,
        subject="oncall@example.com",
        scopes={"reconciliation:write"},
    )
    with TestClient(app) as client:
        assert client.get("/ops/reconciliation", headers={
            "X-Kunlun-Ops-Token": read_token,
        }).status_code == 200
        assert client.get("/ops/reconciliation", headers={
            "X-Kunlun-Ops-Token": write_token,
        }).status_code == 401
        assert client.post("/ops/reconciliation/missing", headers={
            "X-Kunlun-Ops-Token": read_token,
        }, json={
            "action": "release",
            "reason": "already verified with the upstream provider",
        }).status_code == 401
        # Correct scope reaches the resource lookup and therefore returns 404.
        assert client.post("/ops/reconciliation/missing", headers={
            "X-Kunlun-Ops-Token": write_token,
        }, json={
            "action": "release",
            "reason": "already verified with the upstream provider",
        }).status_code == 404


def test_legacy_operator_token_is_limited_to_reconciliation(tmp_path):
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'legacy-ops.sqlite3'}",
        operator_token="legacy-local-token-with-more-than-32-bytes",
        provider_clients=[],
    )
    with TestClient(app) as client:
        headers = {"X-Kunlun-Ops-Token": "legacy-local-token-with-more-than-32-bytes"}
        assert client.get("/ops/reconciliation", headers=headers).status_code == 200
        # The same legacy secret must not become a metrics/account/payment
        # operator credential merely because it is valid for reconciliation.
        assert client.get("/metrics", headers=headers).status_code == 404
