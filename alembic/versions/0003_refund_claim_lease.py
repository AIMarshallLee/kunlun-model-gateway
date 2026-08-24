"""Add an immutable-audit-safe lease timestamp for refund commands."""

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade


revision: str = "0003_refund_claim_lease"
down_revision: Union[str, None] = "0002_production_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "payment_refunds",
        sa.Column("claim_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(sa.text(
        "UPDATE payment_refunds SET claim_started_at = created_at "
        "WHERE claim_started_at IS NULL"
    ))
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("payment_refunds", recreate="always") as batch:
            batch.alter_column("claim_started_at", nullable=False)
    else:
        op.alter_column("payment_refunds", "claim_started_at", nullable=False)


def downgrade() -> None:
    allow = context.config.attributes.get("allow_destructive_downgrade", False)
    environment = context.config.attributes.get("environment", "development")
    assert_safe_downgrade(environment, bool(allow))
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("payment_refunds", recreate="always") as batch:
            batch.drop_column("claim_started_at")
    else:
        op.drop_column("payment_refunds", "claim_started_at")
