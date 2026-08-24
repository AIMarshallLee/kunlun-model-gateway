"""Production hardening: explicit cash/credit accounting and audit boundaries."""

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade


revision: str = '0002_production_hardening'
down_revision: Union[str, None] = '0001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _batch_mode() -> str:
    # PostgreSQL must ALTER in place: payment_orders already has inbound
    # foreign keys. SQLite needs batch recreation for rename/constraint work.
    return "never" if _is_postgres() else "always"


def upgrade() -> None:
    # The initial schema called customer credit ``amount_microusd``.  Make the
    # semantic boundary explicit: provider cash is payment_amount_minor while
    # credit remains microUSD.  Batch mode keeps this migration executable on
    # SQLite test databases as well as PostgreSQL.
    # Existing PostgreSQL triggers ledger_entries_no_update,
    # ledger_entries_no_delete and ledger_entries_balance_deferred are retained.
    with op.batch_alter_table("payment_orders", recreate=_batch_mode()) as batch:
        batch.alter_column("amount_microusd", new_column_name="credit_amount_microusd")
        batch.drop_constraint("payment_amount_positive", type_="check")
        batch.add_column(sa.Column("payment_amount_minor", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("payment_currency", sa.String(length=3), nullable=True))
        batch.add_column(sa.Column("checkout_url", sa.String(length=2048), nullable=True))
        batch.add_column(sa.Column("quote_numerator", sa.BigInteger(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("quote_denominator", sa.BigInteger(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("quote_id", sa.String(length=80), nullable=True))
        batch.add_column(sa.Column("client_idempotency_key", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("risk_reason", sa.String(length=80), nullable=True))
        batch.add_column(sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True))
        # The legacy column described internal test credit as a "currency".
        # Cash currency now has its own nullable ISO-4217 field.
        batch.drop_column("currency")
        batch.create_check_constraint("payment_credit_amount_positive", "credit_amount_microusd > 0")
        batch.create_check_constraint(
            "payment_cash_amount_positive",
            "payment_amount_minor IS NULL OR payment_amount_minor > 0",
        )
        batch.create_check_constraint("payment_quote_numerator_positive", "quote_numerator > 0")
        batch.create_check_constraint("payment_quote_denominator_positive", "quote_denominator > 0")
        batch.create_unique_constraint("uq_payment_user_idempotency", ["user_id", "client_idempotency_key"])

    with op.batch_alter_table("payment_webhook_events", recreate=_batch_mode()) as batch:
        batch.add_column(sa.Column("nonce", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("event_type", sa.String(length=48), nullable=False, server_default="unknown"))
    op.get_bind().execute(sa.text("UPDATE payment_webhook_events SET nonce = id WHERE nonce IS NULL"))
    with op.batch_alter_table("payment_webhook_events", recreate=_batch_mode()) as batch:
        batch.alter_column("nonce", nullable=False)
        batch.create_unique_constraint("uq_payment_provider_nonce", ["provider", "nonce"])

    # Operator actions were originally only request-scoped.  Keep the old
    # rows valid while adding a durable, idempotent audit identity.
    with op.batch_alter_table("operator_actions", recreate=_batch_mode()) as batch:
        batch.alter_column("request_id", nullable=True)
        batch.add_column(sa.Column("actor", sa.String(length=200), nullable=False, server_default="legacy-operator"))
        batch.add_column(sa.Column("scopes", sa.String(length=500), nullable=False, server_default=""))
        batch.add_column(sa.Column("token_id", sa.String(length=120), nullable=False, server_default="legacy"))
        # Existing rows are backfilled immediately after the batch rebuild;
        # using a nullable staging column avoids a non-portable expression
        # default (and keeps SQLite compatible).
        batch.add_column(sa.Column("operation_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("source_ip_digest", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("before_status", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("after_status", sa.String(length=32), nullable=True))
    op.get_bind().execute(sa.text("UPDATE operator_actions SET operation_id = id WHERE operation_id IS NULL"))
    with op.batch_alter_table("operator_actions", recreate=_batch_mode()) as batch:
        batch.alter_column("operation_id", nullable=False)
    op.create_index(
        "ix_operator_actions_operation_id",
        "operator_actions",
        ["operation_id"],
        unique=True,
    )

    op.create_table(
        "payment_refunds",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("order_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("provider_refund_id", sa.String(length=120), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("payment_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("payment_currency", sa.String(length=3), nullable=False),
        sa.Column("credit_amount_microusd", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("risk_reason", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("payment_amount_minor > 0", name="refund_cash_amount_positive"),
        sa.CheckConstraint("credit_amount_microusd > 0", name="refund_credit_amount_positive"),
        sa.ForeignKeyConstraint(["order_id"], ["payment_orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_refund_id"),
        sa.UniqueConstraint("order_id", "idempotency_key", name="uq_refund_order_idempotency"),
    )
    op.create_index("ix_payment_refunds_order_id", "payment_refunds", ["order_id"])
    op.create_index("ix_payment_refunds_user_id", "payment_refunds", ["user_id"])
    op.create_index("ix_payment_refunds_status", "payment_refunds", ["status"])

    op.create_table(
        "safety_audits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("api_key_id", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=True),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=True),
        sa.Column("policy_version", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["request_id"], ["model_requests.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("user_id", "api_key_id", "request_id", "phase", "outcome"):
        op.create_index(f"ix_safety_audits_{column}", "safety_audits", [column])

    # A transaction's owner and each entry's owner must agree.  PostgreSQL is
    # the production enforcement point; SQLite has no portable ALTER TABLE FK
    # support, so its upgrade remains executable and application checks remain.
    if _is_postgres():
        # PostgreSQL emits this as DEFERRABLE INITIALLY DEFERRED so a balanced
        # pair of entries may be inserted in either order within one commit.
        op.create_unique_constraint("uq_ledger_transactions_id_user", "ledger_transactions", ["id", "user_id"])
        op.create_foreign_key(
            "fk_ledger_entries_transaction_user",
            "ledger_entries",
            "ledger_transactions",
            ["transaction_id", "user_id"],
            ["id", "user_id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        )
        # The bundled production compose uses a non-owner runtime role. It may
        # append and read journal rows, but cannot mutate history, truncate, or
        # disable the trigger boundary. Managed deployments can enforce the
        # same grants under a differently named role.
        op.execute(sa.text("REVOKE ALL ON ledger_transactions, ledger_entries FROM PUBLIC"))
        op.execute(sa.text("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kunlun_runtime') THEN
                    GRANT SELECT, INSERT ON ledger_transactions, ledger_entries TO kunlun_runtime;
                    REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
                        ON ledger_transactions, ledger_entries FROM kunlun_runtime;
                    GRANT SELECT ON alembic_version TO kunlun_runtime;
                    REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
                        ON alembic_version FROM kunlun_runtime;
                END IF;
            END;
            $$;
        """))


def downgrade() -> None:
    allow_destructive_downgrade = context.config.attributes.get("allow_destructive_downgrade", False)
    environment = context.config.attributes.get("environment", "development")
    assert_safe_downgrade(environment, bool(allow_destructive_downgrade))
    if _is_postgres():
        op.drop_constraint("fk_ledger_entries_transaction_user", "ledger_entries", type_="foreignkey")
        op.drop_constraint("uq_ledger_transactions_id_user", "ledger_transactions", type_="unique")
    for column in ("user_id", "api_key_id", "request_id", "phase", "outcome"):
        op.drop_index(f"ix_safety_audits_{column}", table_name="safety_audits")
    op.drop_table("safety_audits")
    op.drop_index("ix_payment_refunds_status", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_user_id", table_name="payment_refunds")
    op.drop_index("ix_payment_refunds_order_id", table_name="payment_refunds")
    op.drop_table("payment_refunds")
    op.drop_index("ix_operator_actions_operation_id", table_name="operator_actions")
    with op.batch_alter_table("operator_actions", recreate=_batch_mode()) as batch:
        for column in ("after_status", "before_status", "source_ip_digest", "operation_id", "token_id", "scopes", "actor"):
            batch.drop_column(column)
        batch.alter_column("request_id", nullable=False)
    with op.batch_alter_table("payment_webhook_events", recreate=_batch_mode()) as batch:
        batch.drop_constraint("uq_payment_provider_nonce", type_="unique")
        batch.drop_column("nonce")
        batch.drop_column("event_type")
    with op.batch_alter_table("payment_orders", recreate=_batch_mode()) as batch:
        batch.drop_constraint("uq_payment_user_idempotency", type_="unique")
        for name in ("payment_quote_denominator_positive", "payment_quote_numerator_positive", "payment_cash_amount_positive", "payment_credit_amount_positive"):
            batch.drop_constraint(name, type_="check")
        batch.add_column(sa.Column("currency", sa.String(length=16), nullable=False, server_default="microUSD"))
        for column in ("refunded_at", "risk_reason", "client_idempotency_key", "quote_id", "quote_denominator", "quote_numerator", "checkout_url", "payment_currency", "payment_amount_minor"):
            batch.drop_column(column)
        batch.alter_column("credit_amount_microusd", new_column_name="amount_microusd")
        batch.create_check_constraint("payment_amount_positive", "amount_microusd > 0")
