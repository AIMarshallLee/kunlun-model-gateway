"""SQLAlchemy engine/session setup for the modular monolith."""

from __future__ import annotations

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, future=True, pool_pre_ping=True, connect_args=connect_args)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()
    return engine


def build_session_factory(engine: Engine):
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


def install_ledger_guards(engine: Engine) -> None:
    """Install defense-in-depth append-only guards for the local SQLite MVP.

    Production PostgreSQL must install equivalent, separately permissioned
    migration-time triggers before the production gate is lifted.
    """
    if engine.dialect.name != "sqlite":
        return
    statements = (
        """CREATE TRIGGER IF NOT EXISTS ledger_entries_no_update
        BEFORE UPDATE ON ledger_entries BEGIN
          SELECT RAISE(ABORT, 'ledger_entries append-only');
        END""",
        """CREATE TRIGGER IF NOT EXISTS ledger_entries_no_delete
        BEFORE DELETE ON ledger_entries BEGIN
          SELECT RAISE(ABORT, 'ledger_entries append-only');
        END""",
        """CREATE TRIGGER IF NOT EXISTS ledger_transactions_no_update
        BEFORE UPDATE ON ledger_transactions BEGIN
          SELECT RAISE(ABORT, 'ledger_transactions append-only');
        END""",
        """CREATE TRIGGER IF NOT EXISTS ledger_transactions_no_delete
        BEFORE DELETE ON ledger_transactions BEGIN
          SELECT RAISE(ABORT, 'ledger_transactions append-only');
        END""",
        """CREATE TRIGGER IF NOT EXISTS operator_actions_no_update
        BEFORE UPDATE ON operator_actions BEGIN
          SELECT RAISE(ABORT, 'operator_actions append-only');
        END""",
        """CREATE TRIGGER IF NOT EXISTS operator_actions_no_delete
        BEFORE DELETE ON operator_actions BEGIN
          SELECT RAISE(ABORT, 'operator_actions append-only');
        END""",
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
