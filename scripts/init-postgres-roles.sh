#!/usr/bin/env bash
set -euo pipefail

: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${KUNLUN_RUNTIME_DB_PASSWORD:?KUNLUN_RUNTIME_DB_PASSWORD is required}"
: "${KUNLUN_MIGRATOR_DB_PASSWORD:?KUNLUN_MIGRATOR_DB_PASSWORD is required}"

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 \
  --set=db_name="$POSTGRES_DB" \
  --set=runtime_password="$KUNLUN_RUNTIME_DB_PASSWORD" \
  --set=migrator_password="$KUNLUN_MIGRATOR_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE kunlun_runtime LOGIN PASSWORD %L', :'runtime_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kunlun_runtime') \gexec
SELECT format('CREATE ROLE kunlun_migrator LOGIN PASSWORD %L', :'migrator_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kunlun_migrator') \gexec

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO kunlun_runtime, kunlun_migrator;
GRANT USAGE ON SCHEMA public TO kunlun_runtime;
GRANT USAGE, CREATE ON SCHEMA public TO kunlun_migrator;

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
