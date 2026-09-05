-- CI-only fixture. This emulates the Supabase Vault catalog and ACL surface
-- for local PostgreSQL contract checks; it is not Supabase encryption or a
-- production security signal.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    CREATE ROLE anon NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    CREATE ROLE authenticated NOLOGIN;
  END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS vault;
CREATE TABLE IF NOT EXISTS vault.secrets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  description text,
  secret text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE VIEW vault.decrypted_secrets AS
SELECT id, name, secret AS decrypted_secret FROM vault.secrets;

CREATE OR REPLACE FUNCTION vault.create_secret(
  new_secret text, new_name text, new_description text
) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, vault
AS $$
DECLARE secret_id uuid;
BEGIN
  INSERT INTO vault.secrets(name, description, secret)
  VALUES (new_name, new_description, new_secret)
  RETURNING id INTO secret_id;
  RETURN secret_id;
END
$$;

-- Owned by the isolated fixture administrator, matching the extension-owner
-- boundary. The custom migrator only receives the bootstrap EXECUTE grant.
