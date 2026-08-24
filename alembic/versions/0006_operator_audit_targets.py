"""Add explicit operator audit targets and append-only database guards."""

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade


revision: str = "0006_operator_audit_targets"
down_revision: Union[str, None] = "0005_checkout_claim_lease"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _batch_mode() -> str:
    return "never" if _is_postgres() else "always"


def upgrade() -> None:
    # Backfill old request-scoped actions truthfully. Historical rows without
    # a request target retain their audit identity as legacy_unknown:<row id>.
    with op.batch_alter_table("operator_actions", recreate=_batch_mode()) as batch:
        batch.add_column(sa.Column("target_type", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("target_id", sa.String(length=120), nullable=True))
    op.execute(sa.text("""
        UPDATE operator_actions
           SET target_type = CASE
                   WHEN request_id IS NOT NULL THEN 'model_request'
                   ELSE 'legacy_unknown'
               END,
               target_id = COALESCE(request_id, id)
         WHERE target_type IS NULL OR target_id IS NULL
    """))
    with op.batch_alter_table("operator_actions", recreate=_batch_mode()) as batch:
        batch.alter_column("target_type", nullable=False)
        batch.alter_column("target_id", nullable=False)
    op.create_index(
        "ix_operator_actions_target_type", "operator_actions", ["target_type"],
    )
    op.create_index(
        "ix_operator_actions_target_id", "operator_actions", ["target_id"],
    )

    if _is_postgres():
        op.execute(sa.text("""
            CREATE OR REPLACE FUNCTION kunlun_reject_operator_action_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'operator audit is append-only: %', TG_OP
                    USING ERRCODE = '55000';
            END;
            $$;
        """))
        op.execute(sa.text("""
            CREATE TRIGGER operator_actions_no_update
            BEFORE UPDATE ON operator_actions
            FOR EACH ROW EXECUTE FUNCTION kunlun_reject_operator_action_mutation()
        """))
        op.execute(sa.text("""
            CREATE TRIGGER operator_actions_no_delete
            BEFORE DELETE ON operator_actions
            FOR EACH ROW EXECUTE FUNCTION kunlun_reject_operator_action_mutation()
        """))
        op.execute(sa.text("""
            CREATE TRIGGER operator_actions_no_truncate
            BEFORE TRUNCATE ON operator_actions
            FOR EACH STATEMENT EXECUTE FUNCTION kunlun_reject_operator_action_mutation()
        """))
        op.execute(sa.text("REVOKE ALL ON operator_actions FROM PUBLIC"))
        op.execute(sa.text("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kunlun_runtime') THEN
                    GRANT SELECT, INSERT ON operator_actions TO kunlun_runtime;
                    REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
                        ON operator_actions FROM kunlun_runtime;
                END IF;
            END;
            $$;
        """))


def downgrade() -> None:
    allow = context.config.attributes.get("allow_destructive_downgrade", False)
    environment = context.config.attributes.get("environment", "development")
    assert_safe_downgrade(environment, bool(allow))
    if _is_postgres():
        op.execute(sa.text(
            "DROP TRIGGER IF EXISTS operator_actions_no_truncate ON operator_actions"
        ))
        op.execute(sa.text(
            "DROP TRIGGER IF EXISTS operator_actions_no_delete ON operator_actions"
        ))
        op.execute(sa.text(
            "DROP TRIGGER IF EXISTS operator_actions_no_update ON operator_actions"
        ))
        op.execute(sa.text(
            "DROP FUNCTION IF EXISTS kunlun_reject_operator_action_mutation()"
        ))
    op.drop_index("ix_operator_actions_target_id", table_name="operator_actions")
    op.drop_index("ix_operator_actions_target_type", table_name="operator_actions")
    with op.batch_alter_table("operator_actions", recreate=_batch_mode()) as batch:
        batch.drop_column("target_id")
        batch.drop_column("target_type")
