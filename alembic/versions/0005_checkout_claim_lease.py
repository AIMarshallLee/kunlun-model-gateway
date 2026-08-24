"""Add a durable lease marker for payment checkout creation."""

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade


revision: str = "0005_checkout_claim_lease"
down_revision: Union[str, None] = "0004_single_full_refund"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "payment_orders",
        sa.Column("checkout_claim_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    allow = context.config.attributes.get("allow_destructive_downgrade", False)
    environment = context.config.attributes.get("environment", "development")
    assert_safe_downgrade(environment, bool(allow))
    op.drop_column("payment_orders", "checkout_claim_started_at")
