from __future__ import annotations

from pathlib import Path

from scripts.preflight import (
    AUDIT_TRIGGER_CONTRACT,
    LEDGER_TRIGGER_CONTRACT,
    _database_user,
    _database_credential_errors,
    _database_target_errors,
    _installation_marker_errors,
    _runtime_permission_errors,
    _migrator_database_errors,
    _supabase_rls_errors,
    _trigger_guard_sql,
    _vault_contract_errors,
)
from app.db_guards import ledger_trigger_names, operator_audit_trigger_names


def test_trigger_guard_query_covers_every_expected_trigger_and_rejects_disabled():
    ledger_sql = _trigger_guard_sql(LEDGER_TRIGGER_CONTRACT, "ledger_guards")
    audit_sql = _trigger_guard_sql(AUDIT_TRIGGER_CONTRACT, "audit_guards")

    for name in ledger_trigger_names() + operator_audit_trigger_names():
        assert f"'{name}'" in ledger_sql + audit_sql
    assert "t.tgenabled = 'O'" in ledger_sql
    assert "t.tgtype = expected.trigger_type" in ledger_sql
    assert "t.tgdeferrable = expected.expected_deferrable" in ledger_sql
    assert "ledger_entries_balance_deferred" in ledger_sql
    assert "kunlun_check_ledger_transaction_balance" in ledger_sql
    assert "function_namespace.nspname = 'public'" in ledger_sql
    assert "COUNT(*) = 5" in ledger_sql
    assert "COUNT(*) = 3" in audit_sql


class _FakeResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one(self):
        return self.row


class _FakeConnection:
    def __init__(self, row):
        self.row = row
        self.statement = None

    def execute(self, statement, _params=None):
        self.statement = str(statement)
        return _FakeResult(self.row)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeEngine:
    def __init__(self, row):
        self.connection = _FakeConnection(row)

    def connect(self):
        return self.connection


def _permission_row(*, ledger_guards=True, audit_guards=True, function_search_paths=True):
    row = {
        "current_user": "kunlun_runtime",
        "ledger_guards": ledger_guards,
        "audit_guards": audit_guards,
        "function_search_paths": function_search_paths,
        "runtime_role_safe": True,
        "runtime_memberships_safe": True,
        "kunlun_role_memberships_safe": True,
    }
    for key in (
        "ledger_select", "ledger_insert", "entry_select", "entry_insert",
        "audit_select", "audit_insert", "version_select",
    ):
        row[key] = True
    for key in (
        "schema_create", "ledger_update", "ledger_delete", "ledger_truncate", "ledger_references", "ledger_trigger",
        "entry_update", "entry_delete", "entry_truncate", "entry_references", "entry_trigger",
        "audit_update", "audit_delete", "audit_truncate", "audit_references", "audit_trigger",
        "version_insert", "version_update", "version_delete", "version_truncate", "version_references", "version_trigger",
    ):
        row[key] = False
    return row


def test_runtime_permission_preflight_fails_when_ledger_trigger_is_missing_or_disabled():
    ledger_engine = _FakeEngine(_permission_row(ledger_guards=False))
    assert _runtime_permission_errors(ledger_engine, "kunlun_runtime")
    rendered = ledger_engine.connection.statement
    assert "{" not in rendered and "}" not in rendered
    for name in ledger_trigger_names() + operator_audit_trigger_names():
        assert name in rendered
    assert _runtime_permission_errors(_FakeEngine(_permission_row(audit_guards=False)), "kunlun_runtime")
    assert _runtime_permission_errors(
        _FakeEngine(_permission_row(function_search_paths=False)), "kunlun_runtime"
    )
    for key in ("runtime_role_safe", "runtime_memberships_safe", "kunlun_role_memberships_safe"):
        row = _permission_row()
        row[key] = False
        assert _runtime_permission_errors(_FakeEngine(row), "kunlun_runtime")


def test_supabase_rls_preflight_checks_every_table_and_api_role_boundary():
    good = {
        "rls_and_owner_contract": True,
        "policy_contract": True,
        "ordinary_privileges": True,
        "credential_metadata_read_only": True,
        "api_grants_locked": True,
        "api_policies_locked": True,
        "api_effective_privileges_locked": True,
    }
    engine = _FakeEngine(good)
    assert _supabase_rls_errors(engine) == []
    rendered = engine.connection.statement
    assert "pg_policies" in rendered
    assert "policyname = 'kunlun_runtime_all_' || tablename" in rendered
    assert "ordinary_privileges" in rendered
    assert "aclexplode" in rendered
    assert "acl.grantee = 0" in rendered
    assert "pg_get_userbyid(c.relowner) = 'kunlun_migrator'" in rendered
    assert "api_effective_privileges_locked" in rendered
    assert "has_any_column_privilege" in rendered
    assert "anon" in rendered and "authenticated" in rendered

    for key in good:
        row = dict(good)
        row[key] = False
        assert _supabase_rls_errors(_FakeEngine(row))


def test_migrator_database_url_requires_independent_verified_tls_connection():
    ca_path = Path(__file__).resolve().parents[1] / "certs" / "supabase-prod-ca-2021.crt"
    safe = (
        "postgresql+psycopg://kunlun_migrator:secret@db.example/postgres"
        f"?sslmode=verify-full&sslrootcert={ca_path}"
    )
    assert _migrator_database_errors(safe, "kunlun_runtime") == []
    assert _migrator_database_errors(safe, "kunlun_migrator")
    wrong_role = safe.replace("kunlun_migrator", "custom_migrator", 1)
    assert _migrator_database_errors(wrong_role, "kunlun_runtime")
    assert _migrator_database_errors(
        "postgresql+psycopg://kunlun_migrator:secret@db.example/postgres?sslmode=require",
        "kunlun_runtime",
    )


def test_database_credential_preflight_rejects_percent_decoded_duplicates():
    ca_path = Path(__file__).resolve().parents[1] / "certs" / "supabase-prod-ca-2021.crt"
    runtime = f"postgresql+psycopg://kunlun_runtime:shared%2Fsecret@db.example/postgres?sslmode=verify-full&sslrootcert={ca_path}"
    migrator = f"postgresql+psycopg://kunlun_migrator:shared%2fsecret@db.example/postgres?sslmode=verify-full&sslrootcert={ca_path}"
    executor = f"postgresql+psycopg://kunlun_vault_executor:executor-secret@db.example/postgres?sslmode=verify-full&sslrootcert={ca_path}"
    errors = _database_credential_errors(runtime, migrator, executor)
    assert errors == ["KUNLUN_RUNTIME_DATABASE_URL 与 KUNLUN_MIGRATOR_DATABASE_URL 数据库凭据不得重复"]
    assert _database_credential_errors(runtime, migrator) == errors


def test_database_target_preflight_rejects_other_project_or_cloned_database_name():
    ca_path = Path(__file__).resolve().parents[1] / "certs" / "supabase-prod-ca-2021.crt"
    project_ref = "oyhavtaalkidrllxfigw"
    def url(role: str, database: str = "postgres", project: str = project_ref) -> str:
        return (
            f"postgresql+psycopg://{role}.{project}:secret@"
            f"aws-0-ca-central-1.pooler.supabase.com/{database}"
            f"?sslmode=verify-full&sslrootcert={ca_path}"
        )

    runtime = url("kunlun_runtime")
    migrator = url("kunlun_migrator")
    executor = url("kunlun_vault_executor")
    assert _database_target_errors(runtime, migrator, executor) == []
    assert _database_target_errors(
        runtime, migrator, url("kunlun_vault_executor", database="cloned_postgres"),
    )
    assert _database_target_errors(
        runtime, migrator,
        url("kunlun_vault_executor", project="abcdefghijklmnopqrst"),
    )
    assert _database_target_errors(
        runtime, migrator,
        "postgresql+psycopg://kunlun_vault_executor:secret@self-hosted/postgres",
    )


def test_installation_marker_preflight_requires_same_database_and_exact_roles():
    marker = "8a277174-f81d-4ce4-a068-b212cf923a91"
    runtime = _FakeEngine({
        "current_user": "kunlun_runtime", "installation_id": marker,
        "marker_contract": True,
    })
    migrator = _FakeEngine({
        "current_user": "kunlun_migrator", "installation_id": marker,
        "marker_contract": True,
    })
    executor = _FakeEngine({
        "current_user": "kunlun_vault_executor", "installation_id": marker,
        "marker_contract": True,
    })
    assert _installation_marker_errors(
        runtime, migrator, executor,
        "kunlun_runtime", "kunlun_migrator", "kunlun_vault_executor",
    ) == []

    other = _FakeEngine({
        "current_user": "kunlun_vault_executor",
        "installation_id": "0ad44ea4-724c-4ad7-952e-99d909c3e101",
        "marker_contract": True,
    })
    assert _installation_marker_errors(
        runtime, migrator, other,
        "kunlun_runtime", "kunlun_migrator", "kunlun_vault_executor",
    ) == ["runtime、migrator 与 Vault executor 未连接同一数据库安装"]

    wrong_role = _FakeEngine({
        "current_user": "postgres", "installation_id": marker,
        "marker_contract": True,
    })
    assert _installation_marker_errors(
        runtime, wrong_role, executor,
        "kunlun_runtime", "kunlun_migrator", "kunlun_vault_executor",
    )

    unsafe_contract = _FakeEngine({
        "current_user": "kunlun_vault_executor", "installation_id": marker,
        "marker_contract": False,
    })
    assert _installation_marker_errors(
        runtime, migrator, unsafe_contract,
        "kunlun_runtime", "kunlun_migrator", "kunlun_vault_executor",
    )


def test_preflight_normalizes_only_supavisor_project_qualified_users():
    assert _database_user(
        "postgresql+psycopg://kunlun_runtime.oyhavtaalkidrllxfigw:secret@"
        "aws-0-ca-central-1.pooler.supabase.com:5432/postgres"
    ) == "kunlun_runtime"
    assert _database_user(
        "postgresql+psycopg://role.with.dot:secret@db.example/postgres"
    ) == "role.with.dot"


def test_vault_preflight_uses_catalog_oids_for_non_visible_private_relations():
    source = _vault_contract_errors.__code__.co_consts
    rendered = "\n".join(item for item in source if isinstance(item, str))
    assert "protected_relations" in rendered
    assert "has_table_privilege(current_user, oid" in rendered
    assert "has_table_privilege(current_user, 'vault." not in rendered
    assert "identity_arguments = 'new_secret text, new_name text, new_description text'" in rendered
    assert "executor_role_safe" in rendered
    assert "executor_memberships_safe" in rendered
    assert "api_effective_access_denied" in rendered
    assert "has_any_column_privilege" in rendered
    assert "protected_functions" in rendered
    assert "n.nspname IN ('kunlun_private', 'vault')" in rendered
    assert "relation.nspname IN ('kunlun_private', 'vault')" in rendered
    assert "has_schema_privilege(api_role.rolname, relation.nspname, 'USAGE')" in rendered
    assert "pg_auth_members" in rendered


def test_role_membership_queries_reject_both_directions():
    runtime_sql = "\n".join(item for item in _runtime_permission_errors.__code__.co_consts if isinstance(item, str))
    vault_sql = "\n".join(item for item in _vault_contract_errors.__code__.co_consts if isinstance(item, str))
    assert "member.rolname = current_user OR role.rolname = current_user" in runtime_sql
    assert "member.rolname = current_user OR role.rolname = current_user" in vault_sql
    assert "kunlun_migrator" in runtime_sql and "kunlun_vault_executor" in runtime_sql
