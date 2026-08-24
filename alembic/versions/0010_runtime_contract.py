"""Remove direct runtime execution of internal trigger functions."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade


revision: str = "0010_runtime_contract"
down_revision: Union[str, None] = "0009_pg_function_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FUNCTIONS = (
    "public.kunlun_reject_ledger_mutation()",
    "public.kunlun_check_ledger_transaction_balance()",
    "public.kunlun_reject_operator_action_mutation()",
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    op.execute(sa.text("""
        DO $$
        BEGIN
            IF current_user <> 'kunlun_migrator' THEN
                RAISE EXCEPTION '0010 must run as kunlun_migrator (got %)', current_user;
            END IF;
        END;
        $$;
    """))
    functions = ", ".join(FUNCTIONS)
    op.execute(sa.text(
        f"REVOKE ALL PRIVILEGES ON FUNCTION {functions} FROM PUBLIC, kunlun_runtime"
    ))
    for api_role in ("anon", "authenticated"):
        op.execute(sa.text(f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{api_role}') THEN
                    EXECUTE 'REVOKE ALL PRIVILEGES ON FUNCTION {functions} FROM {api_role}';
                END IF;
            END;
            $$;
        """))


def downgrade() -> None:
    allow = context.config.attributes.get("allow_destructive_downgrade", False)
    environment = context.config.attributes.get("environment", "development")
    assert_safe_downgrade(environment, bool(allow))
    # Fail closed: do not restore PUBLIC/Data API execution on internal trigger
    # functions.  Trigger execution itself does not require caller EXECUTE.
    return
