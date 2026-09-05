#!/usr/bin/env bash
set -euo pipefail

: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${KUNLUN_RUNTIME_DB_PASSWORD:?KUNLUN_RUNTIME_DB_PASSWORD is required}"
: "${KUNLUN_MIGRATOR_DB_PASSWORD:?KUNLUN_MIGRATOR_DB_PASSWORD is required}"
: "${KUNLUN_VAULT_EXECUTOR_DB_PASSWORD:?KUNLUN_VAULT_EXECUTOR_DB_PASSWORD is required}"

if [[ "$KUNLUN_RUNTIME_DB_PASSWORD" == "$KUNLUN_MIGRATOR_DB_PASSWORD" \
   || "$KUNLUN_RUNTIME_DB_PASSWORD" == "$KUNLUN_VAULT_EXECUTOR_DB_PASSWORD" \
   || "$KUNLUN_MIGRATOR_DB_PASSWORD" == "$KUNLUN_VAULT_EXECUTOR_DB_PASSWORD" ]]; then
  echo "数据库角色密码必须两两不同: KUNLUN_RUNTIME_DB_PASSWORD, KUNLUN_MIGRATOR_DB_PASSWORD, KUNLUN_VAULT_EXECUTOR_DB_PASSWORD" >&2
  exit 1
fi

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 \
  --set=db_name="$POSTGRES_DB" \
  --set=runtime_password="$KUNLUN_RUNTIME_DB_PASSWORD" \
  --set=migrator_password="$KUNLUN_MIGRATOR_DB_PASSWORD" \
  --set=executor_password="$KUNLUN_VAULT_EXECUTOR_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE kunlun_runtime LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', :'runtime_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kunlun_runtime') \gexec
SELECT format('CREATE ROLE kunlun_migrator LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', :'migrator_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kunlun_migrator') \gexec
SELECT format('CREATE ROLE kunlun_vault_executor LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', :'executor_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kunlun_vault_executor') \gexec

-- Re-running bootstrap must also repair pre-existing roles created with
-- PostgreSQL's permissive INHERIT default. These options are idempotent.
ALTER ROLE kunlun_runtime NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE kunlun_migrator NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE kunlun_vault_executor NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
DO $$ BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_auth_members AS membership
    JOIN pg_roles AS member ON member.oid = membership.member
    JOIN pg_roles AS role ON role.oid = membership.roleid
    WHERE member.rolname IN ('kunlun_runtime', 'kunlun_migrator', 'kunlun_vault_executor')
       OR role.rolname IN ('kunlun_runtime', 'kunlun_migrator', 'kunlun_vault_executor')
  ) THEN
    RAISE EXCEPTION 'Kunlun database roles must not have membership edges in either direction';
  END IF;
END $$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO kunlun_runtime, kunlun_migrator, kunlun_vault_executor;
GRANT USAGE ON SCHEMA public TO kunlun_runtime;
GRANT USAGE, CREATE ON SCHEMA public TO kunlun_migrator;
GRANT USAGE ON SCHEMA public TO kunlun_vault_executor;

ALTER DEFAULT PRIVILEGES FOR ROLE kunlun_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kunlun_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE kunlun_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO kunlun_runtime;

-- This script is safe both before and after migrations. Custom deployments
-- that create roles late must still remove mutation rights from immutable
-- journals and the migration version table.
SELECT 'REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON ledger_transactions, ledger_entries FROM kunlun_runtime'
WHERE to_regclass('public.ledger_transactions') IS NOT NULL
  AND to_regclass('public.ledger_entries') IS NOT NULL \gexec
SELECT 'GRANT SELECT, INSERT ON ledger_transactions, ledger_entries TO kunlun_runtime'
WHERE to_regclass('public.ledger_transactions') IS NOT NULL
  AND to_regclass('public.ledger_entries') IS NOT NULL \gexec
SELECT 'REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON operator_actions FROM kunlun_runtime'
WHERE to_regclass('public.operator_actions') IS NOT NULL \gexec
SELECT 'GRANT SELECT, INSERT ON operator_actions TO kunlun_runtime'
WHERE to_regclass('public.operator_actions') IS NOT NULL \gexec
SELECT 'REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON alembic_version FROM kunlun_runtime'
WHERE to_regclass('public.alembic_version') IS NOT NULL \gexec
SELECT 'GRANT SELECT ON alembic_version TO kunlun_runtime'
WHERE to_regclass('public.alembic_version') IS NOT NULL \gexec
SQL
