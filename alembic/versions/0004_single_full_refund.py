"""Enforce the product's one full-refund command per order invariant."""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import context, op

from app.db_guards import assert_safe_downgrade


revision: str = "0004_single_full_refund"
down_revision: Union[str, None] = "0003_refund_claim_lease"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A constraint failure alone is opaque and can leave an operator unsure
    # whether retrying the migration is safe.  Detect legacy duplicate refund
    # commands before changing the schema and require an explicit data repair.
    if not context.is_offline_mode():
        duplicate_order = op.get_bind().execute(sa.text(
            "SELECT order_id FROM payment_refunds "
            "GROUP BY order_id HAVING COUNT(*) > 1 LIMIT 1"
        )).scalar_one_or_none()
        if duplicate_order is not None:
            raise RuntimeError(
                "payment_refunds 存在同订单多条退款记录；请先人工核对并修复后再迁移"
            )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("payment_refunds", recreate="always") as batch:
            batch.create_unique_constraint("uq_refund_order", ["order_id"])
    else:
        op.create_unique_constraint("uq_refund_order", "payment_refunds", ["order_id"])


def downgrade() -> None:
    allow = context.config.attributes.get("allow_destructive_downgrade", False)
    environment = context.config.attributes.get("environment", "development")
    assert_safe_downgrade(environment, bool(allow))
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("payment_refunds", recreate="always") as batch:
            batch.drop_constraint("uq_refund_order", type_="unique")
    else:
        op.drop_constraint("uq_refund_order", "payment_refunds", type_="unique")
