"""Production database invariants shared by startup and migration tooling."""

from __future__ import annotations

from sqlalchemy import Engine, text


SCHEMA_HEAD = "0010_runtime_contract"

# Every application-owned table in the public schema.  Keep this allow-list
# explicit: a Supabase deployment may contain unrelated extension tables that
# must not accidentally receive Kunlun grants or policies.
KUNLUN_BUSINESS_TABLES = (
    "users",
    "access_sessions",
    "api_keys",
    "budgets",
    "email_verification_tokens",
    "password_reset_tokens",
    "ledger_transactions",
    "payment_orders",
    "wallets",
    "ledger_entries",
    "model_requests",
    "payment_webhook_events",
    "rate_limit_counters",
    "operator_actions",
    "provider_attempts",
    "payment_refunds",
    "safety_audits",
    "model_prices",
    "auth_rate_limit_counters",
    "outbox_events",
)

IMMUTABLE_APPEND_TABLES = ("ledger_transactions", "ledger_entries", "operator_actions")

LEDGER_TRIGGER_NAMES = (
    "ledger_transactions_no_update",
    "ledger_transactions_no_delete",
    "ledger_entries_no_update",
    "ledger_entries_no_delete",
    "ledger_entries_balance_deferred",
)

OPERATOR_AUDIT_TRIGGER_NAMES = (
    "operator_actions_no_update",
    "operator_actions_no_delete",
    "operator_actions_no_truncate",
)


def ledger_trigger_names() -> tuple[str, ...]:
    return LEDGER_TRIGGER_NAMES


def operator_audit_trigger_names() -> tuple[str, ...]:
    return OPERATOR_AUDIT_TRIGGER_NAMES


def assert_schema_revision(engine: Engine, expected_revision: str) -> None:
    """Fail closed unless the database has exactly the expected Alembic head."""
    try:
        with engine.connect() as connection:
            row = connection.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    except Exception as exc:  # table missing, inaccessible DB, or malformed schema
        raise RuntimeError("生产数据库缺少 schema revision，请先执行 alembic upgrade") from exc
    revisions = {str(item[0]) for item in row}
    if revisions != {expected_revision}:
        raise RuntimeError(
            f"生产数据库 schema revision 不匹配: expected={expected_revision}, actual={sorted(revisions)}"
        )


def assert_safe_downgrade(environment: str, allow_destructive_downgrade: bool) -> None:
    """Downgrades are maintenance-only; production needs an explicit flag."""
    if environment.lower() == "production" and not allow_destructive_downgrade:
        raise RuntimeError("production downgrade 被阻止；必须显式设置 allow_destructive_downgrade")
