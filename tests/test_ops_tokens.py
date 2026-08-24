from __future__ import annotations

import pytest

from app.services.ops_tokens import OpsTokenError, mint_operator_token, verify_operator_token


SECRET = "ops-signing-secret-that-is-at-least-thirty-two-bytes"


def test_short_lived_operator_token_enforces_scope_and_expiry():
    token = mint_operator_token(
        SECRET,
        subject="oncall@example.com",
        scopes={"reconciliation:read"},
        ttl_seconds=300,
        now=1_700_000_000,
    )
    claims = verify_operator_token(
        token,
        SECRET,
        required_scope="reconciliation:read",
        now=1_700_000_100,
    )
    assert claims.subject == "oncall@example.com"
    assert claims.expires_at == 1_700_000_300

    with pytest.raises(OpsTokenError, match="权限"):
        verify_operator_token(
            token,
            SECRET,
            required_scope="reconciliation:write",
            now=1_700_000_100,
        )
    with pytest.raises(OpsTokenError, match="过期"):
        verify_operator_token(
            token,
            SECRET,
            required_scope="reconciliation:read",
            now=1_700_000_301,
        )


def test_operator_token_rejects_tampering_long_ttl_and_weak_secret():
    with pytest.raises(OpsTokenError, match="密钥"):
        mint_operator_token("short", subject="operator", scopes={"reconciliation:read"})
    with pytest.raises(OpsTokenError, match="有效期"):
        mint_operator_token(
            SECRET,
            subject="operator",
            scopes={"reconciliation:read"},
            ttl_seconds=901,
        )

    token = mint_operator_token(
        SECRET,
        subject="operator",
        scopes={"reconciliation:write"},
        now=1_700_000_000,
    )
    prefix, payload, signature = token.split(".")
    tampered = ".".join((prefix, payload[:-1] + ("A" if payload[-1] != "A" else "B"), signature))
    with pytest.raises(OpsTokenError, match="签名"):
        verify_operator_token(
            tampered,
            SECRET,
            required_scope="reconciliation:write",
            now=1_700_000_001,
        )
