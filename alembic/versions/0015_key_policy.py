"""Optional immutable key scope and lifetime spend ceiling; old keys unchanged."""

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade

revision: str = "0015_key_policy"
down_revision: str = "0014_managed_gateway"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("api_keys") as batch:
        batch.add_column(sa.Column("allowed_models_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("max_output_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("spend_limit_microusd", sa.BigInteger(), nullable=True))
        batch.create_check_constraint("key_output_positive", "max_output_tokens IS NULL OR max_output_tokens > 0")
        batch.create_check_constraint("key_spend_positive", "spend_limit_microusd IS NULL OR spend_limit_microusd > 0")


def downgrade() -> None:
    assert_safe_downgrade(
        context.config.attributes.get("environment", "development"),
        bool(context.config.attributes.get("allow_destructive_downgrade", False)),
    )
    if op.get_bind().execute(sa.text("SELECT 1 FROM api_keys WHERE allowed_models_json IS NOT NULL OR max_output_tokens IS NOT NULL OR spend_limit_microusd IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("存在受限 API Key，降级会移除安全限制；请先迁移策略")
    with op.batch_alter_table("api_keys") as batch:
        batch.drop_constraint("key_spend_positive", type_="check")
        batch.drop_constraint("key_output_positive", type_="check")
        batch.drop_column("spend_limit_microusd")
        batch.drop_column("max_output_tokens")
        batch.drop_column("allowed_models_json")
