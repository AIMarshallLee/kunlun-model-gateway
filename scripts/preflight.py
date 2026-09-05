#!/usr/bin/env python3
"""Fail-closed production configuration, role and schema preflight.

This command validates facts the process can observe. It deliberately does not
claim that DNS, inbox delivery, merchant onboarding, filings, WAF or a real
payment/refund has been verified.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import unquote, urlparse
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import (
    Settings,
    _database_name,
    _database_user,
    _production_database_url_is_safe,
    _supabase_project_ref,
)
from app.db import build_engine
from app.db_guards import (
    KUNLUN_BUSINESS_TABLES,
    SCHEMA_HEAD,
    assert_schema_revision,
)


LEDGER_TRIGGER_CONTRACT = (
    ("ledger_transactions_no_update", "ledger_transactions", "kunlun_reject_ledger_mutation", 19, False, False),
    ("ledger_transactions_no_delete", "ledger_transactions", "kunlun_reject_ledger_mutation", 11, False, False),
    ("ledger_entries_no_update", "ledger_entries", "kunlun_reject_ledger_mutation", 19, False, False),
    ("ledger_entries_no_delete", "ledger_entries", "kunlun_reject_ledger_mutation", 11, False, False),
    ("ledger_entries_balance_deferred", "ledger_entries", "kunlun_check_ledger_transaction_balance", 29, True, True),
)
AUDIT_TRIGGER_CONTRACT = (
    ("operator_actions_no_update", "operator_actions", "kunlun_reject_operator_action_mutation", 19, False, False),
    ("operator_actions_no_delete", "operator_actions", "kunlun_reject_operator_action_mutation", 11, False, False),
    ("operator_actions_no_truncate", "operator_actions", "kunlun_reject_operator_action_mutation", 34, False, False),
)


def _database_credential_errors(
    runtime_url: str, migrator_url: str, executor_url: str = "",
) -> list[str]:
    """Compare percent-decoded passwords without including any value in output."""
    urls = {
        "KUNLUN_RUNTIME_DATABASE_URL": runtime_url,
        "KUNLUN_MIGRATOR_DATABASE_URL": migrator_url,
    }
    if executor_url:
        urls["KUNLUN_VAULT_EXECUTOR_DATABASE_URL"] = executor_url
    passwords: dict[str, str] = {}
    for field, value in urls.items():
        try:
            password = unquote(urlparse(value).password or "")
        except (TypeError, ValueError):
            password = ""
        if not password:
            return [f"{field} 缺少可解析的数据库凭据"]
        passwords[field] = password
    fields = tuple(passwords)
    return [f"{left} 与 {right} 数据库凭据不得重复"
            for index, left in enumerate(fields) for right in fields[index + 1:]
            if passwords[left] == passwords[right]]


def _database_target_errors(
    runtime_url: str, migrator_url: str, executor_url: str,
) -> list[str]:
    """Bind every production BYOK role to one managed Supabase database."""
    urls = (runtime_url, migrator_url, executor_url)
    project_refs = tuple(_supabase_project_ref(value) for value in urls)
    database_names = tuple(_database_name(value) for value in urls)
    if not all(project_refs) or not all(database_names):
        return ["BYOK 三角色 URL 必须指向可识别的 Supabase project/database"]
    if (
        not all(secrets.compare_digest(project_refs[0], item) for item in project_refs[1:])
        or len(set(database_names)) != 1
    ):
        return ["BYOK 三角色 URL 未指向同一 Supabase project/database"]
    return []


def _trigger_guard_sql(
    contract: tuple[tuple[str, str, str, int, bool, bool], ...], alias: str,
) -> str:
    """Return a fail-closed check for exact trigger bindings and semantics.

    PostgreSQL ``tgtype`` is a bit mask covering row/statement timing and
    INSERT/DELETE/UPDATE/TRUNCATE events.  Every value here is a fixed source
    constant, never deployment input.
    """
    rows = ", ".join(
        "(" + ", ".join((
            "'" + name.replace("'", "''") + "'",
            "'" + table.replace("'", "''") + "'",
            "'" + function.replace("'", "''") + "'",
            str(trigger_type),
            "true" if deferrable else "false",
            "true" if initially_deferred else "false",
        )) + ")"
        for name, table, function, trigger_type, deferrable, initially_deferred in contract
    )
    return f"""(
                SELECT COUNT(*) = {len(contract)}
                FROM (VALUES {rows}) AS expected(
                    trigger_name, table_name, function_name, trigger_type,
                    expected_deferrable, expected_initially_deferred
                )
                JOIN pg_class AS c ON c.relname = expected.table_name
                JOIN pg_namespace AS n
                  ON n.oid = c.relnamespace AND n.nspname = 'public'
                JOIN pg_trigger AS t
                  ON t.tgrelid = c.oid AND t.tgname = expected.trigger_name
                JOIN pg_proc AS p
                  ON p.oid = t.tgfoid AND p.proname = expected.function_name
                JOIN pg_namespace AS function_namespace
                  ON function_namespace.oid = p.pronamespace
                 AND function_namespace.nspname = 'public'
                WHERE NOT t.tgisinternal
                  AND t.tgenabled = 'O'
                  AND t.tgtype = expected.trigger_type
                  AND t.tgdeferrable = expected.expected_deferrable
                  AND t.tginitdeferred = expected.expected_initially_deferred
                ) AS {alias}"""


def _runtime_permission_errors(engine, expected_user: str) -> list[str]:
    with engine.connect() as connection:
        row = connection.execute(text(f"""
            SELECT
                current_user AS current_user,
                has_schema_privilege(current_user, 'public', 'CREATE') AS schema_create,
                has_table_privilege(current_user, 'ledger_transactions', 'SELECT') AS ledger_select,
                has_table_privilege(current_user, 'ledger_transactions', 'INSERT') AS ledger_insert,
                has_table_privilege(current_user, 'ledger_transactions', 'UPDATE') AS ledger_update,
                has_table_privilege(current_user, 'ledger_transactions', 'DELETE') AS ledger_delete,
                has_table_privilege(current_user, 'ledger_transactions', 'TRUNCATE') AS ledger_truncate,
                has_table_privilege(current_user, 'ledger_transactions', 'REFERENCES') AS ledger_references,
                has_table_privilege(current_user, 'ledger_transactions', 'TRIGGER') AS ledger_trigger,
                has_table_privilege(current_user, 'ledger_entries', 'SELECT') AS entry_select,
                has_table_privilege(current_user, 'ledger_entries', 'INSERT') AS entry_insert,
                has_table_privilege(current_user, 'ledger_entries', 'UPDATE') AS entry_update,
                has_table_privilege(current_user, 'ledger_entries', 'DELETE') AS entry_delete,
                has_table_privilege(current_user, 'ledger_entries', 'TRUNCATE') AS entry_truncate,
                has_table_privilege(current_user, 'ledger_entries', 'REFERENCES') AS entry_references,
                has_table_privilege(current_user, 'ledger_entries', 'TRIGGER') AS entry_trigger,
                has_table_privilege(current_user, 'operator_actions', 'SELECT') AS audit_select,
                has_table_privilege(current_user, 'operator_actions', 'INSERT') AS audit_insert,
                has_table_privilege(current_user, 'operator_actions', 'UPDATE') AS audit_update,
                has_table_privilege(current_user, 'operator_actions', 'DELETE') AS audit_delete,
                has_table_privilege(current_user, 'operator_actions', 'TRUNCATE') AS audit_truncate,
                has_table_privilege(current_user, 'operator_actions', 'REFERENCES') AS audit_references,
                has_table_privilege(current_user, 'operator_actions', 'TRIGGER') AS audit_trigger,
                {_trigger_guard_sql(LEDGER_TRIGGER_CONTRACT, 'ledger_guards')},
                {_trigger_guard_sql(AUDIT_TRIGGER_CONTRACT, 'audit_guards')},
                (
                    SELECT COUNT(*) = 3
                    FROM pg_proc AS p
                    JOIN pg_namespace AS n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'public'
                      AND p.proname IN (
                          'kunlun_reject_ledger_mutation',
                          'kunlun_check_ledger_transaction_balance',
                          'kunlun_reject_operator_action_mutation'
                      )
                      AND p.proconfig = ARRAY['search_path=pg_catalog, public']::text[]
                      AND NOT p.prosecdef
                      AND NOT p.proleakproof
                      AND pg_get_userbyid(p.proowner) = 'kunlun_migrator'
                      AND NOT has_function_privilege(current_user, p.oid, 'EXECUTE')
                ) AS function_search_paths,
                (
                    SELECT NOT rolsuper
                       AND NOT rolcreatedb
                       AND NOT rolcreaterole
                       AND NOT rolinherit
                       AND NOT rolreplication
                       AND NOT rolbypassrls
                    FROM pg_roles
                    WHERE rolname = current_user
                ) AS runtime_role_safe,
                NOT EXISTS (
                    SELECT 1
                    FROM pg_auth_members AS membership
                    JOIN pg_roles AS member ON member.oid = membership.member
                    JOIN pg_roles AS role ON role.oid = membership.roleid
                    WHERE member.rolname = current_user OR role.rolname = current_user
                ) AS runtime_memberships_safe,
                NOT EXISTS (
                    SELECT 1 FROM pg_auth_members AS membership
                    JOIN pg_roles AS member ON member.oid = membership.member
                    JOIN pg_roles AS role ON role.oid = membership.roleid
                    WHERE member.rolname IN ('kunlun_runtime', 'kunlun_migrator', 'kunlun_vault_executor')
                       OR role.rolname IN ('kunlun_runtime', 'kunlun_migrator', 'kunlun_vault_executor')
                ) AS kunlun_role_memberships_safe,
                has_table_privilege(current_user, 'alembic_version', 'SELECT') AS version_select,
                has_table_privilege(current_user, 'alembic_version', 'INSERT') AS version_insert,
                has_table_privilege(current_user, 'alembic_version', 'UPDATE') AS version_update,
                has_table_privilege(current_user, 'alembic_version', 'DELETE') AS version_delete,
                has_table_privilege(current_user, 'alembic_version', 'TRUNCATE') AS version_truncate,
                has_table_privilege(current_user, 'alembic_version', 'REFERENCES') AS version_references,
                has_table_privilege(current_user, 'alembic_version', 'TRIGGER') AS version_trigger
        """)).mappings().one()
    errors: list[str] = []
    if row["current_user"] != expected_user:
        errors.append("数据库实际 runtime 角色与连接 URL 不一致")
    if not all(row[key] for key in (
        "ledger_select", "ledger_insert", "entry_select", "entry_insert",
        "audit_select", "audit_insert", "ledger_guards", "audit_guards",
        "function_search_paths", "runtime_role_safe", "runtime_memberships_safe", "kunlun_role_memberships_safe",
        "version_select",
    )):
        errors.append("runtime 缺少必要的账本/审计追加、守卫或 schema 读取权限")
    if any(row[key] for key in (
        "schema_create",
        "ledger_update", "ledger_delete", "ledger_truncate", "ledger_references", "ledger_trigger",
        "entry_update", "entry_delete", "entry_truncate", "entry_references", "entry_trigger",
        "audit_update", "audit_delete", "audit_truncate", "audit_references", "audit_trigger",
        "version_insert", "version_update", "version_delete", "version_truncate",
        "version_references", "version_trigger",
    )):
        errors.append("runtime 仍持有 schema、历史账本、运维审计或 Alembic 版本修改权限")
    return errors


def _supabase_rls_errors(engine) -> list[str]:
    """Verify that Supabase Data API roles cannot reach Kunlun tables."""
    protected_tables = (*KUNLUN_BUSINESS_TABLES, "alembic_version")
    append_only = {"ledger_transactions", "ledger_entries", "operator_actions"}
    credential_metadata = {"provider_connections", "credential_action_audits"}
    ordinary_tables = tuple(
        table for table in KUNLUN_BUSINESS_TABLES if table not in append_only | credential_metadata
    )
    table_literals = ", ".join(
        "'" + table.replace("'", "''") + "'" for table in protected_tables
    )
    ordinary_literals = ", ".join(
        "'" + table.replace("'", "''") + "'" for table in ordinary_tables
    )
    expected_policy_count = len(ordinary_tables) + len(append_only) * 2 + 1 + len(credential_metadata)
    with engine.connect() as connection:
        row = connection.execute(text(f"""
            SELECT
                (
                    SELECT COUNT(*) = {len(protected_tables)}
                    FROM pg_class AS c
                    JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relkind IN ('r', 'p')
                      AND c.relname IN ({table_literals})
                      AND c.relrowsecurity
                      AND pg_get_userbyid(c.relowner) = 'kunlun_migrator'
                ) AS rls_and_owner_contract,
                (
                    SELECT COUNT(*) = {expected_policy_count}
                       AND COUNT(*) FILTER (WHERE
                            permissive = 'PERMISSIVE'
                            AND roles = ARRAY['kunlun_runtime']::name[]
                            AND (
                                (
                                    tablename IN ({ordinary_literals})
                                    AND policyname = 'kunlun_runtime_all_' || tablename
                                    AND cmd = 'ALL'
                                    AND qual = 'true'
                                    AND with_check = 'true'
                                )
                                OR (
                                    tablename IN ('provider_connections', 'credential_action_audits')
                                    AND policyname = 'kunlun_runtime_select_' || tablename
                                    AND cmd = 'SELECT'
                                    AND qual = 'true'
                                    AND with_check IS NULL
                                )
                                OR (
                                    tablename IN ('ledger_transactions', 'ledger_entries', 'operator_actions')
                                    AND policyname = 'kunlun_runtime_select_' || tablename
                                    AND cmd = 'SELECT'
                                    AND qual = 'true'
                                    AND with_check IS NULL
                                )
                                OR (
                                    tablename IN ('ledger_transactions', 'ledger_entries', 'operator_actions')
                                    AND policyname = 'kunlun_runtime_insert_' || tablename
                                    AND cmd = 'INSERT'
                                    AND qual IS NULL
                                    AND with_check = 'true'
                                )
                                OR (
                                    tablename = 'alembic_version'
                                    AND policyname = 'kunlun_runtime_select_alembic_version'
                                    AND cmd = 'SELECT'
                                    AND qual = 'true'
                                    AND with_check IS NULL
                                )
                            )
                       ) = {expected_policy_count}
                    FROM pg_policies
                    WHERE schemaname = 'public'
                      AND tablename IN ({table_literals})
                ) AS policy_contract,
                (
                    SELECT COUNT(*) = {len(ordinary_tables)}
                       AND bool_and(
                            has_table_privilege(
                                current_user, format('public.%I', table_name), 'SELECT'
                            )
                            AND has_table_privilege(
                                current_user, format('public.%I', table_name), 'INSERT'
                            )
                            AND has_table_privilege(
                                current_user, format('public.%I', table_name), 'UPDATE'
                            )
                            AND has_table_privilege(
                                current_user, format('public.%I', table_name), 'DELETE'
                            )
                            AND NOT has_table_privilege(
                                current_user, format('public.%I', table_name), 'TRUNCATE'
                            )
                            AND NOT has_table_privilege(
                                current_user, format('public.%I', table_name), 'REFERENCES'
                            )
                            AND NOT has_table_privilege(
                                current_user, format('public.%I', table_name), 'TRIGGER'
                            )
                       )
                    FROM unnest(ARRAY[{ordinary_literals}]::text[]) AS table_name
                ) AS ordinary_privileges,
                has_table_privilege(current_user, 'public.provider_connections', 'SELECT')
                AND has_table_privilege(current_user, 'public.credential_action_audits', 'SELECT')
                AND NOT has_table_privilege(current_user, 'public.provider_connections', 'INSERT, UPDATE, DELETE, TRUNCATE')
                AND NOT has_table_privilege(current_user, 'public.credential_action_audits', 'INSERT, UPDATE, DELETE, TRUNCATE')
                AS credential_metadata_read_only,
                NOT EXISTS (
                    SELECT 1
                    FROM pg_class AS c
                    JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    CROSS JOIN LATERAL aclexplode(
                        COALESCE(c.relacl, acldefault('r', c.relowner))
                    ) AS acl
                    LEFT JOIN pg_roles AS grantee ON grantee.oid = acl.grantee
                    WHERE n.nspname = 'public'
                      AND c.relkind IN ('r', 'p')
                      AND c.relname IN ({table_literals})
                      AND (
                          acl.grantee = 0
                          OR grantee.rolname IN ('anon', 'authenticated')
                      )
                ) AS api_grants_locked,
                NOT EXISTS (
                    SELECT 1
                    FROM pg_policies
                    WHERE schemaname = 'public'
                      AND tablename IN ({table_literals})
                      AND roles && ARRAY['public', 'anon', 'authenticated']::name[]
                ) AS api_policies_locked,
                NOT EXISTS (
                    SELECT 1
                    FROM pg_roles AS api_role
                    CROSS JOIN pg_class AS c
                    JOIN pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE api_role.rolname IN ('anon', 'authenticated')
                      AND n.nspname = 'public'
                      AND c.relkind IN ('r', 'p')
                      AND c.relname IN ({table_literals})
                      AND (
                        has_table_privilege(
                          api_role.rolname, c.oid,
                          'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'
                        )
                        OR has_any_column_privilege(
                          api_role.rolname, c.oid,
                          'SELECT, INSERT, UPDATE, REFERENCES'
                        )
                      )
                ) AS api_effective_privileges_locked
        """)).mappings().one()
    errors: list[str] = []
    if not row["rls_and_owner_contract"]:
        errors.append("Kunlun 业务表或 Alembic 版本表的 RLS/owner 契约不完整")
    if not row["policy_contract"]:
        errors.append("Kunlun runtime RLS 策略与预期契约不一致")
    if not row["ordinary_privileges"]:
        errors.append("Kunlun runtime 普通业务表权限与最小权限契约不一致")
    if not row["credential_metadata_read_only"]:
        errors.append("runtime credential metadata 必须只读")
    if (
        not row["api_grants_locked"]
        or not row["api_policies_locked"]
        or not row["api_effective_privileges_locked"]
    ):
        errors.append("Supabase anon/authenticated 仍可访问 Kunlun 业务表")
    return errors


def _migrator_database_errors(migrator_url: str, runtime_user: str) -> list[str]:
    if not _production_database_url_is_safe(migrator_url):
        return [
            "KUNLUN_MIGRATOR_DATABASE_URL 必须使用 verify-full 与可读的绝对 sslrootcert"
        ]
    migrator_user = _database_user(migrator_url)
    if migrator_user != "kunlun_migrator" or migrator_user == runtime_user:
        return ["KUNLUN_MIGRATOR_DATABASE_URL 必须使用独立的 kunlun_migrator 角色"]
    return []


def _installation_marker_errors(
    runtime_engine,
    migrator_engine,
    executor_engine,
    runtime_user: str,
    migrator_user: str,
    executor_user: str,
) -> list[str]:
    """Prove all privileged URLs terminate at the same Kunlun installation."""
    connections = (
        ("runtime", runtime_user, runtime_engine),
        ("migrator", migrator_user, migrator_engine),
        ("Vault executor", executor_user, executor_engine),
    )
    markers: list[str] = []
    errors: list[str] = []
    for label, expected_user, engine in connections:
        with engine.connect() as connection:
            row = connection.execute(text("""
                SELECT current_user AS current_user,
                       public.kunlun_installation_id()::text AS installation_id,
                       (
                         SELECT count(*) = 1 AND bool_and(
                           p.prosecdef
                           AND NOT p.proleakproof
                           AND p.proconfig = ARRAY['search_path=pg_catalog']::text[]
                           AND p.prorettype = 'uuid'::regtype
                           AND pg_get_userbyid(p.proowner) = 'kunlun_migrator'
                           AND has_function_privilege('kunlun_runtime', p.oid, 'EXECUTE')
                           AND has_function_privilege('kunlun_migrator', p.oid, 'EXECUTE')
                           AND has_function_privilege('kunlun_vault_executor', p.oid, 'EXECUTE')
                           AND NOT has_function_privilege('anon', p.oid, 'EXECUTE')
                           AND NOT has_function_privilege('authenticated', p.oid, 'EXECUTE')
                         )
                         FROM pg_proc AS p
                         JOIN pg_namespace AS n ON n.oid = p.pronamespace
                         WHERE n.nspname = 'public'
                           AND p.proname = 'kunlun_installation_id'
                           AND pg_get_function_identity_arguments(p.oid) = ''
                       ) AS marker_contract
            """)).mappings().one()
        if row["current_user"] != expected_user:
            errors.append(f"{label} 数据库实际角色与连接 URL 不一致")
        if not row["marker_contract"]:
            errors.append(f"{label} 数据库安装标记函数的 owner、search_path 或 ACL 不安全")
        try:
            markers.append(str(UUID(str(row["installation_id"]))))
        except (TypeError, ValueError, AttributeError):
            errors.append(f"{label} 缺少有效的数据库安装标记")
    if len(markers) == len(connections) and not all(
        secrets.compare_digest(markers[0], marker) for marker in markers[1:]
    ):
        errors.append("runtime、migrator 与 Vault executor 未连接同一数据库安装")
    return errors


def _vault_contract_errors(engine, executor_engine, runtime_user: str, executor_user: str) -> list[str]:
    """Check runtime/executor ACLs without reading or writing a secret."""
    with engine.connect() as connection:
        row = connection.execute(text("""
            WITH private_functions AS (
                SELECT p.*
                FROM pg_proc AS p
                JOIN pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = 'kunlun_private'
                  AND (
                    (p.proname = 'credential_put_v2'
                     AND pg_get_function_identity_arguments(p.oid) =
                         'p_user_id uuid, p_provider text, p_label text, p_secret text')
                    OR (p.proname = 'credential_resolve_v2'
                        AND pg_get_function_identity_arguments(p.oid) =
                            'p_user_id uuid, p_connection_id uuid, p_provider text, p_credential_version integer')
                    OR (p.proname = 'credential_revoke_v2'
                        AND pg_get_function_identity_arguments(p.oid) =
                            'p_user_id uuid, p_provider text')
                    OR (p.proname = 'credential_probe_v2'
                        AND pg_get_function_identity_arguments(p.oid) = '')
                  )
            ), vault_functions AS (
                SELECT p.oid, p.proname, pg_get_function_identity_arguments(p.oid) AS identity_arguments
                FROM pg_proc AS p
                JOIN pg_namespace AS n ON n.oid = p.pronamespace
                WHERE n.nspname = 'vault'
            ), protected_relations AS (
                SELECT c.oid, n.nspname, c.relname
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE (n.nspname, c.relname) IN (
                    ('vault', 'secrets'),
                    ('vault', 'decrypted_secrets'),
                    ('kunlun_private', 'provider_credential_bindings'),
                    ('kunlun_private', 'installation_marker')
                )
            )
            SELECT
                (SELECT count(*) = 4 FROM private_functions) AS functions_present,
                (
                    SELECT count(*) = 4
                    FROM private_functions AS p
                    WHERE p.prosecdef
                    AND p.proconfig = ARRAY['search_path=pg_catalog']::text[]
                    AND pg_get_userbyid(p.proowner) = 'kunlun_migrator'
                    AND NOT has_function_privilege(current_user, p.oid, 'EXECUTE')
                ) AS functions_secure,
                has_schema_privilege(
                    'kunlun_migrator',
                    (SELECT oid FROM pg_namespace WHERE nspname = 'vault'),
                    'USAGE'
                ) AND (
                    SELECT count(*) = 1 AND bool_and(
                        has_function_privilege('kunlun_migrator', oid, 'EXECUTE')
                    )
                    FROM vault_functions
                    WHERE proname = 'create_secret'
                      AND identity_arguments = 'new_secret text, new_name text, new_description text'
                ) AND (
                    SELECT count(*) = 2 AND bool_and(
                        has_table_privilege('kunlun_migrator', oid, 'SELECT')
                        AND (relname <> 'secrets'
                             OR has_table_privilege('kunlun_migrator', oid, 'DELETE'))
                    )
                    FROM protected_relations WHERE nspname = 'vault'
                ) AS definer_vault_privileges,
                NOT has_schema_privilege(current_user, 'kunlun_private', 'USAGE')
                AND NOT has_schema_privilege(current_user, 'vault', 'USAGE')
                AND (
                    SELECT count(*) = 4 AND bool_and(
                        NOT has_table_privilege(current_user, oid, 'SELECT')
                        AND NOT has_table_privilege(current_user, oid, 'INSERT')
                        AND NOT has_table_privilege(current_user, oid, 'UPDATE')
                        AND NOT has_table_privilege(current_user, oid, 'DELETE')
                        AND NOT has_table_privilege(current_user, oid, 'TRUNCATE')
                        AND NOT has_table_privilege(current_user, oid, 'REFERENCES')
                        AND NOT has_table_privilege(current_user, oid, 'TRIGGER')
                        AND NOT has_any_column_privilege(
                            current_user, oid, 'SELECT, INSERT, UPDATE, REFERENCES'
                        )
                    ) FROM protected_relations
                ) AND (
                    SELECT count(*) > 0 AND bool_and(
                        NOT has_function_privilege(current_user, oid, 'EXECUTE')
                    ) FROM vault_functions
                ) AS caller_vault_locked,
                (SELECT pg_get_userbyid(nspowner) = 'kunlun_migrator'
                 FROM pg_namespace WHERE nspname = 'kunlun_private') AS bootstrap_schema
        """)).mappings().one()
    errors: list[str] = []
    if not row["bootstrap_schema"]:
        errors.append("kunlun_private bootstrap schema 不存在，请先执行管理员 bootstrap")
    if not row["functions_present"] or not row["functions_secure"]:
        errors.append("Supabase Vault private 函数签名、owner、SECURITY DEFINER、search_path 或 EXECUTE 契约不完整")
    if not row["definer_vault_privileges"]:
        errors.append("kunlun_migrator 缺少 Supabase Vault definer 最小权限")
    if not row["caller_vault_locked"]:
        errors.append("runtime 仍持有 private/Vault 或 credential function 直接权限")
    with executor_engine.connect() as connection:
        executor = connection.execute(text("""
            WITH private_functions AS (
              SELECT p.oid
              FROM pg_proc AS p
              JOIN pg_namespace AS n ON n.oid = p.pronamespace
              WHERE n.nspname = 'kunlun_private'
                AND p.proname IN (
                  'credential_put_v2', 'credential_resolve_v2',
                  'credential_revoke_v2', 'credential_probe_v2'
                )
            ), protected_functions AS (
              SELECT p.oid, n.nspname
              FROM pg_proc AS p
              JOIN pg_namespace AS n ON n.oid = p.pronamespace
              WHERE n.nspname IN ('kunlun_private', 'vault')
            ), protected_relations AS (
              SELECT c.oid, n.nspname
              FROM pg_class AS c
              JOIN pg_namespace AS n ON n.oid = c.relnamespace
              WHERE (n.nspname, c.relname) IN (
                ('public', 'provider_connections'),
                ('public', 'credential_action_audits'),
                ('kunlun_private', 'provider_credential_bindings'),
                ('kunlun_private', 'installation_marker'),
                ('vault', 'secrets'),
                ('vault', 'decrypted_secrets')
              )
            )
            SELECT current_user = :executor_user AS exact_executor,
              has_schema_privilege(current_user, 'kunlun_private', 'USAGE') AS private_usage,
              (SELECT NOT rolsuper
                    AND NOT rolcreatedb
                    AND NOT rolcreaterole
                    AND NOT rolinherit
                    AND NOT rolreplication
                    AND NOT rolbypassrls
               FROM pg_roles WHERE rolname = current_user) AS executor_role_safe,
                NOT EXISTS (
                    SELECT 1
                    FROM pg_auth_members AS membership
                    JOIN pg_roles AS member ON member.oid = membership.member
                    JOIN pg_roles AS role ON role.oid = membership.roleid
                    WHERE member.rolname = current_user OR role.rolname = current_user
              ) AS executor_memberships_safe,
              (SELECT count(*) = 6 AND bool_and(
                 NOT has_table_privilege(current_user, oid, 'SELECT')
                 AND NOT has_table_privilege(current_user, oid, 'INSERT')
                 AND NOT has_table_privilege(current_user, oid, 'UPDATE')
                 AND NOT has_table_privilege(current_user, oid, 'DELETE')
                 AND NOT has_table_privilege(current_user, oid, 'TRUNCATE')
                 AND NOT has_table_privilege(current_user, oid, 'REFERENCES')
                 AND NOT has_table_privilege(current_user, oid, 'TRIGGER')
                 AND NOT has_any_column_privilege(
                   current_user, oid, 'SELECT, INSERT, UPDATE, REFERENCES'
                 )
               ) FROM protected_relations) AS no_direct_reads,
              (SELECT count(*) = 4 AND bool_and(
                 has_function_privilege(current_user, oid, 'EXECUTE')
               ) FROM private_functions) AS functions_only,
              NOT has_table_privilege('anon', 'public.provider_connections', 'SELECT')
              AND NOT has_table_privilege('authenticated', 'public.provider_connections', 'SELECT')
              AND NOT EXISTS (
                SELECT 1
                FROM pg_roles AS api_role
                CROSS JOIN protected_relations AS relation
                WHERE api_role.rolname IN ('anon', 'authenticated')
                  AND (
                    (
                      relation.nspname IN ('kunlun_private', 'vault')
                      AND has_schema_privilege(api_role.rolname, relation.nspname, 'USAGE')
                    )
                    OR has_table_privilege(
                      api_role.rolname, relation.oid,
                      'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'
                    )
                    OR has_any_column_privilege(
                      api_role.rolname, relation.oid,
                      'SELECT, INSERT, UPDATE, REFERENCES'
                    )
                  )
              )
              AND NOT EXISTS (
                SELECT 1
                FROM pg_roles AS api_role
                CROSS JOIN protected_functions AS function
                WHERE api_role.rolname IN ('anon', 'authenticated')
                  AND has_function_privilege(api_role.rolname, function.oid, 'EXECUTE')
              )
              AS api_effective_access_denied
        """), {"executor_user": executor_user}).mappings().one()
    if not all(executor.values()):
        errors.append("Vault executor 角色或仅函数访问 ACL 不完整")
    return errors


def main(*, config_only: bool = False, require_managed_launch: bool = False) -> int:
    errors: list[str] = []
    try:
        # The production compose file injects KUNLUN_DATABASE_URL separately
        # into the API container. When this command runs on the host against
        # the example env file, use the explicit runtime URL instead of the
        # compose placeholder.
        runtime_override = os.getenv("KUNLUN_RUNTIME_DATABASE_URL", "")
        database_override = runtime_override if runtime_override and os.getenv("KUNLUN_DATABASE_URL") == "overridden_by_compose" else None
        settings = Settings.from_env(database_url=database_override)
    except (RuntimeError, ValueError):
        print("生产预检失败：应用配置未通过安全校验（敏感值已隐藏）")
        return 1
    if not settings.is_production:
        errors.append("KUNLUN_ENV=production")
    if require_managed_launch:
        if settings.gateway_mode != "managed_gateway":
            errors.append("商业发布检查要求 KUNLUN_GATEWAY_MODE=managed_gateway")
        for field, enabled in (
            ("KUNLUN_PUBLIC_SIGNUP", settings.public_signup),
            ("KUNLUN_LIVE_PAYMENTS", settings.live_payments),
            ("KUNLUN_LIVE_UPSTREAM", settings.live_upstream),
        ):
            if not enabled:
                errors.append(f"商业发布检查要求 {field}=true；仅在对应授权和验收完成后启用")
    if settings.gateway_mode in {"byok", "managed_gateway"} and (
        not settings.identity_token_pepper_persisted or len(settings.identity_token_pepper) < 32
    ):
        errors.append("客户开通需要持久化的 KUNLUN_IDENTITY_TOKEN_PEPPER（至少 32 字符）")

    runtime_user = _database_user(settings.database_url)
    migrator_url = os.getenv("KUNLUN_MIGRATOR_DATABASE_URL", "")
    migrator_user = _database_user(migrator_url)
    executor_url = settings.vault_executor_database_url
    if not runtime_user or runtime_user in {"postgres", "kunlun_migrator"}:
        errors.append("KUNLUN_DATABASE_URL 必须使用非 owner 的 runtime 角色")
    errors.extend(_migrator_database_errors(migrator_url, runtime_user))
    executor_user = _database_user(executor_url)
    errors.extend(_database_credential_errors(
        settings.database_url,
        migrator_url,
        executor_url if settings.gateway_mode in {"byok", "managed_gateway"} else "",
    ))
    if settings.gateway_mode in {"byok", "managed_gateway"}:
        errors.extend(_database_target_errors(settings.database_url, migrator_url, executor_url))
    if settings.gateway_mode in {"byok", "managed_gateway"} and (not _production_database_url_is_safe(executor_url) or executor_user != "kunlun_vault_executor" or executor_user == runtime_user):
        errors.append("Vault executor URL 必须是独立的 kunlun_vault_executor verify-full 连接")

    if not errors and not config_only:
        engine = build_engine(settings.database_url)
        try:
            assert_schema_revision(engine, SCHEMA_HEAD)
            errors.extend(_runtime_permission_errors(engine, runtime_user))
            errors.extend(_supabase_rls_errors(engine))
            if settings.gateway_mode in {"byok", "managed_gateway"}:
                executor_engine = build_engine(executor_url)
                try:
                    migrator_engine = build_engine(migrator_url)
                    try:
                        errors.extend(_vault_contract_errors(engine, executor_engine, runtime_user, executor_user))
                        if settings.gateway_mode == "managed_gateway":
                            from app.services.platform_credentials import platform_contract_errors
                            errors.extend(platform_contract_errors(engine, executor_engine))
                        errors.extend(_installation_marker_errors(
                            engine,
                            migrator_engine,
                            executor_engine,
                            runtime_user,
                            migrator_user,
                            executor_user,
                        ))
                    finally:
                        migrator_engine.dispose()
                finally:
                    executor_engine.dispose()
        except RuntimeError:
            errors.append(f"数据库必须精确位于 Alembic head {SCHEMA_HEAD}")
        except Exception:
            errors.append("无法验证 runtime 数据库权限边界")
        finally:
            engine.dispose()

    if errors:
        print("生产预检失败：")
        for item in errors:
            print(f"- {item}")
        return 1
    if config_only:
        print("配置静态检查通过：仅核验配置与数据库 URL/角色目标，未连接数据库或外部服务。")
        print("未验证 schema、权限、Vault 凭据、正式支付 SDK、供应商授权或真实交易；不等于商业上线。")
        return 0
    print("生产技术预检通过。")
    if settings.gateway_mode == "managed_gateway":
        print("仍需人工验收：TLS/WAF、备份恢复、供应商商业用途依据、真实小额支付/调用/退款及成本对账；技术预检不等于商业上线。")
    else:
        print("仍需人工验收：TLS/WAF、备份恢复、客户开通、真实 BYOK 调用/成本对账及合规材料；不提供充值收款。")
    return 0


def cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-only", action="store_true", help="Only validate local configuration; never connect to a database or service")
    parser.add_argument("--require-managed-launch", action="store_true", help="Require managed mode and the complete signup/payment/upstream feature profile")
    args = parser.parse_args(argv)
    return main(config_only=args.config_only, require_managed_launch=args.require_managed_launch)


if __name__ == "__main__":
    raise SystemExit(cli())
