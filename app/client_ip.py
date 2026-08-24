"""Resolve client IPs only across an explicitly trusted reverse-proxy hop."""

from __future__ import annotations

from ipaddress import ip_address, ip_network
import secrets
from typing import Iterable, Sequence


CLIENT_IP_HEADER = b"x-kunlun-client-ip"
PROXY_SECRET_HEADER = b"x-kunlun-proxy-secret"


def _networks(cidrs: Iterable[str]):
    return tuple(ip_network(cidr, strict=False) for cidr in cidrs)


def resolve_client_ip(
    *, peer_host: str, forwarded_values: Sequence[bytes],
    trusted_proxy_cidrs: Iterable[str],
    proxy_secret_values: Sequence[bytes] = (),
    trusted_proxy_secret: str = "",
) -> str:
    """Return a proxy-supplied IP only for one valid header from a trusted peer.

    Production Caddy authenticates by an exact peer address. The Cloudflare
    Worker profile authenticates a dynamic internal peer with a shared secret.
    Both overwrite the private client-IP header; direct callers and malformed
    or duplicate headers always fall back to the TCP peer.
    """
    try:
        peer = ip_address(peer_host)
        trusted = _networks(trusted_proxy_cidrs)
    except ValueError:
        return peer_host
    cidr_authenticated = any(peer in network for network in trusted)
    secret_authenticated = False
    if trusted_proxy_secret and len(proxy_secret_values) == 1:
        try:
            expected = trusted_proxy_secret.encode("ascii")
        except UnicodeEncodeError:
            expected = b""
        secret_authenticated = bool(expected) and secrets.compare_digest(
            proxy_secret_values[0], expected
        )
    if not (cidr_authenticated or secret_authenticated) or len(forwarded_values) != 1:
        return str(peer)
    try:
        raw = forwarded_values[0].decode("ascii").strip()
        if not raw or "," in raw:
            return str(peer)
        return str(ip_address(raw))
    except (UnicodeDecodeError, ValueError):
        return str(peer)


class TrustedProxyClientIPMiddleware:
    """Replace ASGI's client host after validating the private proxy hop."""

    def __init__(
        self,
        app,
        *,
        trusted_proxy_cidrs: Iterable[str],
        trusted_proxy_secret: str = "",
    ) -> None:
        self.app = app
        self.trusted_proxy_cidrs = tuple(trusted_proxy_cidrs)
        self.trusted_proxy_secret = trusted_proxy_secret
        # Validate once during application construction as a second defense in
        # addition to Settings.validate().
        _networks(self.trusted_proxy_cidrs)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") in {"http", "websocket"} and scope.get("client"):
            peer_host, peer_port = scope["client"]
            values = [
                value for name, value in scope.get("headers", ())
                if name.lower() == CLIENT_IP_HEADER
            ]
            secret_values = [
                value for name, value in scope.get("headers", ())
                if name.lower() == PROXY_SECRET_HEADER
            ]
            resolved = resolve_client_ip(
                peer_host=peer_host,
                forwarded_values=values,
                trusted_proxy_cidrs=self.trusted_proxy_cidrs,
                proxy_secret_values=secret_values,
                trusted_proxy_secret=self.trusted_proxy_secret,
            )
            scope = dict(scope)
            scope["headers"] = [
                (name, value)
                for name, value in scope.get("headers", ())
                if name.lower() not in {CLIENT_IP_HEADER, PROXY_SECRET_HEADER}
            ]
            if resolved != peer_host:
                scope["client"] = (resolved, peer_port)
        await self.app(scope, receive, send)
