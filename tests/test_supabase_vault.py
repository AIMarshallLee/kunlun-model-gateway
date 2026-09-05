from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.services.credentials import SupabaseVaultCredentialVault


@dataclass
class RecordingConnection:
    value: object = True
    statements: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    def execute(self, statement, params=None):  # type: ignore[no-untyped-def]
        self.statements.append((str(statement), dict(params or {})))
        return self
    def scalar_one(self): return self.value
    def one(self): return self.value
    def __enter__(self): return self
    def __exit__(self, *_args): return False


@dataclass
class RecordingEngine:
    connection: RecordingConnection
    def connect(self): return self.connection
    def begin(self): return self.connection


USER_ID = "00000000-0000-4000-8000-000000000001"
CONNECTION_ID = "00000000-0000-4000-8000-000000000002"


def test_executor_adapter_only_uses_v2_functions_and_never_accepts_vault_ref():
    connection = RecordingConnection(value="private-secret")
    vault = SupabaseVaultCredentialVault(RecordingEngine(connection))
    assert vault.get(user_id=USER_ID, connection_id=CONNECTION_ID, provider="openai", credential_version=2) == "private-secret"
    vault.revoke(user_id=USER_ID, provider="openai")
    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "credential_resolve_v2" in sql and "credential_revoke_v2" in sql
    assert "vault_ref" not in sql and "vault.decrypted_secrets" not in sql


def test_put_function_qualifies_columns_that_overlap_return_table_names():
    source = (Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0012_supabase_vault.py").read_text()
    assert "SELECT candidate.* INTO pc" in source
    assert "candidate.user_id = p_user_id::text" in source
    assert "candidate.provider = p_provider" in source
    assert "UPDATE public.provider_connections AS target" in source
    assert "WHERE target.id=pc.id" in source
    assert "c.id::text,c.provider::text,c.label::text,c.status::text" in source
    assert source.count("DELETE FROM vault.secrets AS secret_row") == 2
    assert "secret_row.id = old_ref" in source
    assert "secret_row.id=ref" in source
    assert "pc.status NOT IN ('active', 'revoked')" in source
    assert "pc.status = 'active' AND old_ref IS NULL" in source
    assert "revoked credential still has a private binding" in source


def test_executor_probe_fails_closed():
    connection = RecordingConnection(value=True)
    assert SupabaseVaultCredentialVault(RecordingEngine(connection)).probe() is True
    assert "credential_probe_v2" in connection.statements[0][0]
    assert SupabaseVaultCredentialVault(RecordingEngine(RecordingConnection(value=False))).probe() is False


def test_v2_migration_separates_runtime_executor_and_private_bindings():
    source = (Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0012_supabase_vault.py").read_text()
    assert "provider_credential_bindings" in source
    assert "credential_put_v2" in source and "credential_resolve_v2" in source and "credential_revoke_v2" in source
    assert "SECURITY DEFINER SET search_path = pg_catalog" in source
    assert "TO kunlun_vault_executor" in source
    assert "kunlun_runtime" in source and "REVOKE ALL ON ALL TABLES" in source
    assert "vault_ref" not in (Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0011_byok_credentials.py").read_text()


def test_v2_migration_persists_a_single_installation_marker_for_all_application_roles():
    source = (Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0012_supabase_vault.py").read_text()
    assert "CREATE TABLE kunlun_private.installation_marker" in source
    assert "singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton)" in source
    assert "installation_id uuid NOT NULL DEFAULT gen_random_uuid()" in source
    assert "INSERT INTO kunlun_private.installation_marker(singleton) VALUES (true)" in source
    assert "ON CONFLICT (singleton) DO NOTHING" in source
    assert "CREATE OR REPLACE FUNCTION public.kunlun_installation_id()" in source
    assert "RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog" in source
    assert "session_user NOT IN ('kunlun_runtime', 'kunlun_migrator', 'kunlun_vault_executor')" in source
    assert "FROM kunlun_private.installation_marker AS marker" in source
    assert "REVOKE ALL ON FUNCTION public.kunlun_installation_id() FROM PUBLIC, anon, authenticated" in source
    assert "TO kunlun_runtime, kunlun_migrator, kunlun_vault_executor" in source


def test_bootstrap_keeps_runtime_out_of_private_schema():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_supabase_vault.sql").read_text()
    assert "kunlun_vault_executor" in source
    assert "kunlun_runtime" in source
    assert "GRANT SELECT, DELETE ON TABLE vault.secrets TO kunlun_migrator" in source
    assert "REVOKE ALL ON SCHEMA vault FROM PUBLIC, anon, authenticated, kunlun_runtime, kunlun_vault_executor" in source
    assert "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA vault FROM PUBLIC, anon, authenticated, kunlun_runtime, kunlun_vault_executor" in source
    assert "sk-" not in source
