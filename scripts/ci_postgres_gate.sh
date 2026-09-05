#!/usr/bin/env bash
set -euo pipefail

# This gate deliberately uses an isolated local PostgreSQL instance and a
# fake Vault catalog. It proves SQL roles/ACLs/transactions only; it does not
# prove Supabase encryption, provider availability, or production readiness.
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${KUNLUN_RUNTIME_DB_PASSWORD:?KUNLUN_RUNTIME_DB_PASSWORD is required}"
: "${KUNLUN_MIGRATOR_DB_PASSWORD:?KUNLUN_MIGRATOR_DB_PASSWORD is required}"
: "${KUNLUN_VAULT_EXECUTOR_DB_PASSWORD:?KUNLUN_VAULT_EXECUTOR_DB_PASSWORD is required}"
if [[ "${KUNLUN_CI_ISOLATED_DATABASE:-}" != "kunlun-ci-disposable" \
   || "$POSTGRES_DB" != "kunlun_ci" \
   || "${PGHOST:-127.0.0.1}" != "127.0.0.1" ]]; then
  echo "This script requires an explicitly acknowledged disposable local kunlun_ci database." >&2
  exit 1
fi
python_bin="${KUNLUN_CI_PYTHON:-.venv/bin/python}"

root_psql=(psql --host "${PGHOST:-127.0.0.1}" --port "${PGPORT:-5432}" --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1)
runtime_url="postgresql+psycopg://kunlun_runtime:${KUNLUN_RUNTIME_DB_PASSWORD}@${PGHOST:-127.0.0.1}:${PGPORT:-5432}/${POSTGRES_DB}"
migrator_url="postgresql+psycopg://kunlun_migrator:${KUNLUN_MIGRATOR_DB_PASSWORD}@${PGHOST:-127.0.0.1}:${PGPORT:-5432}/${POSTGRES_DB}"
executor_url="postgresql+psycopg://kunlun_vault_executor:${KUNLUN_VAULT_EXECUTOR_DB_PASSWORD}@${PGHOST:-127.0.0.1}:${PGPORT:-5432}/${POSTGRES_DB}"

export KUNLUN_RUNTIME_DB_PASSWORD KUNLUN_MIGRATOR_DB_PASSWORD KUNLUN_VAULT_EXECUTOR_DB_PASSWORD

"${root_psql[@]}" < scripts/ci_fake_supabase_vault.sql
KUNLUN_RUNTIME_DB_PASSWORD="$KUNLUN_RUNTIME_DB_PASSWORD" \
KUNLUN_MIGRATOR_DB_PASSWORD="$KUNLUN_MIGRATOR_DB_PASSWORD" \
KUNLUN_VAULT_EXECUTOR_DB_PASSWORD="$KUNLUN_VAULT_EXECUTOR_DB_PASSWORD" \
scripts/init-postgres-roles.sh

"${root_psql[@]}" < scripts/bootstrap_supabase_vault.sql
KUNLUN_DATABASE_URL="$migrator_url" "$python_bin" -m alembic upgrade head

export KUNLUN_DATABASE_URL="$runtime_url"
export KUNLUN_RUNTIME_DATABASE_URL="$runtime_url"
export KUNLUN_MIGRATOR_DATABASE_URL="$migrator_url"
export KUNLUN_VAULT_EXECUTOR_DATABASE_URL="$executor_url"

"$python_bin" - <<'PY'
from sqlalchemy import create_engine, text
from scripts.preflight import (
    _installation_marker_errors,
    _runtime_permission_errors,
    _supabase_rls_errors,
    _vault_contract_errors,
)

runtime = create_engine(__import__("os").environ["KUNLUN_RUNTIME_DATABASE_URL"])
migrator = create_engine(__import__("os").environ["KUNLUN_MIGRATOR_DATABASE_URL"])
executor = create_engine(__import__("os").environ["KUNLUN_VAULT_EXECUTOR_DATABASE_URL"])
try:
    assert _runtime_permission_errors(runtime, "kunlun_runtime") == []
    assert _supabase_rls_errors(runtime) == []
    assert _vault_contract_errors(runtime, executor, "kunlun_runtime", "kunlun_vault_executor") == []
    assert _installation_marker_errors(
        runtime, migrator, executor,
        "kunlun_runtime", "kunlun_migrator", "kunlun_vault_executor",
    ) == []
    from uuid import uuid4
    from sqlalchemy.orm import Session
    from app.models import User
    from app.security import hash_password
    from app.services.credentials import SupabaseVaultCredentialVault, SecretUnavailable
    user_id, outsider_id = str(uuid4()), str(uuid4())
    with Session(runtime) as session:
        session.add_all([User(id=user_id, email='vault-ci@example.invalid', password_hash=hash_password('ci inert user password')),
                         User(id=outsider_id, email='outsider-ci@example.invalid', password_hash=hash_password('ci inert outsider password'))])
        session.commit()
    vault = SupabaseVaultCredentialVault(executor)
    assert vault.probe()
    first = vault.provision(user_id=user_id, provider='openai', label='CI only', secret='ci-inert-upstream-key')
    binding = dict(user_id=user_id, connection_id=first.id, provider='openai', credential_version=first.credential_version)
    assert vault.get(**binding) == 'ci-inert-upstream-key'
    def must_reject(candidate):
        try:
            vault.get(**candidate)
        except SecretUnavailable:
            return
        raise AssertionError('Vault resolved an invalid tenant/version binding')
    must_reject({**binding, 'user_id': outsider_id})
    must_reject({**binding, 'provider': 'other-provider'})
    second = vault.provision(user_id=user_id, provider='openai', label='CI only', secret='ci-inert-rotated-key')
    assert second.credential_version == first.credential_version + 1
    must_reject(binding)
    current = {**binding, 'connection_id': second.id, 'credential_version': second.credential_version}
    assert vault.get(**current) == 'ci-inert-rotated-key'
    vault.revoke(user_id=user_id, provider='openai')
    must_reject(current)
finally:
    runtime.dispose()
    migrator.dispose()
    executor.dispose()
PY

"$python_bin" - <<'PY'
import os
import psycopg

def connect(role, password):
    return psycopg.connect(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ["POSTGRES_DB"], user=role, password=password,
    )

# Runtime must not read private Vault metadata or call the executor function.
with connect("kunlun_runtime", os.environ["KUNLUN_RUNTIME_DB_PASSWORD"]) as conn:
    for query in (
        "SELECT vault_ref FROM kunlun_private.provider_credential_bindings LIMIT 1",
        "SELECT kunlun_private.credential_resolve_v2(gen_random_uuid(), gen_random_uuid(), 'openai', 1)",
    ):
        try:
            with conn.cursor() as cur:
                cur.execute(query)
        except psycopg.errors.InsufficientPrivilege:
            conn.rollback()
        else:
            raise AssertionError("runtime unexpectedly accessed a private Vault surface")

# Supabase Data API roles remain NOLOGIN; inspect their effective privileges
# using the administrator, rather than granting them extra login capabilities.
with connect(os.environ["POSTGRES_USER"], os.environ.get("PGPASSWORD")) as conn:
    for role in ("anon", "authenticated"):
        with conn.cursor() as cur:
            cur.execute("SELECT has_table_privilege(%s, 'public.users', 'SELECT'), has_any_column_privilege(%s, 'public.users', 'SELECT'), has_function_privilege(%s, 'public.kunlun_installation_id()', 'EXECUTE')", (role, role, role))
            assert cur.fetchone() == (False, False, False), role
PY

# Bidirectional membership is a hard failure: add one edge and prove the
# role bootstrap refuses to continue, then restore the isolated fixture.
"${root_psql[@]}" -c "GRANT kunlun_runtime TO kunlun_migrator"
if KUNLUN_RUNTIME_DB_PASSWORD="$KUNLUN_RUNTIME_DB_PASSWORD" \
  KUNLUN_MIGRATOR_DB_PASSWORD="$KUNLUN_MIGRATOR_DB_PASSWORD" \
  KUNLUN_VAULT_EXECUTOR_DB_PASSWORD="$KUNLUN_VAULT_EXECUTOR_DB_PASSWORD" \
  scripts/init-postgres-roles.sh; then
  echo "role bootstrap unexpectedly accepted a privileged membership edge" >&2
  exit 1
fi
"${root_psql[@]}" -c "REVOKE kunlun_runtime FROM kunlun_migrator"

echo "PostgreSQL 16 isolated role/Vault/ACL gate passed (fake Vault fixture only)."
