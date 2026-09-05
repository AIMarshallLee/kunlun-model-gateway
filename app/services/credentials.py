"""Credential operations isolated from the application runtime database role."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import Settings
from app.db import build_engine


class SecretUnavailable(RuntimeError):
    """A credential operation could not be completed atomically and safely."""


@dataclass(frozen=True, slots=True)
class CredentialConnection:
    id: str
    provider: str
    label: str | None
    status: str
    credential_version: int
    created_at: str
    updated_at: str
    revoked_at: str | None


class CredentialVault(Protocol):
    manages_metadata: bool

    def put(self, *, user_id: str, connection_id: str, provider: str,
            credential_version: int, secret: str) -> None: ...

    def get(self, *, user_id: str, connection_id: str, provider: str,
            credential_version: int) -> str: ...

    def destroy(self, *, user_id: str, connection_id: str, provider: str,
                credential_version: int) -> None: ...

    def provision(self, *, user_id: str, provider: str, label: str | None,
                  secret: str) -> CredentialConnection: ...

    def revoke(self, *, user_id: str, provider: str) -> None: ...
    def probe(self) -> bool: ...


class DisabledCredentialVault:
    manages_metadata = False
    def put(self, **_kwargs: object) -> None: raise SecretUnavailable("credential vault is disabled")
    def get(self, **_kwargs: object) -> str: raise SecretUnavailable("credential vault is disabled")
    def destroy(self, **_kwargs: object) -> None: raise SecretUnavailable("credential vault is disabled")
    def provision(self, **_kwargs: object) -> CredentialConnection: raise SecretUnavailable("credential vault is disabled")
    def revoke(self, **_kwargs: object) -> None: raise SecretUnavailable("credential vault is disabled")
    def probe(self) -> bool: return False


class InMemoryCredentialVault:
    """Test-only tuple binding; it deliberately has no public vault reference."""
    manages_metadata = False

    def __init__(self) -> None:
        self._values: dict[tuple[str, str, str, int], str] = {}

    @staticmethod
    def _key(*, user_id: str, connection_id: str, provider: str, credential_version: int) -> tuple[str, str, str, int]:
        if not user_id or not connection_id or not provider or credential_version < 1:
            raise SecretUnavailable("credential binding is invalid")
        return user_id, connection_id, provider, credential_version

    def put(self, *, secret: str, **kwargs: object) -> None:
        if not secret:
            raise SecretUnavailable("credential is unavailable")
        self._values[self._key(**kwargs)] = secret  # type: ignore[arg-type]

    def get(self, **kwargs: object) -> str:
        try:
            return self._values[self._key(**kwargs)]  # type: ignore[arg-type]
        except KeyError as exc:
            raise SecretUnavailable("credential is unavailable") from exc

    def destroy(self, **kwargs: object) -> None:
        self._values.pop(self._key(**kwargs), None)  # type: ignore[arg-type]

    def provision(self, **_kwargs: object) -> CredentialConnection:
        raise SecretUnavailable("test vault does not manage metadata")
    def revoke(self, **_kwargs: object) -> None:
        raise SecretUnavailable("test vault does not manage metadata")
    def probe(self) -> bool: return True
    def values(self): return self._values.values()  # pragma: no cover


class SupabaseVaultCredentialVault:
    """Uses a separate executor engine and only v2 SECURITY DEFINER calls."""
    manages_metadata = True

    def __init__(self, executor_engine: Engine) -> None:
        self._executor_engine = executor_engine

    @staticmethod
    def _params(*, user_id: str, connection_id: str, provider: str,
                credential_version: int) -> dict[str, object]:
        if not user_id or not connection_id or not provider or credential_version < 1:
            raise SecretUnavailable("credential binding is invalid")
        return {"user_id": user_id, "connection_id": connection_id,
                "provider": provider, "credential_version": credential_version}

    @staticmethod
    def _connection(row: object) -> CredentialConnection:
        values = tuple(row)  # SQLAlchemy Row is tuple-compatible.
        if len(values) != 8:
            raise SecretUnavailable("credential vault returned invalid metadata")
        return CredentialConnection(*values)  # type: ignore[arg-type]

    def provision(self, *, user_id: str, provider: str, label: str | None, secret: str) -> CredentialConnection:
        if not user_id or not provider or not secret:
            raise SecretUnavailable("credential is unavailable")
        try:
            with self._executor_engine.begin() as connection:
                row = connection.execute(text("""
                    SELECT * FROM kunlun_private.credential_put_v2(
                      CAST(:user_id AS uuid), :provider, :label, :secret)
                """), {"user_id": user_id, "provider": provider, "label": label, "secret": secret}).one()
        except Exception as exc:
            raise SecretUnavailable("credential vault is unavailable") from exc
        return self._connection(row)

    def revoke(self, *, user_id: str, provider: str) -> None:
        try:
            with self._executor_engine.begin() as connection:
                connection.execute(text("""
                    SELECT kunlun_private.credential_revoke_v2(CAST(:user_id AS uuid), :provider)
                """), {"user_id": user_id, "provider": provider}).scalar_one()
        except Exception as exc:
            raise SecretUnavailable("credential vault is unavailable") from exc

    def get(self, *, user_id: str, connection_id: str, provider: str, credential_version: int) -> str:
        try:
            with self._executor_engine.connect() as connection:
                secret = connection.execute(text("""
                    SELECT kunlun_private.credential_resolve_v2(
                      CAST(:user_id AS uuid), CAST(:connection_id AS uuid), :provider, :credential_version)
                """), self._params(user_id=user_id, connection_id=connection_id,
                                    provider=provider, credential_version=credential_version)).scalar_one()
        except Exception as exc:
            raise SecretUnavailable("credential vault is unavailable") from exc
        if not isinstance(secret, str) or not secret:
            raise SecretUnavailable("credential is unavailable")
        return secret

    def put(self, **_kwargs: object) -> None: raise SecretUnavailable("Supabase credential metadata is executor-managed")
    def destroy(self, **_kwargs: object) -> None: raise SecretUnavailable("Supabase credential metadata is executor-managed")
    def probe(self) -> bool:
        try:
            with self._executor_engine.connect() as connection:
                return connection.execute(text("SELECT kunlun_private.credential_probe_v2()")).scalar_one() is True
        except Exception:
            return False


def build_credential_vault(settings: Settings, _runtime_engine: Engine) -> CredentialVault:
    if settings.vault_backend == "disabled":
        return DisabledCredentialVault()
    if settings.vault_backend != "supabase_vault" or not settings.vault_executor_database_url:
        raise SecretUnavailable("credential vault backend is unsupported")
    vault = SupabaseVaultCredentialVault(build_engine(settings.vault_executor_database_url))
    if not vault.probe():
        raise SecretUnavailable("Supabase Vault probe failed")
    return vault
