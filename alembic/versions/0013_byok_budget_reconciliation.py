"""Allow audited BYOK reconciliation costs to exceed an exhausted cap.

``limit_microusd`` remains the hard limit for new reservations. A historical
upstream charge may only exceed it after the reservation has been released,
which keeps automatic admission fail-closed while preserving verified facts.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade


revision: str = "0013_byok_budget_reconciliation"
down_revision: Union[str, None] = "0012_supabase_vault"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CONSTRAINT = (
    "spent_microusd + reserved_microusd <= limit_microusd OR "
    "(kind = 'provider_spend_cap' AND spent_microusd > limit_microusd "
    "AND reserved_microusd = 0)"
)


def _replace_constraint(expression: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite cannot DROP CONSTRAINT; batch mode rebuilds only this table.
        with op.batch_alter_table("budgets", recreate="always") as batch:
            batch.drop_constraint("budget_within_limit", type_="check")
            batch.create_check_constraint("budget_within_limit", expression)
        return
    op.drop_constraint("budget_within_limit", "budgets", type_="check")
    op.create_check_constraint("budget_within_limit", "budgets", expression)


def upgrade() -> None:
    _replace_constraint(CONSTRAINT)
    duplicates = op.get_bind().execute(sa.text("""
        SELECT user_id, kind, period_start
        FROM budgets
        WHERE status = 'active'
        GROUP BY user_id, kind, period_start
        HAVING count(*) > 1
    """)).first()
    if duplicates is not None:
        raise RuntimeError("存在重复 active budget，拒绝建立并发唯一性约束")
    op.create_index(
        "uq_active_budget_user_kind_period",
        "budgets",
        ["user_id", "kind", "period_start"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    assert_safe_downgrade(
        context.config.attributes.get("environment", "development"),
        bool(context.config.attributes.get("allow_destructive_downgrade", False)),
    )
    remaining = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM budgets WHERE spent_microusd > limit_microusd"
    )).scalar_one()
    if remaining:
        raise RuntimeError("存在已核验的超预算 BYOK 成本，不能降级预算约束")
    op.drop_index("uq_active_budget_user_kind_period", table_name="budgets")
    _replace_constraint("spent_microusd + reserved_microusd <= limit_microusd")
