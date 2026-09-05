"""BYOK connection metadata and provider spend budgets.

Secrets and Vault references never enter the public schema.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade


revision: str = "0011_byok_credentials"
down_revision: Union[str, None] = "0010_runtime_contract"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.add_column("budgets", sa.Column("kind", sa.String(length=32), nullable=False, server_default="prepaid_credit"))
    op.create_index("ix_budgets_kind", "budgets", ["kind"])
    op.add_column("model_requests", sa.Column("billing_mode", sa.String(length=24), nullable=False, server_default="prepaid"))
    op.create_index("ix_model_requests_billing_mode", "model_requests", ["billing_mode"])
    op.add_column("model_requests", sa.Column("final_attempt_id", sa.String(length=36), nullable=True))
    op.create_index("ix_model_requests_final_attempt_id", "model_requests", ["final_attempt_id"])
    op.add_column("model_requests", sa.Column("cost_state", sa.String(length=32), nullable=False, server_default="reserved"))
    op.create_index("ix_model_requests_cost_state", "model_requests", ["cost_state"])
    op.add_column("provider_attempts", sa.Column("credential_connection_id", sa.String(length=36), nullable=True))
    op.create_index("ix_provider_attempts_credential_connection_id", "provider_attempts", ["credential_connection_id"])
    op.add_column("provider_attempts", sa.Column("credential_version", sa.Integer(), nullable=True))
    op.add_column("provider_attempts", sa.Column("pricing_snapshot_json", sa.Text(), nullable=False, server_default="{}"))
    op.add_column("provider_attempts", sa.Column("input_tokens", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("provider_attempts", sa.Column("output_tokens", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("provider_attempts", sa.Column("upstream_cost_microusd", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("provider_attempts", sa.Column("usage_estimated", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("provider_attempts", sa.Column("billing_status", sa.String(length=32), nullable=False, server_default="unsettled"))
    op.create_index("ix_provider_attempts_billing_status", "provider_attempts", ["billing_status"])
    op.add_column("provider_attempts", sa.Column("is_final", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_provider_attempts_is_final", "provider_attempts", ["is_final"])
    if _is_postgres():
        op.alter_column("provider_attempts", "completed_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    else:
        with op.batch_alter_table("provider_attempts") as batch:
            batch.alter_column("completed_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    op.add_column("provider_attempts", sa.Column("duration_ms", sa.BigInteger(), nullable=True))
    op.create_table(
        "provider_connections",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column("credential_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "provider", name="uq_provider_connection_user_provider"),
    )
    op.create_index("ix_provider_connections_user_id", "provider_connections", ["user_id"])
    op.create_index("ix_provider_connections_provider", "provider_connections", ["provider"])
    op.create_index("ix_provider_connections_status", "provider_connections", ["status"])
    op.create_table(
        "credential_action_audits",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("connection_id", sa.String(length=36), sa.ForeignKey("provider_connections.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_credential_action_audits_user_id", "credential_action_audits", ["user_id"])
    op.create_index("ix_credential_action_audits_connection_id", "credential_action_audits", ["connection_id"])
    op.create_index("ix_credential_action_audits_action", "credential_action_audits", ["action"])
    if not _is_postgres():
        return
    op.execute(sa.text("""
        DO $$ BEGIN
            IF current_user <> 'kunlun_migrator' THEN
                RAISE EXCEPTION '0011 must run as kunlun_migrator (got %)', current_user;
            END IF;
        END $$;
    """))
    for table in ("provider_connections", "credential_action_audits"):
        op.execute(sa.text(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY'))
        op.execute(sa.text(f'REVOKE ALL ON TABLE public."{table}" FROM PUBLIC, anon, authenticated'))
    op.execute(sa.text("GRANT SELECT ON TABLE public.provider_connections, public.credential_action_audits TO kunlun_runtime"))
    op.execute(sa.text("""
        CREATE POLICY kunlun_runtime_select_provider_connections ON public.provider_connections
        FOR SELECT TO kunlun_runtime USING (true);
        CREATE POLICY kunlun_runtime_select_credential_action_audits ON public.credential_action_audits
        FOR SELECT TO kunlun_runtime USING (true);
    """))
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION public.kunlun_reject_credential_audit_mutation()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        BEGIN RAISE EXCEPTION 'credential_action_audits append-only'; END $$;
        CREATE TRIGGER credential_action_audits_no_update BEFORE UPDATE ON public.credential_action_audits
        FOR EACH ROW EXECUTE FUNCTION public.kunlun_reject_credential_audit_mutation();
        CREATE TRIGGER credential_action_audits_no_delete BEFORE DELETE ON public.credential_action_audits
        FOR EACH ROW EXECUTE FUNCTION public.kunlun_reject_credential_audit_mutation();
        CREATE TRIGGER credential_action_audits_no_truncate BEFORE TRUNCATE ON public.credential_action_audits
        FOR EACH STATEMENT EXECUTE FUNCTION public.kunlun_reject_credential_audit_mutation();
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON public.provider_connections, public.credential_action_audits FROM kunlun_runtime;
    """))


def downgrade() -> None:
    assert_safe_downgrade(context.config.attributes.get("environment", "development"), bool(context.config.attributes.get("allow_destructive_downgrade", False)))
    raise RuntimeError("BYOK credential metadata downgrade requires an approved maintenance migration")
