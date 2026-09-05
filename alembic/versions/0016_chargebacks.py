"""Independent chargeback tracking; never downgrade away financial evidence."""

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade

revision: str = "0016_chargebacks"
down_revision = "0015_key_policy"
branch_labels = depends_on = None


def upgrade():
    op.create_table("payment_chargebacks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("order_id", sa.String(36), sa.ForeignKey("payment_orders.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_dispute_id", sa.String(120), nullable=False),
        sa.Column("payment_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("payment_currency", sa.String(3), nullable=False),
        sa.Column("credit_amount_microusd", sa.BigInteger(), nullable=False),
        sa.Column("recovered_microusd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("outstanding_microusd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("written_off_microusd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("risk_reason", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("provider", "provider_dispute_id", name="uq_chargeback_provider_dispute"),
        sa.CheckConstraint("payment_amount_minor > 0 AND credit_amount_microusd > 0", name="chargeback_amount_positive"),
        sa.CheckConstraint("recovered_microusd >= 0 AND outstanding_microusd >= 0 AND written_off_microusd >= 0 AND recovered_microusd + outstanding_microusd + written_off_microusd <= credit_amount_microusd", name="chargeback_credit_bounds"),
        sa.CheckConstraint("status IN ('recovered', 'risk', 'pending_reconciliation', 'resolved')", name="chargeback_status"))
    for column in ("order_id", "user_id", "status"):
        op.create_index(f"ix_payment_chargebacks_{column}", "payment_chargebacks", [column])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("""
          DO $$ BEGIN
            IF current_user <> 'kunlun_migrator' THEN RAISE EXCEPTION '0016 requires kunlun_migrator'; END IF;
          END $$;
          ALTER TABLE public.payment_chargebacks ENABLE ROW LEVEL SECURITY;
          CREATE POLICY kunlun_runtime_all_payment_chargebacks ON public.payment_chargebacks
            TO kunlun_runtime USING (true) WITH CHECK (true);
          REVOKE ALL ON public.payment_chargebacks FROM PUBLIC, anon, authenticated, kunlun_vault_executor;
          GRANT SELECT, INSERT, UPDATE, DELETE ON public.payment_chargebacks TO kunlun_runtime;
        """))


def downgrade():
    assert_safe_downgrade(context.config.attributes.get("environment", "development"),
                          bool(context.config.attributes.get("allow_destructive_downgrade", False)))
    if op.get_bind().execute(sa.text("SELECT 1 FROM payment_chargebacks LIMIT 1")).first():
        raise RuntimeError("拒付记录必须保留，禁止降级删除财务证据")
    op.drop_table("payment_chargebacks")
