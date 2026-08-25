"""Vercel ingress boundary for the container deployment profile."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import unquote

from starlette.responses import PlainTextResponse


CLIENT_IP_HEADER = b"x-kunlun-client-ip"
PROXY_SECRET_HEADER = b"x-kunlun-proxy-secret"
# Vercel explicitly documents that it overwrites this value at ingress to
# prevent caller spoofing. Do not prefer a generic application-supplied header.
VERCEL_CLIENT_IP_HEADER = b"x-forwarded-for"
VERCEL_REQUEST_HEADER = b"x-vercel-id"
REMOVED_PROXY_HEADERS = {
    b"forwarded",
    VERCEL_CLIENT_IP_HEADER,
    b"x-forwarded-host",
    b"x-forwarded-port",
    b"x-forwarded-proto",
    b"x-real-ip",
    b"x-vercel-forwarded-for",
    CLIENT_IP_HEADER,
    PROXY_SECRET_HEADER,
}


def _canonical_path(raw_path: bytes | str) -> str | None:
    try:
        value = raw_path.decode("ascii") if isinstance(raw_path, bytes) else raw_path
    except UnicodeDecodeError:
        return None
    stable = False
    try:
        for _count in range(8):
            decoded = unquote(value, errors="strict")
            if decoded == value:
                stable = True
                break
            value = decoded
    except (UnicodeDecodeError, ValueError):
        return None
    if not stable or any(ord(character) < 32 for character in value):
        return None
    segments: list[str] = []
    for segment in value.replace("\\", "/").split("/"):
        if not segment or segment == ".":
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/" + "/".join(segments).casefold()


def public_route_allowed(raw_path: bytes | str) -> bool:
    path = _canonical_path(raw_path)
    if path is None:
        return False
    if path == "/metrics" or path.startswith("/metrics/"):
        return False
    return path != "/ops" and not path.startswith("/ops/")


def _single_ip(values: list[bytes]) -> bytes | None:
    if len(values) != 1:
        return None
    try:
        raw = values[0].decode("ascii").strip()
        if not raw or "," in raw:
            return None
        return str(ip_address(raw)).encode("ascii")
    except (UnicodeDecodeError, ValueError):
        return None


class VercelIngressMiddleware:
    """Translate Vercel-owned headers into the gateway's private contract.

    Vercel documents that its ingress overwrites ``X-Forwarded-For`` to
    prevent spoofing and also provides ``X-Vercel-Forwarded-For``.
    The container keeps Uvicorn proxy parsing disabled; this adapter removes
    caller-supplied forwarding/internal headers and authenticates the one
    resulting client IP to ``TrustedProxyClientIPMiddleware``.
    """

    def __init__(self, app, *, proxy_secret: str) -> None:
        self.app = app
        self.proxy_secret = proxy_secret.encode("ascii")

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        raw_path = scope.get("raw_path") or scope.get("path") or "/"
        if not public_route_allowed(raw_path):
            response = PlainTextResponse(
                "Not Found",
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        headers = list(scope.get("headers", ()))
        client_ip = _single_ip([
            value for name, value in headers
            if name.lower() == VERCEL_CLIENT_IP_HEADER
        ])
        vercel_requests = [
            value for name, value in headers
            if name.lower() == VERCEL_REQUEST_HEADER and value.strip()
        ]
        filtered = [
            (name, value) for name, value in headers
            if name.lower() not in REMOVED_PROXY_HEADERS
        ]
        if client_ip is not None and len(vercel_requests) == 1:
            filtered.extend((
                (CLIENT_IP_HEADER, client_ip),
                (PROXY_SECRET_HEADER, self.proxy_secret),
            ))
        scope = dict(scope)
        scope["headers"] = filtered
        await self.app(scope, receive, send)
