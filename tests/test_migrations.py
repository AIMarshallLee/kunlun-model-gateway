from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db_guards import (
    SCHEMA_HEAD,
    assert_schema_revision,
    assert_safe_downgrade,
    ledger_trigger_names,
)
from app.config import Settings
from app.db import Base
from app import create_app


ROOT = Path(__file__).resolve().parents[1]


def test_production_requires_alembic_revision(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.sqlite'}")
    with pytest.raises(RuntimeError, match="schema revision"):
        assert_schema_revision(engine, "0001_initial")


def test_schema_revision_must_match_exactly(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'version.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version(version_num) VALUES ('0000_old')"))
    with pytest.raises(RuntimeError, match="schema revision"):
        assert_schema_revision(engine, "0001_initial")


def test_schema_revision_accepts_expected_revision(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'version-ok.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version(version_num) VALUES ('0001_initial')"))
    assert_schema_revision(engine, "0001_initial") is None


def test_downgrade_is_blocked_without_explicit_maintenance_flag():
    with pytest.raises(RuntimeError, match="downgrade"):
        assert_safe_downgrade("production", False)
    assert_safe_downgrade("development", False) is None
    assert_safe_downgrade("production", True) is None


def test_initial_migration_contains_all_model_tables_and_append_only_guards():
    source = (ROOT / "alembic" / "versions" / "0001_initial.py").read_text()
    assert "revision: str = '0001_initial'" in source
    for table in (
        "users", "access_sessions", "api_keys", "wallets", "ledger_transactions",
        "ledger_entries", "payment_orders", "payment_webhook_events", "budgets",
        "model_prices", "model_requests", "provider_attempts", "rate_limit_counters",
        "auth_rate_limit_counters", "outbox_events", "operator_actions",
    ):
        assert f'"{table}"' in source or f"'{table}'" in source
    for trigger in ledger_trigger_names():
        assert trigger in source
    assert "kunlun_check_ledger_transaction_balance" in source
    assert "CREATE CONSTRAINT TRIGGER" in source
    assert source.count("sa.BigInteger()") >= 12


def test_initial_migration_has_reversible_boundary():
    source = (ROOT / "alembic" / "versions" / "0001_initial.py").read_text()
    assert "assert_safe_downgrade" in source
    assert "allow_destructive_downgrade" in source


def test_production_hardening_migration_declares_all_money_and_audit_changes():
    source = (ROOT / "alembic" / "versions" / "0002_production_hardening.py").read_text()
    assert "revision: str = '0002_production_hardening'" in source
    assert "down_revision: Union[str, None] = '0001_initial'" in source
    assert "amount_microusd" in source and "credit_amount_microusd" in source
    for column in (
        "payment_amount_minor", "payment_currency", "checkout_url", "quote_numerator", "quote_denominator",
        "quote_id", "client_idempotency_key", "risk_reason", "refunded_at",
        "event_type", "actor", "scopes", "token_id", "operation_id", "source_ip_digest",
        "before_status", "after_status",
    ):
        assert column in source
    for table in ("payment_refunds", "safety_audits"):
        assert table in source
    assert "nonce" in source and "uq_payment_provider_nonce" in source
    assert "DEFERRABLE INITIALLY DEFERRED" in source
    assert "ledger_entries_no_update" in source
    assert "ledger_entries_balance_deferred" in source


def test_refund_claim_lease_migration_is_chained_and_backfilled():
    source = (ROOT / "alembic" / "versions" / "0003_refund_claim_lease.py").read_text()
    assert 'revision: str = "0003_refund_claim_lease"' in source
    assert 'down_revision: Union[str, None] = "0002_production_hardening"' in source
    assert "claim_started_at" in source
    assert "created_at" in source


def test_single_full_refund_migration_is_chained_and_unique():
    source = (ROOT / "alembic" / "versions" / "0004_single_full_refund.py").read_text()
    assert 'revision: str = "0004_single_full_refund"' in source
    assert 'down_revision: Union[str, None] = "0003_refund_claim_lease"' in source
    assert "uq_refund_order" in source
    assert '["order_id"]' in source
    assert "HAVING COUNT(*) > 1" in source
    assert "请先人工核对并修复后再迁移" in source


def test_checkout_claim_lease_migration_is_chained_and_backfilled():
    source = (ROOT / "alembic" / "versions" / "0005_checkout_claim_lease.py").read_text()
    assert 'revision: str = "0005_checkout_claim_lease"' in source
    assert 'down_revision: Union[str, None] = "0004_single_full_refund"' in source
    assert "checkout_claim_started_at" in source


def test_operator_audit_target_migration_is_chained_and_append_only():
    source = (ROOT / "alembic" / "versions" / "0006_operator_audit_targets.py").read_text()
    assert 'revision: str = "0006_operator_audit_targets"' in source
    assert 'down_revision: Union[str, None] = "0005_checkout_claim_lease"' in source
    assert "target_type" in source and "target_id" in source
    for trigger in (
        "operator_actions_no_update",
        "operator_actions_no_delete",
        "operator_actions_no_truncate",
    ):
        assert trigger in source
    assert "REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER" in source


def test_payment_reconciliation_claim_migration_is_chained():
    source = (ROOT / "alembic" / "versions" / "0007_payment_reconciliation_claim.py").read_text()
    assert 'revision: str = "0007_payment_reconcile_claim"' in source
    assert 'down_revision: Union[str, None] = "0006_operator_audit_targets"' in source
    assert "reconciliation_claim_started_at" in source


def test_all_revision_identifiers_fit_alembic_version_column():
    import ast

    for migration in sorted((ROOT / "alembic" / "versions").glob("[0-9]*.py")):
        tree = ast.parse(migration.read_text(encoding="utf-8"))
        revision = next(
            node.value.value
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "revision"
            and isinstance(node.value, ast.Constant)
        )
        assert len(revision) <= 32, f"{migration.name} revision 超过 Alembic 默认 VARCHAR(32)"


def test_upgrade_from_initial_to_production_hardening_on_sqlite(tmp_path):
    # The application intentionally has a local ``alembic`` script package;
    # load the installed migration runner without allowing that package to
    # shadow the dependency.
    import importlib
    import sys

    project_paths = {"", str(ROOT), str(ROOT / "tests")}
    original_path = list(sys.path)
    sys.path[:] = [item for item in sys.path if item not in project_paths]
    try:
        command = importlib.import_module("alembic.command")
        config_cls = importlib.import_module("alembic.config").Config
    except ModuleNotFoundError as exc:
        pytest.skip(f"alembic dependency not installed: {exc}")
    finally:
        sys.path[:] = original_path
    db_url = f"sqlite:///{tmp_path / 'migration.sqlite'}"
    cfg = config_cls(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    tables = set(inspect(engine).get_table_names())
    assert {"payment_refunds", "safety_audits"}.issubset(tables)
    refund_columns = {item["name"] for item in inspect(engine).get_columns("payment_refunds")}
    assert "claim_started_at" in refund_columns
    payment_columns = {item["name"] for item in inspect(engine).get_columns("payment_orders")}
    assert "credit_amount_microusd" in payment_columns
    assert "amount_microusd" not in payment_columns
    assert "currency" not in payment_columns
    assert {"payment_amount_minor", "checkout_url", "quote_numerator", "client_idempotency_key", "risk_reason"}.issubset(payment_columns)
    assert "checkout_claim_started_at" in payment_columns
    assert "reconciliation_claim_started_at" in payment_columns
    webhook_columns = {item["name"] for item in inspect(engine).get_columns("payment_webhook_events")}
    assert "nonce" in webhook_columns
    operator_columns = {item["name"] for item in inspect(engine).get_columns("operator_actions")}
    assert {
        "actor", "scopes", "token_id", "operation_id", "source_ip_digest",
        "target_type", "target_id",
    }.issubset(operator_columns)
    # Keep the checked-in migration at parity with ORM metadata. Server
    # defaults are intentionally ignored because the ORM owns application
    # defaults while the migration keeps only backfill-safe defaults.
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={
            "compare_type": True,
            "compare_server_default": False,
        })
        assert compare_metadata(context, Base.metadata) == []


def test_production_startup_requires_exact_head_and_never_runs_create_all(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'production-startup.sqlite'}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version(version_num) VALUES (:head)"), {"head": SCHEMA_HEAD})
    engine.dispose()
    settings = Settings(
        environment="production",
        database_url=db_url,
        api_key_pepper="a" * 32,
        session_pepper="b" * 32,
        api_key_pepper_persisted=True,
        session_pepper_persisted=True,
    )
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls, **kwargs: settings))
    monkeypatch.setattr(Base.metadata, "create_all", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("create_all must not run")))
    app = create_app()
    app.state.engine.dispose()
