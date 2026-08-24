"""Fail-closed adapter for an independently operated content-safety service."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any
from urllib.parse import urlparse

import httpx


_REASON_CODE = re.compile(r"^[a-z0-9_.-]{1,64}$")


class ContentSafetyError(RuntimeError):
    """A sanitized safety-service failure that is safe to show to callers."""


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    allowed: bool
    reason_code: str
    decision_id: str | None = None


class HttpContentSafetyAdapter:
    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        allowed_hosts: set[str],
        timeout_seconds: float = 5.0,
        max_content_bytes: int = 262_144,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlparse(endpoint)
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not local_http:
            raise ValueError("内容安全服务必须使用 HTTPS 或本机回环地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
            raise ValueError("内容安全服务地址无效")
        normalized_hosts = {host.casefold() for host in allowed_hosts}
        if parsed.hostname.casefold() not in normalized_hosts:
            raise ValueError("内容安全服务主机不在允许列表")
        if not api_key:
            raise ValueError("内容安全服务密钥未配置")
        if not 0.1 <= timeout_seconds <= 30:
            raise ValueError("内容安全服务超时必须位于 0.1 到 30 秒")
        if not 16 <= max_content_bytes <= 1_048_576:
            raise ValueError("内容安全正文上限无效")
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_content_bytes = max_content_bytes
        self.transport = transport

    async def check(self, *, kind: str, model: str, content: Any) -> SafetyDecision:
        if kind not in {"input", "output"}:
            raise ContentSafetyError("内容安全检查类型无效")
        if not model or len(model) > 120:
            raise ContentSafetyError("内容安全模型标识无效")
        try:
            content_bytes = json.dumps(
                content,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ContentSafetyError("内容安全正文无法序列化") from exc
        if len(content_bytes) > self.max_content_bytes:
            raise ContentSafetyError("内容安全正文超过允许大小")
        payload = b'{"kind":' + json.dumps(kind).encode("ascii")
        payload += b',"model":' + json.dumps(model).encode("utf-8")
        payload += b',"content":' + content_bytes + b"}"
        timeout = httpx.Timeout(self.timeout_seconds)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "POST",
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    content=payload,
                ) as response:
                    if response.status_code != 200:
                        raise ContentSafetyError("内容安全服务不可用，已拒绝本次请求")
                    response_bytes = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(response_bytes) + len(chunk) > 65_536:
                            raise ContentSafetyError("内容安全服务不可用，已拒绝本次请求")
                        response_bytes.extend(chunk)
        except (httpx.HTTPError, OSError) as exc:
            raise ContentSafetyError("内容安全服务不可用，已拒绝本次请求") from exc
        try:
            data = json.loads(response_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ContentSafetyError("内容安全服务返回无效，已拒绝本次请求") from exc
        if not isinstance(data, dict) or not isinstance(data.get("allowed"), bool):
            raise ContentSafetyError("内容安全服务返回无效，已拒绝本次请求")
        reason_code = data.get("reason_code", "ok" if data["allowed"] else "policy_disallowed")
        decision_id = data.get("decision_id")
        if not isinstance(reason_code, str) or not _REASON_CODE.fullmatch(reason_code):
            raise ContentSafetyError("内容安全服务返回无效，已拒绝本次请求")
        if decision_id is not None and (not isinstance(decision_id, str) or len(decision_id) > 128):
            raise ContentSafetyError("内容安全服务返回无效，已拒绝本次请求")
        return SafetyDecision(
            allowed=data["allowed"],
            reason_code=reason_code,
            decision_id=decision_id,
        )
