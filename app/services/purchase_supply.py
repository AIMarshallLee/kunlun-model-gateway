"""Read-only configured supply admission; not an upstream health or license probe."""
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..models import ModelPrice
from .credentials import SecretUnavailable


def has_configured_supply(settings, vault, session_factory) -> bool:
    """Require an enabled allowlisted channel for at least one listed model.

    Never resolve raw keys or make an upstream request from a public read or
    checkout check. Configuration is a snapshot, not a service reservation.
    """
    try:
        channels = {row["provider"] for row in vault.list()
                    if row["active"] and not row["pending_cleanup"]}
        models = {model for provider in settings.providers if provider["name"] in channels
                  for model in provider["models"] if model in settings.models}
        if not models:
            return False
        with session_factory() as db:
            return db.scalar(select(ModelPrice.id).where(
                ModelPrice.active.is_(True), ModelPrice.model.in_(models),
            ).limit(1)) is not None
    except (SecretUnavailable, SQLAlchemyError):
        return False
