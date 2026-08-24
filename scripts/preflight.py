#!/usr/bin/env python3
"""Fail-closed production configuration, role and schema preflight.

This command validates facts the process can observe. It deliberately does not
claim that DNS, inbox delivery, merchant onboarding, filings, WAF or a real
payment/refund has been verified.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from urllib.parse import urlparse

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.db import build_engine
from app.db_guards import (
    SCHEMA_HEAD,
    assert_schema_revision,
    ledger_trigger_names,
    operator_audit_trigger_names,
)


def _database_user(url: str) -> str:
    normalized = url.replace("postgresql+psycopg://", "postgresql://", 1)
    return urlparse(normalized).username or ""


def _trigger_guard_sql(tables: tuple[str, ...], names: tuple[str, ...], alias: str) -> str:
    """Return a fail-closed PostgreSQL check for a complete active trigger set.

    Trigger names are application constants, not user input. Keeping the
    allow-list here also makes the preflight query auditable and prevents a
    partially migrated or disabled append-only boundary from passing.
    """
    literals = ", ".join("'" + name.replace("'", "''") + "'" for name in names)
    table_literals = ", ".join(
        "to_regclass('public." + table.replace("'", "''") + "')" for table in tables
    )
    return f"""(
                SELECT COUNT(*) = {len(names)}
                   FROM pg_trigger
                  WHERE tgrelid IN ({table_literals})
                    AND NOT tgisinternal
                    AND tgenabled <> 'D'
                    AND tgname IN ({literals})
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
                {_trigger_guard_sql(('ledger_transactions', 'ledger_entries'), ledger_trigger_names(), 'ledger_guards')},
                {_trigger_guard_sql(('operator_actions',), operator_audit_trigger_names(), 'audit_guards')},
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
        "audit_select", "audit_insert", "ledger_guards", "audit_guards", "version_select",
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
    migrator_user = _database_user(migrator_url) if migrator_url else ""
    if not runtime_user or runtime_user in {"postgres", "kunlun_migrator"}:
        errors.append("KUNLUN_DATABASE_URL 必须使用非 owner 的 runtime 角色")
    if not migrator_url.startswith(("postgresql://", "postgresql+psycopg://")):
        errors.append("KUNLUN_MIGRATOR_DATABASE_URL 必须单独配置")
    elif not migrator_user or migrator_user == runtime_user:
        errors.append("migrator 与 runtime 数据库角色必须不同")

    if not errors:
        engine = build_engine(settings.database_url)
        try:
            assert_schema_revision(engine, SCHEMA_HEAD)
            errors.extend(_runtime_permission_errors(engine, runtime_user))
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
