"""ASGI import target: uvicorn app.main:app."""

from . import create_app

app = create_app()

__all__ = ["app"]
