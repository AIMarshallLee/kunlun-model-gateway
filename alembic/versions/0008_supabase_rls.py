"""Lock down the public schema for a Supabase deployment.

The migration must be run by the owner role ``kunlun_migrator``.  The API uses
the separate ``kunlun_runtime`` role; RLS policies and table grants are both
kept deliberately explicit so a Supabase ``anon`` or ``authenticated`` JWT
can never inherit access through PUBLIC/default privileges.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade


revision: str = "0008_supabase_rls"
down_revision: Union[str, None] = "0007_payment_reconcile_claim"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BUSINESS_TABLES = (
    "users", "access_sessions", "api_keys", "budgets",
    "email_verification_tokens", "password_reset_tokens",
    "ledger_transactions", "payment_orders", "wallets", "ledger_entries",
    "model_requests", "payment_webhook_events", "rate_limit_counters",
    "operator_actions", "provider_attempts", "payment_refunds", "safety_audits",
    "model_prices", "auth_rate_limit_counters", "outbox_events",
)
APPEND_ONLY_TABLES = ("ledger_transactions", "ledger_entries", "operator_actions")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _identifiers(tables: tuple[str, ...]) -> str:
    return ", ".join(f"public.{table}" for table in tables)


def _policy_name(table: str, suffix: str = "all") -> str:
    return f"kunlun_runtime_{suffix}_{table}"


def upgrade() -> None:
    if not _is_postgres():
        return

    # Only the owner may ALTER tables and create policies.  A fresh Supabase
    # schema must therefore run Alembic with this dedicated migrator role.
    op.execute(sa.text("""
        DO $$
        BEGIN
            IF current_user <> 'kunlun_migrator' THEN
                RAISE EXCEPTION '0008 must run as kunlun_migrator (got %)', current_user;
            END IF;
        END;
        $$;
    """))

    tables = _identifiers(BUSINESS_TABLES)
    # Remove privileges inherited from PUBLIC and, when present, from the
    # Supabase Data API roles.  The conditional blocks keep this migration
    # usable in a plain PostgreSQL clean-room environment as well.
    protected_tables = f"{tables}, public.alembic_version"
    op.execute(sa.text(f"REVOKE ALL PRIVILEGES ON TABLE {protected_tables} FROM PUBLIC"))
    for api_role in ("anon", "authenticated"):
        op.execute(sa.text(f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{api_role}') THEN
                    EXECUTE 'REVOKE ALL PRIVILEGES ON TABLE {protected_tables} FROM {api_role}';
                END IF;
            END;
            $$;
        """))

    # Runtime has CRUD on ordinary application tables, and only append/read on
    # immutable journals.  Version metadata is read-only to the API role.
    ordinary = tuple(table for table in BUSINESS_TABLES if table not in APPEND_ONLY_TABLES)
    if ordinary:
        op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {_identifiers(ordinary)} TO kunlun_runtime"))
    op.execute(sa.text(f"GRANT SELECT, INSERT ON TABLE {_identifiers(APPEND_ONLY_TABLES)} TO kunlun_runtime"))
    op.execute(sa.text("GRANT SELECT ON TABLE public.alembic_version TO kunlun_runtime"))
    op.execute(sa.text(f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE {_identifiers(APPEND_ONLY_TABLES)} FROM kunlun_runtime"))
    op.execute(sa.text("REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE public.alembic_version FROM kunlun_runtime"))

    for table in BUSINESS_TABLES:
        qualified = f"public.{table}"
        op.execute(sa.text(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY"))
        if table in APPEND_ONLY_TABLES:
            op.execute(sa.text(f"CREATE POLICY {_policy_name(table, 'select')} ON {qualified} FOR SELECT TO kunlun_runtime USING (true)"))
            op.execute(sa.text(f"CREATE POLICY {_policy_name(table, 'insert')} ON {qualified} FOR INSERT TO kunlun_runtime WITH CHECK (true)"))
        else:
            op.execute(sa.text(f"CREATE POLICY {_policy_name(table)} ON {qualified} FOR ALL TO kunlun_runtime USING (true) WITH CHECK (true)"))

    op.execute(sa.text("ALTER TABLE public.alembic_version ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("CREATE POLICY kunlun_runtime_select_alembic_version ON public.alembic_version FOR SELECT TO kunlun_runtime USING (true)"))


def downgrade() -> None:
    allow = context.config.attributes.get("allow_destructive_downgrade", False)
    environment = context.config.attributes.get("environment", "development")
    assert_safe_downgrade(environment, bool(allow))
    if not _is_postgres():
        return
    op.execute(sa.text("""
        DO $$
        BEGIN
            IF current_user <> 'kunlun_migrator' THEN
                RAISE EXCEPTION '0008 downgrade must run as kunlun_migrator (got %)', current_user;
            END IF;
        END;
        $$;
    """))
    for table in BUSINESS_TABLES:
        policy_names = (
            (_policy_name(table, "select"), _policy_name(table, "insert"))
            if table in APPEND_ONLY_TABLES else (_policy_name(table),)
        )
        for policy in policy_names:
            op.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON public.{table}"))
        op.execute(sa.text(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("DROP POLICY IF EXISTS kunlun_runtime_select_alembic_version ON public.alembic_version"))
    op.execute(sa.text("ALTER TABLE public.alembic_version DISABLE ROW LEVEL SECURITY"))
