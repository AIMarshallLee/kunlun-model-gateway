"""Add a durable claim lease for operator payment reconciliation.

Revision ID: 0007_payment_reconcile_claim
Revises: 0006_operator_audit_targets
"""

from __future__ import annotations

from typing import Union

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade


revision: str = "0007_payment_reconcile_claim"
down_revision: Union[str, None] = "0006_operator_audit_targets"
branch_labels: Union[str, list[str], None] = None
depends_on: Union[str, list[str], None] = None


def _batch_mode() -> str:
    return "always" if op.get_bind().dialect.name == "sqlite" else "auto"


def upgrade() -> None:
    with op.batch_alter_table("payment_orders", recreate=_batch_mode()) as batch:
        batch.add_column(sa.Column(
            "reconciliation_claim_started_at", sa.DateTime(timezone=True), nullable=True,
        ))


def downgrade() -> None:
    allow = context.config.attributes.get("allow_destructive_downgrade", False)
    environment = context.config.attributes.get("environment", "development")
    assert_safe_downgrade(environment, bool(allow))
    with op.batch_alter_table("payment_orders", recreate=_batch_mode()) as batch:
        batch.drop_column("reconciliation_claim_started_at")
