from __future__ import annotations

from scripts.preflight import _runtime_permission_errors, _trigger_guard_sql
from app.db_guards import ledger_trigger_names, operator_audit_trigger_names


def test_trigger_guard_query_covers_every_expected_trigger_and_rejects_disabled():
    ledger_sql = _trigger_guard_sql(
        ("ledger_transactions", "ledger_entries"), ledger_trigger_names(), "ledger_guards"
    )
    audit_sql = _trigger_guard_sql(("operator_actions",), operator_audit_trigger_names(), "audit_guards")

    for name in ledger_trigger_names() + operator_audit_trigger_names():
        assert f"'{name}'" in ledger_sql + audit_sql
    assert "tgenabled <> 'D'" in ledger_sql
    assert "tgenabled <> 'D'" in audit_sql
    assert "COUNT(*) = 5" in ledger_sql
    assert "COUNT(*) = 3" in audit_sql


class _FakeResult:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def one(self):
        return self.row


class _FakeConnection:
    def __init__(self, row):
        self.row = row
        self.statement = None

    def execute(self, statement):
        self.statement = str(statement)
        return _FakeResult(self.row)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeEngine:
    def __init__(self, row):
        self.connection = _FakeConnection(row)

    def connect(self):
        return self.connection


def _permission_row(*, ledger_guards=True, audit_guards=True):
    row = {"current_user": "kunlun_runtime", "ledger_guards": ledger_guards, "audit_guards": audit_guards}
    for key in (
        "ledger_select", "ledger_insert", "entry_select", "entry_insert",
        "audit_select", "audit_insert", "version_select",
    ):
        row[key] = True
    for key in (
        "schema_create", "ledger_update", "ledger_delete", "ledger_truncate", "ledger_references", "ledger_trigger",
        "entry_update", "entry_delete", "entry_truncate", "entry_references", "entry_trigger",
        "audit_update", "audit_delete", "audit_truncate", "audit_references", "audit_trigger",
        "version_insert", "version_update", "version_delete", "version_truncate", "version_references", "version_trigger",
    ):
        row[key] = False
    return row


def test_runtime_permission_preflight_fails_when_ledger_trigger_is_missing_or_disabled():
    ledger_engine = _FakeEngine(_permission_row(ledger_guards=False))
    assert _runtime_permission_errors(ledger_engine, "kunlun_runtime")
    rendered = ledger_engine.connection.statement
    assert "{" not in rendered and "}" not in rendered
    for name in ledger_trigger_names() + operator_audit_trigger_names():
        assert name in rendered
    assert _runtime_permission_errors(_FakeEngine(_permission_row(audit_guards=False)), "kunlun_runtime")
