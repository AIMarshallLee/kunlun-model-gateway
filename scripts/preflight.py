#!/usr/bin/env python3
"""Fail-closed production configuration, role and schema preflight.

This command validates facts the process can observe. It deliberately does not
claim that DNS, inbox delivery, merchant onboarding, filings, WAF or a real
payment/refund has been verified.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, _production_database_url_is_safe
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


def _database_user(url: str) -> str:
    normalized = url.replace("postgresql+psycopg://", "postgresql://", 1)
    parsed = urlparse(normalized)
    username = parsed.username or ""
    hostname = (parsed.hostname or "").casefold()
    if hostname.endswith(".pooler.supabase.com"):
        role, separator, project_ref = username.rpartition(".")
        if separator and role and re.fullmatch(r"[a-z0-9]{20}", project_ref):
            return role
    return username


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
                    WHERE member.rolname = current_user
                ) AS runtime_memberships_safe,
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
        "function_search_paths", "runtime_role_safe", "runtime_memberships_safe",
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
    ordinary_tables = tuple(
        table for table in KUNLUN_BUSINESS_TABLES if table not in append_only
    )
    table_literals = ", ".join(
        "'" + table.replace("'", "''") + "'" for table in protected_tables
    )
    ordinary_literals = ", ".join(
        "'" + table.replace("'", "''") + "'" for table in ordinary_tables
    )
    expected_policy_count = len(ordinary_tables) + len(append_only) * 2 + 1
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
                ) AS rls_enabled,
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
                ) AS api_policies_locked
        """)).mappings().one()
    errors: list[str] = []
    if not row["rls_enabled"]:
        errors.append("Kunlun 业务表或 Alembic 版本表未完整启用 RLS")
    if not row["policy_contract"]:
        errors.append("Kunlun runtime RLS 策略与预期契约不一致")
    if not row["ordinary_privileges"]:
        errors.append("Kunlun runtime 普通业务表权限与最小权限契约不一致")
    if not row["api_grants_locked"] or not row["api_policies_locked"]:
        errors.append("Supabase anon/authenticated 仍可访问 Kunlun 业务表")
    return errors


def _migrator_database_errors(migrator_url: str, runtime_user: str) -> list[str]:
    if not _production_database_url_is_safe(migrator_url):
        return [
            "KUNLUN_MIGRATOR_DATABASE_URL 必须使用 verify-full 与可读的绝对 sslrootcert"
        ]
    migrator_user = _database_user(migrator_url)
    if not migrator_user or migrator_user == runtime_user:
        return ["migrator 与 runtime 数据库角色必须不同"]
    return []


def main() -> int:
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

    runtime_user = _database_user(settings.database_url)
    migrator_url = os.getenv("KUNLUN_MIGRATOR_DATABASE_URL", "")
    if not runtime_user or runtime_user in {"postgres", "kunlun_migrator"}:
        errors.append("KUNLUN_DATABASE_URL 必须使用非 owner 的 runtime 角色")
    errors.extend(_migrator_database_errors(migrator_url, runtime_user))

    if not errors:
        engine = build_engine(settings.database_url)
        try:
            assert_schema_revision(engine, SCHEMA_HEAD)
            errors.extend(_runtime_permission_errors(engine, runtime_user))
            errors.extend(_supabase_rls_errors(engine))
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
    print("生产技术预检通过。")
    print("仍需人工验收：TLS/WAF、备份恢复、邮件送达、内容审核、正式支付小额付款/退款/对账及合规材料。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
