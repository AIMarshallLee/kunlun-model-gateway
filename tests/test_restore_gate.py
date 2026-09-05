from pathlib import Path
import os
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("script", ["backup_postgres.sh", "restore_postgres.sh"])
def test_retired_production_helpers_never_invoke_docker_or_touch_backup(tmp_path, script):
    marker = tmp_path / "docker-called"
    docker = tmp_path / "docker"
    docker.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    docker.chmod(0o700)
    backup = tmp_path / "backup.dump"
    if script.startswith("restore"):
        backup.write_bytes(b"inert archive")
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
           "BACKUP_FILE": str(backup), "POSTGRES_USER": "postgres",
           "POSTGRES_DB": "production", "CONFIRM_RESTORE": "YES_RESTORE_PRODUCTION"}
    result = subprocess.run(["bash", str(ROOT / "scripts" / script)], env=env, capture_output=True)
    assert result.returncode == 2
    assert b"RESTORE-ACCEPTANCE.md" in result.stderr
    assert not marker.exists()
    if script.startswith("restore"):
        assert backup.read_bytes() == b"inert archive"
    else:
        assert not backup.exists()


def ci_env():
    return {"KUNLUN_CI_ISOLATED_DATABASE": "kunlun-ci-disposable", "PGHOST": "127.0.0.1",
            "PGPORT": "55439", "POSTGRES_DB": "kunlun_ci", "POSTGRES_USER": "postgres",
            "PGPASSWORD": "inert-admin", "KUNLUN_RUNTIME_DB_PASSWORD": "inert-runtime",
            "KUNLUN_MIGRATOR_DB_PASSWORD": "inert-migrator",
            "KUNLUN_VAULT_EXECUTOR_DB_PASSWORD": "inert-executor"}


@pytest.mark.parametrize("key,value", [
    ("KUNLUN_CI_ISOLATED_DATABASE", ""), ("PGHOST", "remote.example"),
    ("POSTGRES_DB", "production"), ("POSTGRES_USER", "owner"),
    ("PGPORT", "0"), ("PGPORT", "65536"), ("PGPORT", "not-a-port"),
    ("PGSERVICE", "remote"), ("PGSERVICEFILE", "/tmp/service"),
    ("PGHOSTADDR", "192.0.2.1"), ("PGOPTIONS", "-c role=owner"),
    ("PGPASSWORD", ""), ("KUNLUN_RUNTIME_DB_PASSWORD", ""),
])
def test_restore_guard_rejects_unsafe_environment(key, value):
    from scripts.verify_restore_postgres import ci_config
    with pytest.raises(ValueError, match="disposable"):
        ci_config({**ci_env(), key: value})


def test_restore_config_keeps_credentials_out_of_command_arguments():
    from scripts.verify_restore_postgres import ci_config
    config = ci_config(ci_env())
    assert config["port"] == 55439
    assert config["source"] == "kunlun_ci"
    assert config["target"] == "kunlun_restore_ci"


@pytest.mark.parametrize("target", [{}, {"public.wallets": ["changed"]},
                                  {"public.wallets": ["sensitive-fixture"], "extra": []}])
def test_snapshot_mismatch_rejects_loss_change_or_extra_data_without_echoing_rows(target):
    from scripts.verify_restore_postgres import compare_snapshots
    with pytest.raises(RuntimeError) as error:
        compare_snapshots({"public.wallets": ["sensitive-fixture"]}, target)
    assert "sensitive-fixture" not in str(error.value)


def test_snapshot_comparison_accepts_identical_empty_and_populated_tables():
    from scripts.verify_restore_postgres import compare_snapshots
    assert compare_snapshots({"a": [], "b": ["row"]}, {"b": ["row"], "a": []}) == (2, 1)


def test_external_tool_failure_is_redacted(monkeypatch):
    from scripts.verify_restore_postgres import run_tool
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="secret row / password")
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="pg_restore failed") as error:
        run_tool(["pg_restore", "--single-transaction"], ci_env())
    assert "secret" not in str(error.value)


def test_existing_restore_target_stops_before_fixture_or_dump(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from scripts import verify_restore_postgres as gate
    monkeypatch.setattr(gate.os, "environ", ci_env())
    admin = SimpleNamespace(connect=lambda: nullcontext(SimpleNamespace(scalar=lambda *a: 1)), dispose=lambda: None)
    monkeypatch.setattr(gate, "create_engine", lambda *a, **kw: admin)
    monkeypatch.setattr(gate, "seed_uncertain_request", lambda *a: pytest.fail("source fixture changed"))
    monkeypatch.setattr(gate, "run_tool", lambda *a: pytest.fail("external tool invoked"))
    with pytest.raises(RuntimeError, match="already exists"):
        gate.main()


def test_restore_command_sequence_preserves_acl_and_never_cleans_target(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from scripts import verify_restore_postgres as gate
    monkeypatch.setattr(gate.os, "environ", ci_env())
    admin = SimpleNamespace(connect=lambda: nullcontext(SimpleNamespace(scalar=lambda *a: None)), dispose=lambda: None)
    monkeypatch.setattr(gate, "create_engine", lambda *a, **kw: admin)
    monkeypatch.setattr(gate, "seed_uncertain_request", lambda *a: "inert-request")
    rows = {f"public.{name}": ["inert"] for name in gate.KUNLUN_BUSINESS_TABLES}
    rows["kunlun_private.installation_marker"] = ["inert-marker"]
    monkeypatch.setattr(gate, "snapshot", lambda *a: rows)
    monkeypatch.setattr(gate, "verify_restored_contract", lambda *a: None)
    calls = []
    def run(args, env):
        assert "inert-admin" not in " ".join(args)
        assert env["PGPASSWORD"] == "inert-admin"
        if args[0] == "pg_dump":
            archive = Path(args[args.index("--file") + 1])
            assert archive.stat().st_mode & 0o777 == 0o600
            assert archive.parent.stat().st_mode & 0o777 == 0o700
        calls.append(args)
    monkeypatch.setattr(gate, "run_tool", run)
    assert gate.main() == 0
    assert [call[0] for call in calls] == ["pg_dump", "createdb", "pg_restore"]
    assert "--template=template0" in calls[1]
    assert "--single-transaction" in calls[2] and "--exit-on-error" in calls[2]
    assert not {"--clean", "--if-exists", "--no-acl", "--no-owner", "--create"} & set(sum(calls, []))
    archive = Path(calls[0][calls[0].index("--file") + 1])
    assert not archive.exists()
