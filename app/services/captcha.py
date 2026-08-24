"""Fail-closed server-side CAPTCHA verification adapter."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx


class CaptchaError(RuntimeError):
    """A sanitized CAPTCHA failure safe to expose to an API caller."""


class CaptchaVerifier:
    """Verify a browser CAPTCHA token with the provider from the server.

    ``transport`` is intentionally injectable for tests and controlled staging.
    The provider endpoint is constrained here so a configuration mistake cannot
    turn this adapter into an arbitrary outbound HTTP client.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        secret: str,
        allowed_hosts: set[str] | frozenset[str],
        expected_hostname: str = "",
        timeout_seconds: float = 5.0,
        max_response_bytes: int = 65_536,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlparse(endpoint)
        hostname = parsed.hostname
        loopback = parsed.scheme == "http" and hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not loopback:
            raise ValueError("验证码服务必须使用 HTTPS 或本机回环地址")
        if not hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("验证码服务地址无效")
        normalized = {host.casefold().rstrip(".") for host in allowed_hosts if host}
        # Host matching is exact (including no wildcard/subdomain matching).
        if hostname.casefold().rstrip(".") not in normalized:
            raise ValueError("验证码服务主机不在允许列表")
        if not secret:
            raise ValueError("验证码服务密钥未配置")
        if not 0.1 <= timeout_seconds <= 30:
            raise ValueError("验证码服务超时必须位于 0.1 到 30 秒")
        if not 1 <= max_response_bytes <= 1_048_576:
            raise ValueError("验证码响应大小上限无效")
        self.endpoint = endpoint
        self.secret = secret
        self.expected_hostname = expected_hostname.casefold().rstrip(".")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.transport = transport

    async def verify(
        self, token: str, *, remote_ip: str | None = None,
        expected_action: str | None = None,
    ) -> bool:
        """Return provider's decision; all provider/network failures reject."""
        if not isinstance(token, str) or not token or len(token) > 2048:
            raise CaptchaError("验证码无效")
        fields: list[tuple[str, str]] = [("secret", self.secret), ("response", token)]
        if remote_ip:
            fields.append(("remoteip", remote_ip))
        body = str(httpx.QueryParams(fields)).encode("ascii")
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "POST",
                    self.endpoint,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    content=body,
                ) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        raise CaptchaError("验证码服务不可用")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_response_bytes:
                            raise CaptchaError("验证码服务不可用")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
        except CaptchaError:
            raise
        except (httpx.HTTPError, OSError, ValueError, UnicodeError) as exc:
            raise CaptchaError("验证码服务不可用") from exc
        try:
            data: Any = json.loads(raw)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise CaptchaError("验证码服务不可用") from exc
        if not isinstance(data, dict) or not isinstance(data.get("success"), bool):
            raise CaptchaError("验证码服务不可用")
        if not data["success"]:
            return False
        if self.expected_hostname:
            hostname = data.get("hostname")
            if (
                not isinstance(hostname, str)
                or hostname.casefold().rstrip(".") != self.expected_hostname
            ):
                return False
        if expected_action is not None and data.get("action") != expected_action:
            return False
        return True

    check = verify


# Name retained as a descriptive alias for callers that prefer adapter wording.
HttpCaptchaAdapter = CaptchaVerifier
