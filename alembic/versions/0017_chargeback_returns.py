"""Confirmed funds returns, separate from dispute outcome notifications."""

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade

revision: str = "0017_chargeback_returns"
down_revision = "0016_chargebacks"
branch_labels = depends_on = None


def upgrade():
    op.create_table("payment_chargeback_returns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("payment_orders.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("chargeback_id", sa.String(36), sa.ForeignKey("payment_chargebacks.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_dispute_id", sa.String(120), nullable=False),
        sa.Column("provider_return_id", sa.String(120), nullable=False),
        sa.Column("payment_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("payment_currency", sa.String(3), nullable=False),
        sa.Column("restored_microusd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("canceled_risk_microusd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reversed_loss_microusd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("risk_reason", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("provider", "provider_return_id", name="uq_chargeback_return_provider_id"),
        sa.CheckConstraint("payment_amount_minor > 0", name="chargeback_return_amount_positive"),
        sa.CheckConstraint("restored_microusd >= 0 AND canceled_risk_microusd >= 0 AND reversed_loss_microusd >= 0", name="chargeback_return_credit_bounds"),
        sa.CheckConstraint("status IN ('applied', 'pending_reconciliation')", name="chargeback_return_status"))
    for column in ("order_id", "user_id", "chargeback_id", "status"):
        op.create_index(f"ix_payment_chargeback_returns_{column}", "payment_chargeback_returns", [column])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("""
          DO $$ BEGIN
            IF current_user <> 'kunlun_migrator' THEN RAISE EXCEPTION '0017 requires kunlun_migrator'; END IF;
          END $$;
          ALTER TABLE public.payment_chargeback_returns ENABLE ROW LEVEL SECURITY;
          CREATE POLICY kunlun_runtime_all_payment_chargeback_returns ON public.payment_chargeback_returns
            TO kunlun_runtime USING (true) WITH CHECK (true);
          REVOKE ALL ON public.payment_chargeback_returns FROM PUBLIC, anon, authenticated, kunlun_vault_executor;
          GRANT SELECT, INSERT, UPDATE, DELETE ON public.payment_chargeback_returns TO kunlun_runtime;
        """))


def downgrade():
    assert_safe_downgrade(context.config.attributes.get("environment", "development"),
                          bool(context.config.attributes.get("allow_destructive_downgrade", False)))
    if op.get_bind().execute(sa.text("SELECT 1 FROM payment_chargeback_returns LIMIT 1")).first():
        raise RuntimeError("拒付返还记录必须保留，禁止降级删除财务证据")
    op.drop_table("payment_chargeback_returns")
