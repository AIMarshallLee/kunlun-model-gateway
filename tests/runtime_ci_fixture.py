"""Synthetic loopback runtime used ONLY by verify_runtime_postgres.py.

Not packaged in the application wheel. No real payment, email, Vault, or
provider transport. Do not use as a deployment entrypoint.
"""
import asyncio
import json
import os

import httpx
from sqlalchemy.engine import make_url

from app import create_app, providers
from app.db import build_engine
from app.db_guards import SCHEMA_HEAD, assert_schema_revision
from app.services.identity import InMemoryEmailSender
from app.services.platform_credentials import InMemoryPlatformVault

PEPPER = "ci-runtime-only-persistent-pepper-1234567890"
MODEL = "runtime-ci-model"


def fixture_database_url(env):
    try:
        url = make_url(env.get("KUNLUN_RUNTIME_DATABASE_URL", "sqlite://"))
        valid = (env.get("KUNLUN_CI_ISOLATED_DATABASE") == "kunlun-ci-disposable"
                 and url.drivername == "postgresql+psycopg" and url.host == "127.0.0.1"
                 and url.database == "kunlun_ci" and url.username == "kunlun_runtime"
                 and bool(url.password) and not url.query
                 and not any(env.get(k) for k in ("PGSERVICE", "PGSERVICEFILE", "PGHOSTADDR", "PGOPTIONS")))
    except Exception:
        valid = False
    if not valid:
        raise ValueError("Requires acknowledged disposable local kunlun_ci runtime database")
    return url


def create_fixture():
    url = fixture_database_url(os.environ)  # Before connections or test routes.
    instance = os.environ["KUNLUN_RUNTIME_INSTANCE"]
    engine = build_engine(url.render_as_string(hide_password=False))
    try:
        assert_schema_revision(engine, SCHEMA_HEAD)
    finally:
        engine.dispose()
    # Never inherit real live/payment/SMTP configuration into the fixture.
    for key in list(os.environ):
        if key.startswith("KUNLUN_"):
            del os.environ[key]
    os.environ.update({
        "KUNLUN_API_KEY_PEPPER": PEPPER, "KUNLUN_SESSION_PEPPER": PEPPER,
        "KUNLUN_MODELS_JSON": json.dumps({MODEL: {"input_microusd_per_million": 1000000,
            "output_microusd_per_million": 1000000, "max_output_tokens": 32}}),
        "KUNLUN_PROVIDERS_JSON": json.dumps([{"name": "openai", "base_url": "https://api.openai.com/v1",
            "models": [MODEL], "pricing": {MODEL: {"input_microusd_per_million": 50000,
                                                   "output_microusd_per_million": 50000}}}]),
        "KUNLUN_PROVIDER_HOST_ALLOWLIST": "api.openai.com",
        "KUNLUN_PLATFORM_DAILY_BUDGET_MICROUSD": "1000000",
    })
    state = {"instance": instance, "calls": 0, "blocked": False}
    async def transport(request):
        state["calls"] += 1
        if json.loads(request.content)["messages"][0]["content"] == "ci-block-until-process-death":
            state["blocked"] = True
            await asyncio.Event().wait()
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"model": MODEL, "choices": [{"message": {
            "role": "assistant", "content": "synthetic"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2}})
    providers.build_managed_provider_client = lambda catalog, *, api_key, allowed_hosts: providers.OpenAICompatibleProvider(
        provider_name=catalog["name"], base_url=catalog["base_url"], api_key=api_key,
        models=set(catalog["models"]), pricing=catalog["pricing"], transport=httpx.MockTransport(transport))
    vault = InMemoryPlatformVault()
    vault.write(provider="openai", secret="inert-runtime-supply", operation_id="fixture",
                actor="ci", reason="synthetic runtime acceptance")
    app = create_app(database_url=url.render_as_string(hide_password=False), environment="test",
        gateway_mode="managed_gateway", platform_vault=vault, public_signup=False,
        identity_sender=InMemoryEmailSender(), require_email_verification=True,
        enable_test_payments=False, rate_limit_per_minute=10000)

    @app.get("/__fixture__/state")
    def fixture_state():
        return state
    return app
