import time

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import AuthRateLimitCounter, RateLimitCounter
from app.services.rate_limit_maintenance import cleanup_rate_limit_counters


def test_cleanup_removes_counters_older_than_seven_days_and_keeps_new_rows():
    engine = create_engine("sqlite://")
    RateLimitCounter.__table__.create(engine)
    AuthRateLimitCounter.__table__.create(engine)
    now_epoch = 2_000_000_000
    old_window = now_epoch // 60 - (7 * 24 * 60 + 1)
    boundary_window = now_epoch // 60 - 7 * 24 * 60
    fresh_window = now_epoch // 60 - 10

    with Session(engine) as session:
        session.add_all([
            RateLimitCounter(id="old", api_key_id="key", window_epoch=old_window),
            RateLimitCounter(id="boundary", api_key_id="key", window_epoch=boundary_window),
            RateLimitCounter(id="fresh", api_key_id="key", window_epoch=fresh_window),
            AuthRateLimitCounter(id="auth-old", subject_digest="a", action="login", window_epoch=old_window),
            AuthRateLimitCounter(id="auth-boundary", subject_digest="b", action="login", window_epoch=boundary_window),
            AuthRateLimitCounter(id="auth-fresh", subject_digest="c", action="login", window_epoch=fresh_window),
        ])
        session.commit()

        result = cleanup_rate_limit_counters(session, now_epoch=now_epoch)
        session.commit()

        assert result == {"rate_limit_counters": 1, "auth_rate_limit_counters": 1}
        assert session.scalars(select(RateLimitCounter.id).order_by(RateLimitCounter.id)).all() == ["boundary", "fresh"]
        assert session.scalars(select(AuthRateLimitCounter.id).order_by(AuthRateLimitCounter.id)).all() == ["auth-boundary", "auth-fresh"]


def test_cleanup_rejects_non_positive_retention():
    engine = create_engine("sqlite://")
    with Session(engine) as session:
        try:
            cleanup_rate_limit_counters(session, retention_days=0)
        except ValueError as exc:
            assert "retention_days" in str(exc)
        else:
            raise AssertionError("non-positive retention must be rejected")
