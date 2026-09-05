"""Two-process HTTP load and hard-stop/replacement on synthetic PostgreSQL CI.

This measures local fixture performance, not Vercel capacity or a production
deployment/rollback. All requests and provider replies are synthetic.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import timedelta
import json
from math import ceil
import os
from pathlib import Path
import socket
import subprocess
import sys
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from runtime_ci_fixture import MODEL, PEPPER, fixture_database_url
from app.db import build_engine, build_session_factory
from app.models import ApiKey, LedgerEntry, ModelRequest, PlatformDailyBudget, ProviderAttempt, User, Wallet
from app.security import issue_api_key, token_digest, utcnow
from app.services.ledger import CUSTOMER_AVAILABLE, PLATFORM_CLEARING, post_transaction
from scripts.maintenance import run_once


def latency_summary(seconds):
    if not seconds or any(value < 0 for value in seconds):
        raise ValueError("Nonempty measured durations required")
    values = sorted(seconds)
    return {"count": len(values), "p50_ms": round(values[ceil(len(values) * .5) - 1] * 1000, 2),
            "p95_ms": round(values[ceil(len(values) * .95) - 1] * 1000, 2),
            "max_ms": round(values[-1] * 1000, 2)}


def stop_child(process, *, crash=False):
    if process.poll() is None:
        process.kill() if crash else process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@contextmanager
def server(url):
    # The inherited bound socket avoids a free-port race or accidentally
    # sending fixture credentials to some other local service.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    instance = uuid4().hex
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT),
           "KUNLUN_CI_ISOLATED_DATABASE": "kunlun-ci-disposable",
           "KUNLUN_RUNTIME_DATABASE_URL": url.render_as_string(hide_password=False),
           "KUNLUN_RUNTIME_INSTANCE": instance}
    process = None
    try:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "runtime_ci_fixture:create_fixture",
            "--factory", "--app-dir", str(ROOT / "tests"), "--fd", str(listener.fileno()),
            "--no-access-log", "--no-proxy-headers", "--log-level", "error"], cwd=ROOT, env=env,
            pass_fds=(listener.fileno(),), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            deadline = monotonic() + 20
            while monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Isolated API child failed during startup")
                try:
                    if client.get("/__fixture__/state").json().get("instance") == instance:
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                sleep(.1)
            else:
                raise RuntimeError("Isolated API child startup deadline exceeded")
            yield process, client
    finally:
        listener.close()
        if process is not None:
            stop_child(process)


def seed_principal(engine, *, funded=True):
    user = str(uuid4())
    raw, parsed = issue_api_key()
    amount = 100000 if funded else 0
    with Session(engine) as db:
        db.add(User(id=user, email=f"{user}@example.invalid", password_hash="not-a-login",
                    status="active", email_verified_at=utcnow()))
        db.flush()
        db.add(ApiKey(id=parsed.key_id, user_id=user, name="Runtime CI",
                      secret_digest=token_digest(parsed.secret, PEPPER), last_four=raw[-4:]))
        db.add(Wallet(user_id=user, balance_microusd=amount))
        db.flush()
        if amount:
            post_transaction(db, user_id=user, kind="runtime_ci_seed", reference=user,
                idempotency_key=f"runtime-ci:{user}",
                entries=[(CUSTOMER_AVAILABLE, amount), (PLATFORM_CLEARING, -amount)])
        db.commit()
    return user, {"Authorization": "Bearer " + raw}


def call(client, headers, operation, *, crash=False):
    start = monotonic()
    response = client.post("/v1/chat/completions", headers={**headers, "Idempotency-Key": operation},
        json={"model": MODEL, "max_tokens": 16, "messages": [{"role": "user", "content":
            "ci-block-until-process-death" if crash else "synthetic runtime load"}]})
    return response, monotonic() - start


def load_phase(clients, principals, prefix):
    durations = []
    # Fixed, bounded workload. No arbitrary URL or open-ended stress loop.
    with ThreadPoolExecutor(max_workers=8) as pool:
        for batch in range(10):
            futures = [pool.submit(call, clients[i % 2], principals[i % 2][1], f"{prefix}:{batch}:{i}")
                       for i in range(8)]
            for future in futures:
                response, elapsed = future.result(timeout=15)
                if response.status_code != 200:
                    raise RuntimeError(f"Synthetic load expected 200, received {response.status_code}")
                durations.append(elapsed)
    return durations


def main():
    url = fixture_database_url(os.environ)
    engine = build_engine(url.render_as_string(hide_password=False))
    run = uuid4().hex
    try:
        with Session(engine) as db:
            budget = db.get(PlatformDailyBudget, utcnow().date().isoformat())
            if budget is None:
                raise RuntimeError("Run ci_postgres_gate.sh first; a frozen synthetic day ceiling is required")
            frozen_ceiling = budget.limit_microusd
            spent_before = budget.spent_microusd
        principals = [seed_principal(engine), seed_principal(engine)]
        empty = seed_principal(engine, funded=False)
        with ExitStack() as stack:
            process_a, client_a = stack.enter_context(server(url))
            _, client_b = stack.enter_context(server(url))
            durations = load_phase([client_a, client_b], principals, run + ":before")
            counts = [c.get("/__fixture__/state").json()["calls"] for c in (client_a, client_b)]
            assert call(client_b, empty[1], run + ":no-credit")[0].status_code == 402
            assert counts == [c.get("/__fixture__/state").json()["calls"] for c in (client_a, client_b)]
            duplicate = run + ":duplicate"
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda i: call([client_a, client_b][i % 2], principals[0][1], duplicate)[0], range(16)))
            assert sorted(r.status_code for r in results) == [200] + [409] * 15
            # The same business identifier is private to its tenant.
            original_id = next(r.json()["error"]["request_id"] for r in results if r.status_code == 409)
            assert client_b.get(f"/v1/requests/{original_id}", headers=principals[1][1]).status_code == 404

            crash_operation = run + ":crash"
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(call, client_a, principals[0][1], crash_operation, crash=True)
                deadline = monotonic() + 10
                while monotonic() < deadline:
                    if client_a.get("/__fixture__/state").json()["blocked"]:
                        break
                    sleep(.05)
                else:
                    raise RuntimeError("Synthetic upstream was not entered before crash deadline")
                with Session(engine) as db:
                    item = db.scalar(select(ModelRequest).where(ModelRequest.idempotency_key == crash_operation))
                    assert item.status == "reserved" and item.reserved_microusd > 0
                    request_id, held = item.id, item.reserved_microusd
                stopped_at = monotonic()
                stop_child(process_a, crash=True)
                try:
                    pending.result(timeout=10)
                except httpx.HTTPError:
                    pass
                else:
                    raise RuntimeError("Hard-stopped request unexpectedly completed")
            assert call(client_b, principals[0][1], crash_operation)[0].status_code == 409
            _, replacement = stack.enter_context(server(url))
            replacement_seconds = monotonic() - stopped_at
            assert replacement.get("/__fixture__/state").json()["calls"] == 0
            assert call(replacement, principals[0][1], duplicate)[0].status_code == 409
            assert call(replacement, principals[0][1], crash_operation)[0].status_code == 409
            assert replacement.get("/__fixture__/state").json()["calls"] == 0
            # Advance only this synthetic reservation's timestamp, then run the
            # real maintenance transaction. Never wait out a production lease.
            with Session(engine) as db:
                db.get(ModelRequest, request_id).created_at = utcnow() - timedelta(minutes=10)
                db.commit()
            maintenance = run_once(SimpleNamespace(model_reservation_lease_seconds=300), build_session_factory(engine))
            assert maintenance["stale_model_reservations"] >= 1
            result = replacement.get(f"/v1/requests/{request_id}", headers=principals[0][1])
            assert result.status_code == 200 and result.json()["status"] == "pending_reconciliation"
            durations += load_phase([replacement, client_b], principals, run + ":after")
            user_ids = [p[0] for p in principals]
            with Session(engine) as db:
                rows = db.scalars(select(ModelRequest).where(ModelRequest.user_id.in_(user_ids))).all()
                settled = [r for r in rows if r.status == "settled"]
                assert len(rows) == 162 and len(settled) == 161
                assert all(r.charged_microusd == 6 and r.upstream_cost_microusd == 2 for r in settled)
                assert db.scalar(select(func.count()).select_from(ProviderAttempt).where(
                    ProviderAttempt.request_id.in_([r.id for r in rows]))) == 161
                uncertain = db.get(ModelRequest, request_id)
                assert uncertain.reserved_microusd == held and uncertain.cost_state == "pending_reconciliation"
                for user in user_ids:
                    wallet = db.get(Wallet, user)
                    assert wallet.balance_microusd >= 0
                    assert wallet.balance_microusd + wallet.reserved_microusd == 100000 - sum(
                        r.charged_microusd for r in rows if r.user_id == user)
                assert db.get(Wallet, user_ids[0]).reserved_microusd == held
                day = db.get(PlatformDailyBudget, uncertain.platform_budget_period)
                assert day.limit_microusd == frozen_ceiling
                assert day.spent_microusd == spent_before + sum(r.upstream_cost_microusd for r in settled)
                assert uncertain.platform_reserved_microusd > 0
                expected_holds = db.scalar(select(func.coalesce(func.sum(ModelRequest.platform_reserved_microusd), 0)).where(
                    ModelRequest.platform_budget_period == day.period,
                    ModelRequest.status.in_(("reserved", "pending_reconciliation"))))
                assert day.reserved_microusd == expected_holds
                assert day.spent_microusd + day.reserved_microusd <= day.limit_microusd
                assert not db.execute(select(LedgerEntry.transaction_id).group_by(LedgerEntry.transaction_id)
                                      .having(func.sum(LedgerEntry.amount_microusd) != 0)).first()
            print("Synthetic two-process HTTP load:", json.dumps(latency_summary(durations), sort_keys=True))
            print(f"Hard-stop/replacement passed in {replacement_seconds:.2f}s: 161 settlements, "
                  "one unknown-cost hold, duplicate and cross-tenant denial; no repeat upstream on replacement.")
            print("NOT production capacity, real supplier behavior, Vercel rollout, or a promised recovery time.")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Isolated runtime gate failed; no live service was tested. "
              "Inspect only the acknowledged synthetic fixture; response bodies and credentials are not logged.", file=sys.stderr)
        raise SystemExit(1)
