"""Executor-only, bound Supabase Vault credential functions."""

from __future__ import annotations

from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa

from app.db_guards import assert_safe_downgrade


revision: str = "0012_supabase_vault"
down_revision: Union[str, None] = "0011_byok_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text("""
        DO $$ BEGIN
          IF current_user <> 'kunlun_migrator' THEN RAISE EXCEPTION '0012 requires kunlun_migrator'; END IF;
          IF to_regnamespace('kunlun_private') IS NULL OR to_regprocedure('vault.create_secret(text,text,text)') IS NULL THEN
            RAISE EXCEPTION 'run bootstrap_supabase_vault.sql as administrator first';
          END IF;
        END $$;
        REVOKE ALL ON SCHEMA kunlun_private FROM PUBLIC, anon, authenticated, kunlun_runtime;
        GRANT USAGE ON SCHEMA kunlun_private TO kunlun_vault_executor;
        CREATE TABLE kunlun_private.provider_credential_bindings (
          connection_id uuid PRIMARY KEY,
          user_id uuid NOT NULL,
          provider text NOT NULL,
          credential_version integer NOT NULL,
          vault_ref uuid NOT NULL UNIQUE,
          status text NOT NULL CHECK (status = 'active'),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (user_id, provider, credential_version)
        );
        CREATE TABLE kunlun_private.installation_marker (
          singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
          installation_id uuid NOT NULL DEFAULT gen_random_uuid(),
          created_at timestamptz NOT NULL DEFAULT now()
        );
        INSERT INTO kunlun_private.installation_marker(singleton) VALUES (true)
        ON CONFLICT (singleton) DO NOTHING;
        REVOKE ALL ON ALL TABLES IN SCHEMA kunlun_private FROM PUBLIC, anon, authenticated, kunlun_runtime, kunlun_vault_executor;
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA kunlun_private FROM PUBLIC, anon, authenticated, kunlun_runtime, kunlun_vault_executor;
    """))
    op.execute(sa.text("""
      CREATE OR REPLACE FUNCTION public.kunlun_installation_id()
      RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
      DECLARE v_installation_id uuid;
      BEGIN
        IF session_user NOT IN ('kunlun_runtime', 'kunlun_migrator', 'kunlun_vault_executor') THEN
          RAISE EXCEPTION 'installation marker caller is not allowed';
        END IF;
        SELECT marker.installation_id INTO v_installation_id
        FROM kunlun_private.installation_marker AS marker
        WHERE marker.singleton = true;
        IF v_installation_id IS NULL THEN
          RAISE EXCEPTION 'installation marker is unavailable';
        END IF;
        RETURN v_installation_id;
      END $$;

      CREATE OR REPLACE FUNCTION kunlun_private.credential_put_v2(
        p_user_id uuid, p_provider text, p_label text, p_secret text
      ) RETURNS TABLE(id text, provider text, label text, status text, credential_version integer,
                      created_at text, updated_at text, revoked_at text)
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
      DECLARE pc public.provider_connections%ROWTYPE; old_ref uuid; new_ref uuid; v integer; action text;
      BEGIN
        IF session_user <> 'kunlun_vault_executor' OR p_secret IS NULL OR length(p_secret) = 0 THEN
          RAISE EXCEPTION 'credential executor binding is invalid';
        END IF;
        SELECT candidate.* INTO pc FROM public.provider_connections AS candidate
          WHERE candidate.user_id = p_user_id::text
            AND candidate.provider = p_provider
          FOR UPDATE;
        IF NOT FOUND THEN
          pc.id := gen_random_uuid()::text; v := 1; action := 'created';
          SELECT vault.create_secret(p_secret, 'kunlun-byok-' || pc.id || '-' || v::text, 'Kunlun BYOK credential') INTO new_ref;
          INSERT INTO public.provider_connections(id,user_id,provider,label,status,credential_version,created_at,updated_at)
            VALUES (pc.id,p_user_id::text,p_provider,p_label,'active',v,now(),now());
        ELSE
          IF pc.status NOT IN ('active', 'revoked') THEN
            RAISE EXCEPTION 'credential connection state is invalid';
          END IF;
          v := pc.credential_version + 1; action := CASE WHEN pc.status = 'revoked' THEN 'reconnected' ELSE 'rotated' END;
          SELECT binding.vault_ref INTO old_ref
          FROM kunlun_private.provider_credential_bindings AS binding
          WHERE binding.connection_id = pc.id::uuid
            AND binding.user_id = p_user_id
            AND binding.provider = p_provider
            AND binding.credential_version = pc.credential_version
            AND binding.status = 'active'
          FOR UPDATE;
          IF pc.status = 'active' AND old_ref IS NULL THEN
            RAISE EXCEPTION 'active credential binding is unavailable';
          END IF;
          IF pc.status = 'revoked' AND EXISTS (
            SELECT 1 FROM kunlun_private.provider_credential_bindings AS stale_binding
            WHERE stale_binding.connection_id = pc.id::uuid
          ) THEN
            RAISE EXCEPTION 'revoked credential still has a private binding';
          END IF;
          IF pc.status = 'active' THEN
            DELETE FROM vault.secrets AS secret_row
            WHERE secret_row.id = old_ref
              AND secret_row.name = 'kunlun-byok-' || pc.id || '-' || pc.credential_version::text;
            IF NOT FOUND THEN RAISE EXCEPTION 'credential Vault identity does not match binding'; END IF;
            DELETE FROM kunlun_private.provider_credential_bindings WHERE connection_id = pc.id::uuid;
          END IF;
          SELECT vault.create_secret(p_secret, 'kunlun-byok-' || pc.id || '-' || v::text, 'Kunlun BYOK credential') INTO new_ref;
          UPDATE public.provider_connections AS target
          SET label=p_label,status='active',credential_version=v,updated_at=now(),revoked_at=NULL
          WHERE target.id=pc.id;
        END IF;
        INSERT INTO kunlun_private.provider_credential_bindings(connection_id,user_id,provider,credential_version,vault_ref,status)
          VALUES (pc.id::uuid,p_user_id,p_provider,v,new_ref,'active');
        INSERT INTO public.credential_action_audits(id,user_id,connection_id,action,credential_version,created_at)
          VALUES (gen_random_uuid()::text,p_user_id::text,pc.id,action,v,now());
        RETURN QUERY SELECT c.id::text,c.provider::text,c.label::text,c.status::text,
          c.credential_version,c.created_at::text,c.updated_at::text,c.revoked_at::text
          FROM public.provider_connections c WHERE c.id=pc.id;
      END $$;

      CREATE OR REPLACE FUNCTION kunlun_private.credential_resolve_v2(
        p_user_id uuid, p_connection_id uuid, p_provider text, p_credential_version integer
      ) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
      DECLARE p_secret text;
      BEGIN
        IF session_user <> 'kunlun_vault_executor' THEN RAISE EXCEPTION 'credential executor binding is invalid'; END IF;
        SELECT ds.decrypted_secret INTO p_secret
        FROM public.provider_connections pc
        JOIN kunlun_private.provider_credential_bindings b ON b.connection_id=pc.id::uuid
        JOIN vault.decrypted_secrets ds ON ds.id=b.vault_ref
        WHERE pc.id=p_connection_id::text AND pc.user_id=p_user_id::text AND pc.provider=p_provider
          AND pc.credential_version=p_credential_version AND pc.status='active'
          AND b.user_id=p_user_id AND b.provider=p_provider AND b.credential_version=p_credential_version
          AND ds.name='kunlun-byok-' || p_connection_id::text || '-' || p_credential_version::text;
        IF p_secret IS NULL OR length(p_secret)=0 THEN RAISE EXCEPTION 'credential is unavailable'; END IF;
        RETURN p_secret;
      END $$;

      CREATE OR REPLACE FUNCTION kunlun_private.credential_revoke_v2(p_user_id uuid, p_provider text)
      RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
      DECLARE pc public.provider_connections%ROWTYPE; ref uuid;
      BEGIN
        IF session_user <> 'kunlun_vault_executor' THEN RAISE EXCEPTION 'credential executor binding is invalid'; END IF;
        SELECT * INTO pc FROM public.provider_connections WHERE user_id=p_user_id::text AND provider=p_provider FOR UPDATE;
        IF NOT FOUND OR pc.status <> 'active' THEN RAISE EXCEPTION 'credential connection is unavailable'; END IF;
        SELECT vault_ref INTO ref FROM kunlun_private.provider_credential_bindings WHERE connection_id=pc.id::uuid FOR UPDATE;
        IF ref IS NULL THEN RAISE EXCEPTION 'credential binding is unavailable'; END IF;
        DELETE FROM vault.secrets AS secret_row
        WHERE secret_row.id=ref
          AND secret_row.name='kunlun-byok-' || pc.id || '-' || pc.credential_version::text;
        IF NOT FOUND THEN RAISE EXCEPTION 'credential Vault identity does not match binding'; END IF;
        DELETE FROM kunlun_private.provider_credential_bindings WHERE connection_id=pc.id::uuid;
        UPDATE public.provider_connections SET status='revoked', revoked_at=now(), updated_at=now() WHERE id=pc.id;
        INSERT INTO public.credential_action_audits(id,user_id,connection_id,action,credential_version,created_at)
          VALUES (gen_random_uuid()::text,p_user_id::text,pc.id,'revoked',pc.credential_version,now());
        RETURN true;
      END $$;

      CREATE OR REPLACE FUNCTION kunlun_private.credential_probe_v2()
      RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
      BEGIN
        IF session_user <> 'kunlun_vault_executor' THEN RAISE EXCEPTION 'credential executor binding is invalid'; END IF;
        IF to_regprocedure('vault.create_secret(text,text,text)') IS NULL THEN RAISE EXCEPTION 'Vault unavailable'; END IF;
        RETURN true;
      END $$;

      REVOKE ALL ON FUNCTION public.kunlun_installation_id() FROM PUBLIC, anon, authenticated;
      GRANT EXECUTE ON FUNCTION public.kunlun_installation_id()
        TO kunlun_runtime, kunlun_migrator, kunlun_vault_executor;
      REVOKE ALL ON FUNCTION kunlun_private.credential_put_v2(uuid,text,text,text) FROM PUBLIC, anon, authenticated, kunlun_runtime;
      REVOKE ALL ON FUNCTION kunlun_private.credential_resolve_v2(uuid,uuid,text,integer) FROM PUBLIC, anon, authenticated, kunlun_runtime;
      REVOKE ALL ON FUNCTION kunlun_private.credential_revoke_v2(uuid,text) FROM PUBLIC, anon, authenticated, kunlun_runtime;
      REVOKE ALL ON FUNCTION kunlun_private.credential_probe_v2() FROM PUBLIC, anon, authenticated, kunlun_runtime;
      GRANT EXECUTE ON FUNCTION kunlun_private.credential_put_v2(uuid,text,text,text),
        kunlun_private.credential_resolve_v2(uuid,uuid,text,integer),
        kunlun_private.credential_revoke_v2(uuid,text), kunlun_private.credential_probe_v2() TO kunlun_vault_executor;
    """))


def downgrade() -> None:
    assert_safe_downgrade(context.config.attributes.get("environment", "development"), bool(context.config.attributes.get("allow_destructive_downgrade", False)))
    raise RuntimeError("Supabase Vault downgrade requires approved maintenance")
