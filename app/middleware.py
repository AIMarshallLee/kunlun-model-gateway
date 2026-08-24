"""ASGI middleware that limits actual bytes, including chunked requests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from starlette.responses import JSONResponse


ASGIApp = Callable[[dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]], Awaitable[None]]


class RequestBodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        default_limit: int = 1_048_576,
        webhook_limit: int = 65_536,
        auth_limit: int = 32_768,
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.webhook_limit = webhook_limit
        self.auth_limit = auth_limit

    def _limit(self, path: str) -> int:
        if path in {"/billing/webhook", "/billing/live/webhook"}:
            return self.webhook_limit
        if path.startswith("/auth/") or path.startswith("/v1/auth/"):
            return self.auth_limit
        return self.default_limit

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope.get("type") != "http" or scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        limit = self._limit(str(scope.get("path") or ""))
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                response = JSONResponse(status_code=400, content={"detail": "Content-Length 无效"})
                await response(scope, receive, send)
                return
        parts: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                continue
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > limit:
                await self._reject(scope, receive, send)
                return
            if chunk:
                parts.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(parts)
        delivered = False

        async def replay() -> dict[str, Any]:
            nonlocal delivered
            if delivered:
                # Streaming responses listen for a real disconnect after the
                # request body has been consumed; a synthetic disconnect here
                # would cancel every healthy stream before headers are sent.
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        response = JSONResponse(
            status_code=413,
            content={"detail": "请求正文超过此端点允许的大小"},
            headers={"Connection": "close", "Cache-Control": "no-store"},
        )
        await response(scope, receive, send)
