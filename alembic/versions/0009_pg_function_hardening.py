"""Harden PostgreSQL functions and add covering foreign-key indexes."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade


revision: str = "0009_pg_function_hardening"
down_revision: Union[str, None] = "0008_supabase_rls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FUNCTIONS = (
    "kunlun_reject_ledger_mutation",
    "kunlun_check_ledger_transaction_balance",
    "kunlun_reject_operator_action_mutation",
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        op.execute(sa.text("""
            DO $$
            BEGIN
                IF current_user <> 'kunlun_migrator' THEN
                    RAISE EXCEPTION '0009 must run as kunlun_migrator (got %)', current_user;
                END IF;
            END;
            $$;
        """))
        for function in FUNCTIONS:
            op.execute(sa.text(
                f"ALTER FUNCTION public.{function}() SET search_path = pg_catalog, public"
            ))
    op.create_index(
        "ix_ledger_entries_transaction_user",
        "ledger_entries",
        ["transaction_id", "user_id"],
    )
    op.create_index(
        "ix_model_requests_budget_id",
        "model_requests",
        ["budget_id"],
    )


def downgrade() -> None:
    allow = context.config.attributes.get("allow_destructive_downgrade", False)
    environment = context.config.attributes.get("environment", "development")
    assert_safe_downgrade(environment, bool(allow))
    op.drop_index("ix_model_requests_budget_id", table_name="model_requests")
    op.drop_index("ix_ledger_entries_transaction_user", table_name="ledger_entries")
    if _is_postgres():
        for function in FUNCTIONS:
            op.execute(sa.text(f"ALTER FUNCTION public.{function}() RESET search_path"))
