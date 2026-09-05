from __future__ import annotations

from datetime import timedelta
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import ApiKey, ModelPrice, ModelRequest, User, Wallet
from app.security import utcnow
from app.services.gateway_billing import BillingError, reserve_model_request
from app.services.reconciliation_maintenance import recover_stale_model_reservations


def _billing_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.sqlite3'}", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    user = User(
        id=str(uuid.uuid4()), email="recovery@example.com", password_hash="test-only",
        status="active",
    )
    key = ApiKey(
        id="recovery-key", user_id=user.id, name="recovery", secret_digest="d" * 64,
        last_four="test", status="active",
    )
    session.add_all([
        user,
        Wallet(user_id=user.id, balance_microusd=100_000),
        key,
        ModelPrice(
            id=str(uuid.uuid4()), model="recovery-model", version=1,
            input_microusd_per_million=1_000_000,
            output_microusd_per_million=1_000_000,
            max_output_tokens=100,
        ),
    ])
    session.commit()
    return engine, session, user, key


def test_stale_reserved_model_request_moves_to_manual_queue_without_releasing_hold(tmp_path):
    engine, session, user, key = _billing_session(tmp_path)
    try:
        reservation = reserve_model_request(
            session,
            user_id=user.id,
            api_key_id=key.id,
            model="recovery-model",
            billable_payload={"messages": [{"role": "user", "content": "hello"}]},
            max_output_tokens=10,
            idempotency_key="crash-before-provider-result",
        )
        request = session.get(ModelRequest, reservation.request_id)
        request.created_at = utcnow() - timedelta(minutes=10)
        session.commit()
        reserved_before = session.get(Wallet, user.id).reserved_microusd

        changed = recover_stale_model_reservations(
            session, lease_seconds=300, now=utcnow(),
        )
        session.commit()

        recovered = session.get(ModelRequest, reservation.request_id)
        assert changed == 1
        assert recovered.status == "pending_reconciliation"
        assert recovered.cost_state == "pending_reconciliation"
        assert recovered.failure_category == "reservation_lease_expired"
        assert recovered.completed_at is not None
        wallet = session.get(Wallet, user.id)
        assert wallet.reserved_microusd == reserved_before
        assert wallet.balance_microusd + wallet.reserved_microusd == 100_000
        assert recover_stale_model_reservations(
            session, lease_seconds=300, now=utcnow(),
        ) == 0
    finally:
        session.close()
        engine.dispose()


def test_fresh_model_reservation_is_not_recovered(tmp_path):
    engine, session, user, key = _billing_session(tmp_path)
    try:
        reservation = reserve_model_request(
            session,
            user_id=user.id,
            api_key_id=key.id,
            model="recovery-model",
            billable_payload={"messages": [{"role": "user", "content": "hello"}]},
            max_output_tokens=10,
            idempotency_key="fresh-reservation",
        )
        assert recover_stale_model_reservations(
            session, lease_seconds=300, now=utcnow(),
        ) == 0
        assert session.get(ModelRequest, reservation.request_id).status == "reserved"
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("user_status", "key_status", "expected_code"),
    [("frozen", "active", 403), ("active", "revoked", 401)],
)
def test_reservation_rechecks_user_and_api_key_inside_billing_transaction(
    tmp_path, user_status, key_status, expected_code,
):
    engine, session, user, key = _billing_session(tmp_path)
    try:
        user.status = user_status
        key.status = key_status
        session.commit()
        with pytest.raises(BillingError) as rejected:
            reserve_model_request(
                session,
                user_id=user.id,
                api_key_id=key.id,
                model="recovery-model",
                billable_payload={"messages": [{"role": "user", "content": "hello"}]},
                max_output_tokens=10,
                idempotency_key=f"rejected-{user_status}-{key_status}",
            )
        assert rejected.value.status_code == expected_code
        assert session.get(Wallet, user.id).reserved_microusd == 0
    finally:
        session.close()
        engine.dispose()
