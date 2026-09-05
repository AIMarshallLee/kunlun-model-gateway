"""OpenAI-compatible provider adapters and the controlled fallback list."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlparse

import httpx

from gateway import ProviderError
from app.config import BYOK_PROVIDER_CATALOG, validate_byok_provider_endpoint


ProviderCallable = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

@dataclass(slots=True)
class ProviderStream:
    client: httpx.AsyncClient
    response: httpx.Response

    async def chunks(self) -> AsyncIterator[bytes]:
        async for chunk in self.response.aiter_bytes():
            yield chunk

    async def close(self) -> None:
        await self.response.aclose()
        await self.client.aclose()


@dataclass(slots=True)
class OpenAICompatibleProvider:
    provider_name: str
    base_url: str
    api_key: str = field(repr=False)
    models: set[str] = field(default_factory=set)
    pricing: dict[str, dict[str, int]] = field(default_factory=dict)
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 60.0
    max_response_bytes: int = 10 * 1024 * 1024
    transport: httpx.AsyncBaseTransport | None = None

    def supports_model(self, model: str) -> bool:
        return not self.models or model in self.models

    def upstream_prices(self, model: str, fallback_input: int, fallback_output: int) -> tuple[int, int]:
        configured = self.pricing.get(model, {})
        return (
            int(configured.get("input_microusd_per_million", fallback_input)),
            int(configured.get("output_microusd_per_million", fallback_output)),
        )

    async def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + "/chat/completions"
        timeout = httpx.Timeout(
            connect=self.connect_timeout_seconds,
            read=self.read_timeout_seconds,
            write=10.0,
            pool=5.0,
        )
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        status_code = response.status_code
                        raise ProviderError(
                            status_code,
                            safe_to_failover=status_code == 429,
                            request_may_be_billable=status_code >= 500,
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > self.max_response_bytes:
                            raise ProviderError(
                                502,
                                category="provider_response_too_large",
                                safe_to_failover=False,
                                request_may_be_billable=True,
                            )
                        content.extend(chunk)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderError(
                503,
                category="provider_connect_failure",
                safe_to_failover=True,
                request_may_be_billable=False,
            ) from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            raise ProviderError(
                504,
                category="provider_ambiguous_timeout",
                safe_to_failover=False,
                request_may_be_billable=True,
            ) from exc
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError(
                502,
                category="provider_invalid_json",
                safe_to_failover=False,
                request_may_be_billable=True,
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(
                502,
                category="provider_invalid_payload",
                safe_to_failover=False,
                request_may_be_billable=True,
            )
        return data

    async def open_stream(self, payload: dict[str, Any]) -> ProviderStream:
        """Open an upstream SSE response before downstream headers are sent."""
        url = self.base_url.rstrip("/") + "/chat/completions"
        timeout = httpx.Timeout(
            connect=self.connect_timeout_seconds,
            read=self.read_timeout_seconds,
            write=10.0,
            pool=5.0,
        )
        client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        )
        try:
            request = client.build_request(
                "POST",
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response = await client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            await client.aclose()
            raise ProviderError(
                503,
                category="provider_connect_failure",
                safe_to_failover=True,
                request_may_be_billable=False,
            ) from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            await client.aclose()
            raise ProviderError(
                504,
                category="provider_ambiguous_timeout",
                safe_to_failover=False,
                request_may_be_billable=True,
            ) from exc
        if response.status_code >= 400:
            status_code = response.status_code
            await response.aclose()
            await client.aclose()
            raise ProviderError(
                status_code,
                safe_to_failover=status_code == 429,
                request_may_be_billable=status_code >= 500,
            )
        return ProviderStream(client=client, response=response)


# Tests and create_app replace this explicit route plan. It is intentionally empty
# when LIVE_UPSTREAM is disabled.
ordered_clients: list[ProviderCallable] = []


def build_provider_clients(
    configs: list[dict[str, Any]],
    *,
    allowed_hosts: set[str] | None = None,
) -> list[ProviderCallable]:
    if len(configs) > 32:
        raise RuntimeError("Provider 数量超过安全上限")
    clients: list[ProviderCallable] = []
    for index, config in enumerate(configs):
        if not isinstance(config, dict):
            raise RuntimeError("Provider 配置必须是对象")
        name = str(config.get("name") or f"provider-{index + 1}")
        if len(name) > 80:
            raise RuntimeError("Provider 名称过长")
        base_url = str(config.get("base_url") or "").strip()
        api_key_env = str(config.get("api_key_env") or "").strip()
        if not base_url or not api_key_env:
            raise RuntimeError(f"Provider {name} 缺少 base_url 或 api_key_env")
        parsed = urlparse(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not local_http:
            raise RuntimeError(f"Provider {name} 必须使用 HTTPS 或本机回环地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError(f"Provider {name} 的 base_url 含禁止的凭证、查询或片段")
        if not parsed.hostname:
            raise RuntimeError(f"Provider {name} 的 base_url 缺少主机")
        if allowed_hosts is not None and (parsed.hostname or "").casefold() not in allowed_hosts:
            raise RuntimeError(f"Provider {name} 的主机不在允许列表")
        api_key = os.getenv(api_key_env, "")
        if not api_key:
            raise RuntimeError(f"Provider {name} 引用的环境变量未设置")
        raw_models = config.get("models") or []
        if not isinstance(raw_models, list) or len(raw_models) > 256:
            raise RuntimeError(f"Provider {name} 的模型列表无效")
        if any(not isinstance(model, str) or not model or len(model) > 120 for model in raw_models):
            raise RuntimeError(f"Provider {name} 的模型名称无效")
        try:
            connect_timeout = float(config.get("connect_timeout_seconds", 5.0))
            read_timeout = float(config.get("read_timeout_seconds", 60.0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Provider {name} 的超时配置无效") from exc
        if not 0.1 <= connect_timeout <= 30.0:
            raise RuntimeError(f"Provider {name} 的连接超时必须位于 0.1 到 30 秒")
        if not 1.0 <= read_timeout <= 600.0:
            raise RuntimeError(f"Provider {name} 的读取超时必须位于 1 到 600 秒")
        raw_pricing = config.get("pricing") or {}
        if not isinstance(raw_pricing, dict) or len(raw_pricing) > 256:
            raise RuntimeError(f"Provider {name} 的价格配置无效")
        pricing: dict[str, dict[str, int]] = {}
        allowed_price_fields = {"input_microusd_per_million", "output_microusd_per_million"}
        for model, prices in raw_pricing.items():
            if not isinstance(model, str) or not isinstance(prices, dict) or set(prices) - allowed_price_fields:
                raise RuntimeError(f"Provider {name} 的价格配置无效")
            normalized: dict[str, int] = {}
            for key, value in prices.items():
                if isinstance(value, bool):
                    raise RuntimeError(f"Provider {name} 的价格必须是非负整数")
                try:
                    amount = int(value)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(f"Provider {name} 的价格必须是非负整数") from exc
                if amount < 0 or amount > 1_000_000_000_000:
                    raise RuntimeError(f"Provider {name} 的价格超出安全范围")
                normalized[str(key)] = amount
            pricing[model] = normalized
        clients.append(OpenAICompatibleProvider(
            provider_name=name,
            base_url=base_url,
            api_key=api_key,
            models={str(model) for model in raw_models},
            pricing=pricing,
            connect_timeout_seconds=connect_timeout,
            read_timeout_seconds=read_timeout,
        ))
    return clients


def build_byok_provider_client(
    config: dict[str, Any],
    *,
    api_key: str,
    allowed_hosts: set[str],
) -> OpenAICompatibleProvider:
    """Build one short-lived BYOK client from a server-owned definition.

    No client URL, model list or credential source is accepted from a request.
    """
    if not isinstance(config, dict):
        raise RuntimeError("BYOK Provider 配置必须是对象")
    name = str(config.get("name") or "").strip().casefold()
    base_url = str(config.get("base_url") or "").strip()
    models = config.get("models") or []
    parsed = urlparse(base_url)
    try:
        validate_byok_provider_endpoint(name, base_url)
    except RuntimeError as exc:
        raise RuntimeError("BYOK Provider 目录无效") from exc
    if (
        name not in BYOK_PROVIDER_CATALOG
        or not base_url
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.casefold() not in allowed_hosts
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or not isinstance(models, list)
        or any(not isinstance(model, str) or not model or len(model) > 120 for model in models)
    ):
        raise RuntimeError("BYOK Provider 目录无效")
    pricing = config.get("pricing") or {}
    if not isinstance(pricing, dict):
        raise RuntimeError("BYOK Provider 价格目录无效")
    return OpenAICompatibleProvider(
        provider_name=name,
        base_url=base_url,
        api_key=api_key,
        models=set(models),
        pricing=pricing,
    )


def provider_name(client: ProviderCallable, index: int) -> str:
    value = getattr(client, "provider_name", None)
    return value if isinstance(value, str) and value else f"provider-{index + 1}"


def build_managed_provider_client(config, *, api_key, allowed_hosts):
    # Same pinned official-endpoint contract; credentials come from a separate
    # platform Vault, never from the client or environment catalog.
    return build_byok_provider_client(config, api_key=api_key, allowed_hosts=allowed_hosts)


def supports_model(client: ProviderCallable, model: str) -> bool:
    # Read from the class so dynamic mocks cannot fabricate an awaitable
    # ``supports_model`` attribute and leak an un-awaited coroutine.
    predicate = getattr(type(client), "supports_model", None)
    return bool(predicate(client, model)) if callable(predicate) else True


def upstream_prices(client: ProviderCallable, model: str, fallback_input: int, fallback_output: int) -> tuple[int, int]:
    resolver = getattr(type(client), "upstream_prices", None)
    if callable(resolver):
        return resolver(client, model, fallback_input, fallback_output)
    return fallback_input, fallback_output
