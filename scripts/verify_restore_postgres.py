"""Fresh-target restore rehearsal for synthetic CI PostgreSQL, NOT Supabase DR.

Run after ci_postgres_gate.sh on its quiescent, disposable cluster. Existing
targets are never overwritten or dropped. Same-cluster roles already exist;
this does not test restoring cluster roles or real Vault encryption keys.
"""

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db_guards import KUNLUN_BUSINESS_TABLES, SCHEMA_HEAD, assert_schema_revision
from app.models import ApiKey, ModelPrice, User, Wallet
from app.security import utcnow
from app.services.gateway_billing import mark_pending_reconciliation, record_attempt, reserve_model_request
from app.services.ledger import CUSTOMER_AVAILABLE, PLATFORM_CLEARING, post_transaction
from app.services.platform_credentials import platform_contract_errors
from scripts.preflight import (
    _installation_marker_errors, _runtime_permission_errors,
    _supabase_rls_errors, _vault_contract_errors,
)


def ci_config(env):
    error = "Requires acknowledged disposable local kunlun_ci with inert CI credentials"
    required = ("PGPASSWORD", "KUNLUN_RUNTIME_DB_PASSWORD", "KUNLUN_MIGRATOR_DB_PASSWORD",
                "KUNLUN_VAULT_EXECUTOR_DB_PASSWORD")
    if (env.get("KUNLUN_CI_ISOLATED_DATABASE") != "kunlun-ci-disposable"
            or env.get("PGHOST") != "127.0.0.1" or env.get("POSTGRES_DB") != "kunlun_ci"
            or env.get("POSTGRES_USER") != "postgres" or any(not env.get(k) for k in required)
            or any(env.get(k) for k in ("PGSERVICE", "PGSERVICEFILE", "PGHOSTADDR", "PGOPTIONS"))):
        raise ValueError(error)
    try:
        port = int(env.get("PGPORT", "5432"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ValueError(error) from None
    return {"port": port, "source": "kunlun_ci", "target": "kunlun_restore_ci"}


def run_tool(args, env):
    try:
        subprocess.run(args, env=env, check=True, capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError):
        # Never echo pg_restore SQL errors, archive contents, or DSNs.
        raise RuntimeError(f"{args[0]} failed; isolated target is retained for inspection") from None


def snapshot(engine):
    with engine.connect() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        tables = db.execute(text("""
            SELECT schemaname, tablename FROM pg_tables
            WHERE schemaname IN ('public', 'kunlun_private', 'vault')
            ORDER BY schemaname, tablename
        """)).all()
        quote = engine.dialect.identifier_preparer.quote
        return {f"{schema}.{table}": sorted(db.execute(text(
            f"SELECT row_to_json(t)::text FROM {quote(schema)}.{quote(table)} AS t"
        )).scalars()) for schema, table in tables}


def compare_snapshots(source, target):
    if source.keys() != target.keys() or any(source[k] != target[k] for k in source):
        raise RuntimeError("Restored table inventory or row contents differ; values are not logged")
    return len(source), sum(map(len, source.values()))


def seed_uncertain_request(runtime):
    """An inert unknown-cost attempt must survive without releasing its holds."""
    user, key, model = str(uuid4()), uuid4().hex, "restore-ci-" + uuid4().hex
    with Session(runtime) as db:
        db.add(User(id=user, email=f"{key}@example.invalid", password_hash="not-a-login",
                    status="active", email_verified_at=utcnow()))
        db.flush()
        db.add(ApiKey(id=key, user_id=user, name="Restore CI", secret_digest="0" * 64, last_four="test"))
        db.add(Wallet(user_id=user, balance_microusd=100000))
        db.add(ModelPrice(id=str(uuid4()), model=model, version=1, active=True,
                          input_microusd_per_million=1000000, output_microusd_per_million=1000000))
        db.flush()
        post_transaction(db, user_id=user, kind="restore_ci_seed", reference=key,
                         idempotency_key=f"restore:{key}",
                         entries=[(CUSTOMER_AVAILABLE, 100000), (PLATFORM_CLEARING, -100000)])
        db.commit()
        hold = reserve_model_request(db, user_id=user, api_key_id=key, model=model,
            billable_payload={"messages": [{"role": "user", "content": "Synthetic restore fixture"}]},
            max_output_tokens=32, idempotency_key=f"restore-call:{key}",
            managed_cost_prices=(500000, 500000), platform_daily_limit=6000)
        attempt = record_attempt(db, request_id=hold.request_id, ordinal=1, provider="ci-synthetic",
                                 model=model, status="running")
        mark_pending_reconciliation(db, hold.request_id, "ci_uncertain", attempt_id=attempt)
    return hold.request_id


def verify_restored_contract(runtime, migrator, executor, request_id):
    assert_schema_revision(runtime, SCHEMA_HEAD)
    errors = _runtime_permission_errors(runtime, "kunlun_runtime")
    errors += _supabase_rls_errors(runtime)
    errors += _vault_contract_errors(runtime, executor, "kunlun_runtime", "kunlun_vault_executor")
    errors += platform_contract_errors(runtime, executor)
    errors += _installation_marker_errors(runtime, migrator, executor,
        "kunlun_runtime", "kunlun_migrator", "kunlun_vault_executor")
    if errors:
        raise RuntimeError("Restored role, RLS, immutable audit, or Vault contract failed")
    with runtime.connect() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        if db.execute(text("""SELECT transaction_id FROM ledger_entries
                GROUP BY transaction_id HAVING SUM(amount_microusd) != 0 LIMIT 1""")).first():
            raise RuntimeError("Restored ledger is not balanced")
        row = db.execute(text("""
            SELECT r.status, r.cost_state, r.reserved_microusd, r.platform_reserved_microusd,
                   w.reserved_microusd AS wallet_hold, a.billing_status
            FROM model_requests r JOIN wallets w ON w.user_id=r.user_id
            JOIN provider_attempts a ON a.request_id=r.id
            WHERE r.id=:id
        """), {"id": request_id}).mappings().one()
        if (row["status"] != "pending_reconciliation" or row["cost_state"] != "pending_reconciliation"
                or row["billing_status"] != "unknown" or row["reserved_microusd"] <= 0
                or row["platform_reserved_microusd"] <= 0 or row["wallet_hold"] != row["reserved_microusd"]):
            raise RuntimeError("Unknown-cost state or reservations were lost during restore")


def main():
    env = dict(os.environ)
    config = ci_config(env)  # Before DB connection, fixture writes or filesystem changes.
    password_vars = {"postgres": "PGPASSWORD", "kunlun_runtime": "KUNLUN_RUNTIME_DB_PASSWORD",
                     "kunlun_migrator": "KUNLUN_MIGRATOR_DB_PASSWORD",
                     "kunlun_vault_executor": "KUNLUN_VAULT_EXECUTOR_DB_PASSWORD"}
    engines = []
    def engine(role, database):
        result = create_engine(URL.create("postgresql+psycopg", username=role,
            password=env[password_vars[role]], host="127.0.0.1", port=config["port"], database=database),
            connect_args={"connect_timeout": 5, "options": "-c statement_timeout=30000"})
        engines.append(result)
        return result
    started = monotonic()
    try:
        admin = engine("postgres", config["source"])
        with admin.connect() as db:
            if db.scalar(text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": config["target"]}):
                raise RuntimeError("Restore target already exists; refusing to overwrite or drop it")
        runtime = engine("kunlun_runtime", config["source"])
        request_id = seed_uncertain_request(runtime)
        before = snapshot(admin)
        required = {f"public.{name}" for name in KUNLUN_BUSINESS_TABLES}
        if not required <= before.keys() or not all(before.get(name) for name in (
                "public.ledger_entries", "public.payment_orders", "public.payment_chargebacks",
                "public.operator_actions", "public.outbox_events", "kunlun_private.installation_marker")):
            raise RuntimeError("Run ci_postgres_gate.sh first; complete synthetic source fixture required")
        cli_env = {k: v for k, v in env.items() if not k.startswith("PG")}
        cli_env.update(PGPASSWORD=env["PGPASSWORD"], PGCONNECT_TIMEOUT="5")
        common = ["--host", "127.0.0.1", "--port", str(config["port"]), "--username", "postgres", "--no-password"]
        # TemporaryDirectory is mode 0700. Dump is explicitly mode 0600 before pg_dump opens it.
        with TemporaryDirectory(prefix="kunlun-restore-ci-") as folder:
            archive = Path(folder) / "synthetic.dump"
            archive.touch(mode=0o600)
            run_tool(["pg_dump", *common, "--format=custom", "--file", str(archive), config["source"]], cli_env)
            run_tool(["createdb", *common, "--maintenance-db", config["source"],
                      "--template=template0", config["target"]], cli_env)
            run_tool(["pg_restore", *common, "--exit-on-error", "--single-transaction",
                      "--dbname", config["target"], str(archive)], cli_env)
        after = snapshot(engine("postgres", config["target"]))
        tables, rows = compare_snapshots(before, after)
        verify_restored_contract(engine("kunlun_runtime", config["target"]),
            engine("kunlun_migrator", config["target"]), engine("kunlun_vault_executor", config["target"]), request_id)
        print(f"Isolated restore passed: {tables} tables, {rows} identical synthetic rows, {monotonic()-started:.1f}s.")
        print("Verified schema/ACL/RLS/immutable audits, balanced ledger and unknown-cost holds. "
              "Fresh target retained; temporary archive removed. NOT a production RTO or Supabase encryption proof.")
        return 0
    finally:
        for item in engines:
            item.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # SQL exceptions can include parameters. Do not put them into CI logs.
        print("Isolated restore gate failed; verify disposable target, fixture and contracts. "
              "No existing target was overwritten; any newly created target is retained.", file=sys.stderr)
        raise SystemExit(1)
