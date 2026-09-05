"""Isolate platform supply secrets and persist global cost holds."""

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade

revision: str = "0014_managed_gateway"
down_revision: str = "0013_byok_budget_reconciliation"
branch_labels = depends_on = None


def upgrade():
    op.create_table("platform_daily_budgets",
        sa.Column("period", sa.String(10), primary_key=True),
        sa.Column("limit_microusd", sa.BigInteger(), nullable=False),
        sa.Column("spent_microusd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_microusd", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("limit_microusd > 0 AND spent_microusd >= 0 AND reserved_microusd >= 0", name="platform_budget_nonnegative"))
    op.add_column("model_requests", sa.Column("platform_budget_period", sa.String(10), nullable=True))
    op.add_column("model_requests", sa.Column("platform_reserved_microusd", sa.BigInteger(), nullable=False, server_default="0"))
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text("""
      DO $$ BEGIN
        IF current_user <> 'kunlun_migrator' THEN RAISE EXCEPTION '0014 requires kunlun_migrator'; END IF;
      END $$;
      ALTER TABLE public.platform_daily_budgets ENABLE ROW LEVEL SECURITY;
      CREATE POLICY kunlun_runtime_all_platform_daily_budgets ON public.platform_daily_budgets TO kunlun_runtime USING (true) WITH CHECK (true);
      REVOKE ALL ON public.platform_daily_budgets FROM PUBLIC, anon, authenticated, kunlun_vault_executor;
      GRANT SELECT, INSERT, UPDATE, DELETE ON public.platform_daily_budgets TO kunlun_runtime;
      CREATE TABLE kunlun_private.platform_channels (
        provider text PRIMARY KEY, id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
        version integer NOT NULL DEFAULT 0, active boolean NOT NULL DEFAULT false,
        vault_ref uuid UNIQUE, pending_cleanup boolean NOT NULL DEFAULT false
      );
      CREATE TABLE kunlun_private.platform_channel_audits (
        operation_id text PRIMARY KEY, provider text NOT NULL, actor text NOT NULL,
        reason text NOT NULL, action text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
      );
      REVOKE ALL ON kunlun_private.platform_channels, kunlun_private.platform_channel_audits
        FROM PUBLIC, anon, authenticated, kunlun_runtime, kunlun_vault_executor;

      CREATE FUNCTION kunlun_private.platform_audit_immutable() RETURNS trigger
      LANGUAGE plpgsql SET search_path = pg_catalog AS $$
      BEGIN RAISE EXCEPTION 'platform audit is append-only'; END $$;
      REVOKE ALL ON FUNCTION kunlun_private.platform_audit_immutable() FROM PUBLIC, anon, authenticated, kunlun_runtime, kunlun_vault_executor;
      CREATE TRIGGER platform_audit_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
        ON kunlun_private.platform_channel_audits FOR EACH STATEMENT EXECUTE FUNCTION kunlun_private.platform_audit_immutable();

      CREATE FUNCTION kunlun_private.platform_channel_write(
        p_provider text, p_secret text, p_operation_id text, p_actor text, p_reason text
      ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
      DECLARE pc kunlun_private.platform_channels%ROWTYPE; new_ref uuid; cleanup_failed boolean := false;
      BEGIN
        IF session_user <> 'kunlun_vault_executor' OR p_provider IS NULL OR length(p_provider) NOT BETWEEN 1 AND 80
          OR p_operation_id IS NULL OR length(p_operation_id) NOT BETWEEN 1 AND 64
          OR p_actor IS NULL OR length(p_actor) NOT BETWEEN 1 AND 512
          OR p_reason IS NULL OR length(p_reason) NOT BETWEEN 10 AND 500
          OR (p_secret IS NOT NULL AND length(p_secret) NOT BETWEEN 1 AND 8192)
        THEN RAISE EXCEPTION 'invalid platform credential operation'; END IF;
        INSERT INTO kunlun_private.platform_channel_audits(operation_id,provider,actor,reason,action)
          VALUES(p_operation_id,p_provider,p_actor,p_reason,CASE WHEN p_secret IS NULL THEN 'revoke' ELSE 'provision' END);
        INSERT INTO kunlun_private.platform_channels(provider) VALUES(p_provider) ON CONFLICT(provider) DO NOTHING;
        SELECT * INTO pc FROM kunlun_private.platform_channels WHERE provider=p_provider FOR UPDATE;
        IF pc.vault_ref IS NOT NULL THEN
          BEGIN
            DELETE FROM vault.secrets WHERE id=pc.vault_ref AND name='kunlun-platform-' || pc.id::text || '-' || pc.version::text;
            IF NOT FOUND THEN RAISE EXCEPTION 'platform Vault binding mismatch'; END IF;
          EXCEPTION WHEN OTHERS THEN
            IF p_secret IS NOT NULL THEN RAISE EXCEPTION 'platform credential rotation failed'; END IF;
            cleanup_failed := true;
          END;
        END IF;
        IF p_secret IS NOT NULL THEN
          SELECT vault.create_secret(p_secret,'kunlun-platform-' || pc.id::text || '-' || (pc.version+1)::text,'Kunlun managed platform supply') INTO new_ref;
        END IF;
        UPDATE kunlun_private.platform_channels SET
          version=CASE WHEN cleanup_failed THEN pc.version ELSE pc.version+1 END,
          active=(p_secret IS NOT NULL), pending_cleanup=cleanup_failed,
          vault_ref=CASE WHEN cleanup_failed THEN pc.vault_ref ELSE new_ref END
          WHERE provider=p_provider RETURNING * INTO pc;
        RETURN jsonb_build_object('id',pc.id,'provider',pc.provider,'version',pc.version,'active',pc.active,'pending_cleanup',pc.pending_cleanup);
      END $$;

      CREATE FUNCTION kunlun_private.platform_channel_list() RETURNS jsonb
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
      BEGIN
        IF session_user <> 'kunlun_vault_executor' THEN RAISE EXCEPTION 'invalid platform executor'; END IF;
        RETURN (SELECT coalesce(jsonb_agg(jsonb_build_object('id',id,'provider',provider,'version',version,'active',active,'pending_cleanup',pending_cleanup) ORDER BY provider),'[]'::jsonb)
          FROM kunlun_private.platform_channels);
      END $$;

      CREATE FUNCTION kunlun_private.platform_channel_resolve(p_provider text)
      RETURNS TABLE(secret text, channel_id text, credential_version integer)
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
      BEGIN
        IF session_user <> 'kunlun_vault_executor' THEN RAISE EXCEPTION 'invalid platform executor'; END IF;
        RETURN QUERY SELECT s.decrypted_secret::text,c.id::text,c.version
          FROM kunlun_private.platform_channels c JOIN vault.decrypted_secrets s ON s.id=c.vault_ref
          WHERE c.provider=p_provider AND c.active AND NOT c.pending_cleanup
            AND s.name='kunlun-platform-' || c.id::text || '-' || c.version::text;
        IF NOT FOUND THEN RAISE EXCEPTION 'platform channel unavailable'; END IF;
      END $$;
      CREATE FUNCTION kunlun_private.platform_operation_get(p_operation_id text) RETURNS jsonb
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
      BEGIN
        IF session_user <> 'kunlun_vault_executor' THEN RAISE EXCEPTION 'invalid platform executor'; END IF;
        RETURN (SELECT jsonb_build_object('operation_id',operation_id,'provider',provider,
          'actor',actor,'reason',reason,'action',action,'created_at',created_at)
          FROM kunlun_private.platform_channel_audits WHERE operation_id=p_operation_id);
      END $$;
      REVOKE ALL ON FUNCTION kunlun_private.platform_channel_write(text,text,text,text,text),
        kunlun_private.platform_channel_list(),kunlun_private.platform_channel_resolve(text),kunlun_private.platform_operation_get(text)
        FROM PUBLIC, anon, authenticated, kunlun_runtime;
      GRANT EXECUTE ON FUNCTION kunlun_private.platform_channel_write(text,text,text,text,text),
        kunlun_private.platform_channel_list(),kunlun_private.platform_channel_resolve(text),kunlun_private.platform_operation_get(text) TO kunlun_vault_executor;
    """))


def downgrade():
    assert_safe_downgrade(context.config.attributes.get("environment", "development"), bool(context.config.attributes.get("allow_destructive_downgrade", False)))
    raise RuntimeError("managed gateway rollback requires approved preservation of cost holds and platform credentials")
