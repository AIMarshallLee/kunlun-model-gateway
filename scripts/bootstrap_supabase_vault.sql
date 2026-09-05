-- Run once as the Supabase project administrator, before Alembic 0012.
-- This file contains no credentials and is intentionally separate from the
-- custom migrator.  It only establishes the private schema contract and the
-- minimum owner privileges required by the bound SECURITY DEFINER functions.

CREATE SCHEMA IF NOT EXISTS kunlun_private AUTHORIZATION kunlun_migrator;
DO $$ BEGIN
  IF (SELECT r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner
      WHERE n.nspname = 'kunlun_private') <> 'kunlun_migrator' THEN
    RAISE EXCEPTION 'kunlun_private must be owned by kunlun_migrator';
  END IF;
END $$;
REVOKE ALL ON SCHEMA kunlun_private FROM PUBLIC, anon, authenticated, kunlun_runtime;
GRANT USAGE ON SCHEMA kunlun_private TO kunlun_vault_executor;

REVOKE ALL ON ALL TABLES IN SCHEMA kunlun_private FROM PUBLIC, anon, authenticated, kunlun_runtime, kunlun_vault_executor;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA kunlun_private FROM PUBLIC, anon, authenticated, kunlun_runtime, kunlun_vault_executor;
ALTER DEFAULT PRIVILEGES FOR ROLE kunlun_migrator IN SCHEMA kunlun_private
  REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated, kunlun_runtime, kunlun_vault_executor;
ALTER DEFAULT PRIVILEGES FOR ROLE kunlun_migrator IN SCHEMA kunlun_private
  REVOKE ALL ON SEQUENCES FROM PUBLIC, anon, authenticated, kunlun_runtime, kunlun_vault_executor;

-- Vault access is granted to the definer owner, never to the application role.
-- The exact Vault function signatures are validated by migration 0012 before
-- any function is created.  Do not replace these grants with broad ownership.
REVOKE ALL ON SCHEMA vault FROM PUBLIC, anon, authenticated, kunlun_runtime, kunlun_vault_executor;
REVOKE ALL ON ALL TABLES IN SCHEMA vault FROM PUBLIC, anon, authenticated, kunlun_runtime, kunlun_vault_executor;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA vault FROM PUBLIC, anon, authenticated, kunlun_runtime, kunlun_vault_executor;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA vault FROM PUBLIC, anon, authenticated, kunlun_runtime, kunlun_vault_executor;
GRANT USAGE ON SCHEMA vault TO kunlun_migrator;
GRANT SELECT ON TABLE vault.decrypted_secrets TO kunlun_migrator;
GRANT SELECT, DELETE ON TABLE vault.secrets TO kunlun_migrator;
GRANT EXECUTE ON FUNCTION vault.create_secret(text, text, text) TO kunlun_migrator;
