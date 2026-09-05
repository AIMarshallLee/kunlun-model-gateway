"""Platform supply credentials: deliberately independent from tenant BYOK identities."""

from threading import Lock
from uuid import uuid4

from sqlalchemy import text

from .credentials import SecretUnavailable


def platform_contract_errors(runtime_engine, executor_engine):
    """Read only: fail closed on schema, definer, audit, or effective ACL drift."""
    with runtime_engine.connect() as connection:
        checks = connection.execute(text("""
          WITH functions AS (
            SELECT p.* FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='kunlun_private' AND p.proname IN
              ('platform_channel_write','platform_channel_list','platform_channel_resolve','platform_operation_get')
          ), relations AS (
            SELECT c.* FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='kunlun_private' AND c.relname IN
              ('platform_channels','platform_channel_audits')
          )
          SELECT current_user='kunlun_runtime' AS runtime_identity,
            (SELECT count(*)=4 AND bool_and(
              p.prosecdef AND p.proconfig=ARRAY['search_path=pg_catalog']::text[]
              AND pg_get_userbyid(p.proowner)='kunlun_migrator'
              AND ((p.proname='platform_channel_write' AND oidvectortypes(p.proargtypes)='text, text, text, text, text')
                OR (p.proname='platform_channel_list' AND oidvectortypes(p.proargtypes)='')
                OR (p.proname IN ('platform_channel_resolve','platform_operation_get') AND oidvectortypes(p.proargtypes)='text'))
              AND has_function_privilege('kunlun_vault_executor',p.oid,'EXECUTE')
              AND NOT has_function_privilege('kunlun_runtime',p.oid,'EXECUTE')
              AND NOT has_function_privilege('anon',p.oid,'EXECUTE')
              AND NOT has_function_privilege('authenticated',p.oid,'EXECUTE')
            ) FROM functions p) AS secure_functions,
            (SELECT count(*)=2 AND bool_and(pg_get_userbyid(c.relowner)='kunlun_migrator')
              FROM relations c) AS protected_relations,
            NOT EXISTS (
              SELECT 1 FROM relations c CROSS JOIN pg_roles r
              WHERE r.rolname IN ('kunlun_runtime','kunlun_vault_executor','anon','authenticated')
              AND (has_table_privilege(r.rolname,c.oid,'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
                OR has_any_column_privilege(r.rolname,c.oid,'SELECT, INSERT, UPDATE, REFERENCES'))
            ) AS no_direct_access,
            (SELECT count(*)=1 FROM pg_trigger t
              JOIN relations c ON c.oid=t.tgrelid
              JOIN pg_proc p ON p.oid=t.tgfoid
              WHERE c.relname='platform_channel_audits' AND t.tgname='platform_audit_immutable'
                AND t.tgenabled IN ('O','A') AND t.tgtype=58
                AND p.proname='platform_audit_immutable'
                AND p.pronamespace=c.relnamespace
                AND pg_get_userbyid(p.proowner)='kunlun_migrator'
                AND p.proconfig=ARRAY['search_path=pg_catalog']::text[]
            ) AS immutable_audit
        """)).mappings().one()
    with executor_engine.connect() as connection:
        executor = connection.execute(text("""
          SELECT current_user='kunlun_vault_executor'
            AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
            AND NOT rolinherit AND NOT rolreplication AND NOT rolbypassrls
            AND NOT EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member=pg_roles.oid OR m.roleid=pg_roles.oid)
          FROM pg_roles WHERE rolname=current_user
        """)).scalar_one()
    return [] if all(checks.values()) and executor else ["平台 Vault 函数、隔离权限或追加审计契约不完整"]


class InMemoryPlatformVault:
    """Explicit test injection only; never constructed from environment settings."""
    def __init__(self):
        self._channels = {}
        self._operations = {}
        self._lock = Lock()

    def write(self, *, provider, secret, operation_id, actor, reason):
        with self._lock:
            if operation_id in self._operations:
                raise SecretUnavailable("operation already recorded")
            previous = self._channels.get(provider, {"id": str(uuid4()), "version": 0})
            item = {"id": previous["id"], "provider": provider, "version": previous["version"] + 1,
                    "active": secret is not None, "secret": secret, "pending_cleanup": False}
            self._channels[provider] = item
            from ..security import utcnow
            self._operations[operation_id] = dict(operation_id=operation_id, provider=provider, actor=actor,
                reason=reason, action="provision" if secret is not None else "revoke", created_at=utcnow().isoformat())
            return {k: v for k, v in item.items() if k != "secret"}

    def list(self):
        with self._lock:
            return [{k: v for k, v in item.items() if k != "secret"} for item in self._channels.values()]

    def resolve(self, provider):
        with self._lock:
            item = self._channels.get(provider)
            if not item or not item["active"] or not item["secret"]:
                raise SecretUnavailable("platform channel unavailable")
            return item["secret"], item["id"], item["version"]

    def probe(self):
        return True

    def operation(self, operation_id):
        with self._lock:
            result = self._operations.get(operation_id)
            return dict(result) if result else None


class SupabasePlatformVault:
    def __init__(self, engine):
        self.engine = engine

    def write(self, *, provider, secret, operation_id, actor, reason):
        try:
            with self.engine.begin() as connection:
                result = connection.execute(text("SELECT kunlun_private.platform_channel_write(:provider,:secret,:operation_id,:actor,:reason)"),
                    dict(provider=provider, secret=secret, operation_id=operation_id, actor=actor, reason=reason)).scalar_one()
            return result
        except Exception as exc:
            raise SecretUnavailable("platform credential operation failed; inspect original operation ID") from exc

    def list(self):
        try:
            with self.engine.connect() as connection:
                return connection.execute(text("SELECT kunlun_private.platform_channel_list()")).scalar_one()
        except Exception as exc:
            raise SecretUnavailable("platform catalog unavailable") from exc

    def resolve(self, provider):
        try:
            with self.engine.connect() as connection:
                row = connection.execute(text("SELECT * FROM kunlun_private.platform_channel_resolve(:provider)"), {"provider": provider}).one()
            return tuple(row)
        except Exception as exc:
            raise SecretUnavailable("platform credential unavailable") from exc

    def probe(self):
        try:
            self.list()
            return True
        except SecretUnavailable:
            return False

    def operation(self, operation_id):
        try:
            with self.engine.connect() as connection:
                return connection.execute(text("SELECT kunlun_private.platform_operation_get(:operation_id)"),
                                          {"operation_id": operation_id}).scalar_one()
        except Exception as exc:
            raise SecretUnavailable("platform operation unavailable") from exc
