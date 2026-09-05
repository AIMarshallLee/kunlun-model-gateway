"""Provider-neutral HTTPS payment bridge.

The bridge is deliberately a narrow protocol adapter. It does not contain
merchant settlement logic, persist payment bodies, or log credentials. The
caller owns durable idempotency and ledger transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import threading
import time
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_TIMESTAMP = re.compile(r"^[0-9]{1,20}$")
_SIGNATURE = re.compile(r"^[0-9a-fA-F]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_STATUSES = {"pending", "paid", "failed", "refunded", "refunding", "closed"}


class PaymentBridgeError(RuntimeError):
    """Sanitized, fail-closed bridge failure."""

    def __init__(self, message: str, *, code: str = "payment_bridge_error", safe_to_retry: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.safe_to_retry = safe_to_retry


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    order_id: str
    payment_amount_minor: int
    currency: str
    status: str
    checkout_url: str
    provider_transaction_id: str
    request_timestamp: str
    request_nonce: str


@dataclass(frozen=True, slots=True)
class PaymentStatus:
    order_id: str
    payment_amount_minor: int
    currency: str
    status: str
    provider_transaction_id: str


@dataclass(frozen=True, slots=True)
class WebhookResult(PaymentStatus):
    event_id: str
    event_type: str
    nonce: str
    idempotency_key: str
    duplicate: bool = False
    provider_refund_id: str | None = None
    provider_dispute_id: str | None = None
    provider_return_id: str | None = None


@dataclass(frozen=True, slots=True)
class RefundResult(PaymentStatus):
    provider_refund_id: str = ""


def sign_bridge_body(body: bytes, *, timestamp: int | str, nonce: str, secret: str) -> str:
    """Return HMAC-SHA256 over the exact ``timestamp.nonce.body`` envelope."""
    try:
        timestamp_text = str(timestamp)
    except Exception:
        return ""
    if (
        not secret
        or not isinstance(nonce, str)
        or not _ID.fullmatch(nonce)
        or not _TIMESTAMP.fullmatch(timestamp_text)
    ):
        return ""
    try:
        message = f"{timestamp_text}.{nonce}.".encode("ascii") + body
        return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    except (UnicodeError, TypeError, ValueError):
        return ""


class LivePaymentBridge:
    """HTTPS sidecar contract compatible with official provider SDK wrappers."""

    def __init__(self, *, endpoint: str, merchant_id: str, secret: str,
                 allowed_hosts: set[str] | frozenset[str],
                 timeout_seconds: float = 10.0, timestamp_window_seconds: int = 300,
                 transport: httpx.AsyncBaseTransport | None = None,
                 clock: callable = time.time) -> None:
        parsed = urlparse(endpoint)
        normalized_hosts = {host.casefold().rstrip(".") for host in allowed_hosts if host}
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise PaymentBridgeError("支付桥接地址必须使用 HTTPS", code="invalid_endpoint")
        if parsed.hostname.casefold().rstrip(".") not in normalized_hosts:
            raise PaymentBridgeError("支付桥接主机不在允许列表", code="invalid_endpoint")
        if not merchant_id or len(merchant_id) > 128 or not secret:
            raise PaymentBridgeError("支付桥接凭据配置无效", code="invalid_config")
        if not 0.1 <= timeout_seconds <= 60:
            raise PaymentBridgeError("支付桥接超时配置无效", code="invalid_config")
        if not 30 <= timestamp_window_seconds <= 900:
            raise PaymentBridgeError("支付桥接时间窗配置无效", code="invalid_config")
        self.endpoint = endpoint.rstrip("/")
        self.allowed_hosts = normalized_hosts
        self.merchant_id = merchant_id
        self._secret = secret
        self.timeout_seconds = timeout_seconds
        self.timestamp_window_seconds = timestamp_window_seconds
        self.transport = transport
        self._clock = clock
        self._seen_nonces: dict[str, str] = {}
        self._seen_events: dict[str, tuple[str, str]] = {}
        self._seen_lock = threading.Lock()

    @staticmethod
    def _validate_order(order_id: str) -> str:
        if not isinstance(order_id, str) or not _ID.fullmatch(order_id):
            raise PaymentBridgeError("订单号格式无效", code="invalid_order")
        return order_id

    @staticmethod
    def _validate_amount(amount: int) -> int:
        if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= 100_000_000_000_000:
            raise PaymentBridgeError("金额必须是允许范围内的整数", code="invalid_amount")
        return amount

    @staticmethod
    def _validate_currency(currency: str) -> str:
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
            raise PaymentBridgeError("币种必须是三位大写代码", code="invalid_currency")
        return currency

    @staticmethod
    def _validate_txn(txn: str) -> str:
        if not isinstance(txn, str) or not _ID.fullmatch(txn):
            raise PaymentBridgeError("供应商交易号格式无效", code="invalid_provider_transaction")
        return txn

    def _request_headers(self, body: bytes) -> dict[str, str]:
        timestamp = str(int(self._clock()))
        nonce = hashlib.sha256(f"{timestamp}:{time.perf_counter_ns()}".encode()).hexdigest()[:32]
        return {
            "Content-Type": "application/json",
            "X-Kunlun-Merchant": self.merchant_id,
            "X-Kunlun-Timestamp": timestamp,
            "X-Kunlun-Nonce": nonce,
            "X-Kunlun-Signature": sign_bridge_body(body, timestamp=timestamp, nonce=nonce, secret=self._secret),
        }

    def _check_signed_headers(self, headers: Mapping[str, str], body: bytes) -> None:
        normalized = {str(key).casefold(): str(value) for key, value in headers.items()}
        timestamp = normalized.get("x-kunlun-timestamp", "")
        nonce = normalized.get("x-kunlun-nonce", "")
        signature = normalized.get("x-kunlun-signature", "")
        if not timestamp or not nonce or not signature:
            raise PaymentBridgeError("支付桥接响应签名缺失", code="response_signature_invalid")
        if not _TIMESTAMP.fullmatch(timestamp):
            raise PaymentBridgeError("支付桥接响应时间戳无效", code="response_timestamp_invalid")
        if not _ID.fullmatch(nonce):
            raise PaymentBridgeError("支付桥接响应 nonce 无效", code="response_signature_invalid")
        if not _SIGNATURE.fullmatch(signature):
            raise PaymentBridgeError("支付桥接响应签名缺失", code="response_signature_invalid")
        try:
            timestamp_value = int(timestamp)
        except (TypeError, ValueError, OverflowError):
            raise PaymentBridgeError("支付桥接响应时间戳无效", code="response_timestamp_invalid") from None
        try:
            clock_value = int(self._clock())
        except (TypeError, ValueError, OverflowError):
            raise PaymentBridgeError("支付桥接响应时间戳无效", code="response_timestamp_invalid") from None
        if abs(clock_value - timestamp_value) > self.timestamp_window_seconds:
            raise PaymentBridgeError("支付桥接响应超出时间窗口", code="response_timestamp_invalid")
        expected = sign_bridge_body(body, timestamp=timestamp, nonce=nonce, secret=self._secret)
        if not hmac.compare_digest(expected, signature):
            raise PaymentBridgeError("支付桥接响应签名无效", code="response_signature_invalid")

    async def _post(self, path: str, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        request_headers = self._request_headers(body)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False, trust_env=False, transport=self.transport) as client:
                async with client.stream(
                    "POST",
                    self.endpoint + path,
                    content=body,
                    headers=request_headers,
                ) as response:
                    status_code = response.status_code
                    response_headers = dict(response.headers)
                    chunks: list[bytes] = []
                    response_size = 0
                    async for chunk in response.aiter_bytes():
                        response_size += len(chunk)
                        if response_size > 256 * 1024:
                            raise PaymentBridgeError("支付桥接响应过大", code="response_too_large")
                        chunks.append(chunk)
                    response_body = b"".join(chunks)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise PaymentBridgeError("支付桥接网络请求失败", code="network_failure", safe_to_retry=False) from exc
        if status_code >= 400:
            raise PaymentBridgeError("支付桥接返回失败状态", code="provider_http_error", safe_to_retry=False)
        self._check_signed_headers(response_headers, response_body)
        try:
            parsed = json.loads(response_body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise PaymentBridgeError("支付桥接响应不是有效 JSON", code="invalid_response_json") from exc
        if not isinstance(parsed, dict):
            raise PaymentBridgeError("支付桥接响应结构无效", code="invalid_response")
        return parsed, request_headers

    def _status(self, data: Mapping[str, Any], *, order_id: str, payment_amount_minor: int,
                currency: str, provider_transaction_id: str | None) -> PaymentStatus:
        actual_order = self._validate_order(data.get("order_id", ""))
        actual_amount = self._validate_amount(data.get("payment_amount_minor"))
        actual_currency = self._validate_currency(data.get("currency", ""))
        actual_txn = self._validate_txn(data.get("provider_transaction_id", ""))
        status = data.get("status")
        if status not in _STATUSES:
            raise PaymentBridgeError("支付状态无效", code="invalid_status")
        if (actual_order, actual_amount, actual_currency) != (order_id, payment_amount_minor, currency):
            raise PaymentBridgeError("支付响应与订单不一致", code="payment_mismatch")
        if provider_transaction_id is not None and actual_txn != provider_transaction_id:
            raise PaymentBridgeError("支付响应与订单不一致", code="payment_mismatch")
        return PaymentStatus(actual_order, actual_amount, actual_currency, status, actual_txn)

    async def create_checkout(
        self, *, order_id: str, payment_amount_minor: int,
        idempotency_key: str, currency: str = "CNY",
        return_url: str | None = None,
    ) -> CheckoutResult:
        order_id = self._validate_order(order_id); payment_amount_minor = self._validate_amount(payment_amount_minor); currency = self._validate_currency(currency)
        if not isinstance(idempotency_key, str) or not _ID.fullmatch(idempotency_key):
            raise PaymentBridgeError("支付幂等键格式无效", code="invalid_idempotency_key")
        payload: dict[str, Any] = {
            "merchant_id": self.merchant_id,
            "order_id": order_id,
            "payment_amount_minor": payment_amount_minor,
            "currency": currency,
            "idempotency_key": idempotency_key,
        }
        if return_url is not None:
            if not isinstance(return_url, str) or not return_url.startswith("https://"):
                raise PaymentBridgeError("回跳地址必须使用 HTTPS", code="invalid_return_url")
            payload["return_url"] = return_url
        data, request_headers = await self._post("/v1/payments/checkout", payload)
        status = self._status(data, order_id=order_id, payment_amount_minor=payment_amount_minor, currency=currency,
                              provider_transaction_id=self._validate_txn(data.get("provider_transaction_id", "")))
        checkout_url = data.get("checkout_url")
        checkout_parsed = urlparse(checkout_url) if isinstance(checkout_url, str) else None
        if (
            checkout_parsed is None
            or checkout_parsed.scheme != "https"
            or not checkout_parsed.hostname
            or checkout_parsed.hostname.casefold().rstrip(".") not in self.allowed_hosts
        ):
            raise PaymentBridgeError("支付跳转地址无效", code="invalid_checkout_url")
        # Request metadata is returned for audit correlation; it contains no secret.
        # The exact values are read from the transport request in production callers.
        return CheckoutResult(status.order_id, status.payment_amount_minor, status.currency, status.status,
                              checkout_url, status.provider_transaction_id,
                              request_headers["X-Kunlun-Timestamp"], request_headers["X-Kunlun-Nonce"])

    async def query_payment(self, *, order_id: str, payment_amount_minor: int, currency: str = "CNY", provider_transaction_id: str) -> PaymentStatus:
        return await self._status_call("/v1/payments/query", order_id, payment_amount_minor, currency, provider_transaction_id)

    async def refund_payment(self, *, order_id: str, payment_amount_minor: int, currency: str = "CNY",
                             provider_transaction_id: str, idempotency_key: str) -> RefundResult:
        if not isinstance(idempotency_key, str) or not _ID.fullmatch(idempotency_key):
            raise PaymentBridgeError("退款幂等键格式无效", code="invalid_idempotency_key")
        data, status = await self._status_call_data(
            "/v1/payments/refund", order_id, payment_amount_minor, currency,
            provider_transaction_id, extra_payload={"idempotency_key": idempotency_key},
        )
        provider_refund_id = self._validate_txn(data.get("provider_refund_id", ""))
        return RefundResult(
            status.order_id,
            status.payment_amount_minor,
            status.currency,
            status.status,
            status.provider_transaction_id,
            provider_refund_id,
        )

    async def reconcile_payment(self, *, order_id: str, payment_amount_minor: int, currency: str = "CNY",
                                provider_transaction_id: str | None = None) -> PaymentStatus:
        """Reconcile by provider transaction or, when unknown, merchant order.

        A checkout timeout can happen before the provider transaction ID is
        returned.  Official payment APIs support querying by the merchant
        order ID for exactly this case; the signed sidecar must return the
        discovered provider transaction ID in its response.
        """
        order_id = self._validate_order(order_id)
        payment_amount_minor = self._validate_amount(payment_amount_minor)
        currency = self._validate_currency(currency)
        payload: dict[str, Any] = {
            "merchant_id": self.merchant_id,
            "order_id": order_id,
            "payment_amount_minor": payment_amount_minor,
            "currency": currency,
        }
        expected_transaction: str | None = None
        if provider_transaction_id is not None:
            expected_transaction = self._validate_txn(provider_transaction_id)
            payload["provider_transaction_id"] = expected_transaction
        data, _ = await self._post("/v1/payments/reconcile", payload)
        return self._status(
            data,
            order_id=order_id,
            payment_amount_minor=payment_amount_minor,
            currency=currency,
            provider_transaction_id=expected_transaction,
        )

    async def _status_call(self, path: str, order_id: str, payment_amount_minor: int, currency: str, provider_transaction_id: str) -> PaymentStatus:
        _, status = await self._status_call_data(path, order_id, payment_amount_minor, currency, provider_transaction_id)
        return status

    async def _status_call_data(self, path: str, order_id: str, payment_amount_minor: int,
                                currency: str, provider_transaction_id: str,
                                extra_payload: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], PaymentStatus]:
        order_id = self._validate_order(order_id); payment_amount_minor = self._validate_amount(payment_amount_minor); currency = self._validate_currency(currency); provider_transaction_id = self._validate_txn(provider_transaction_id)
        payload: dict[str, Any] = {"merchant_id": self.merchant_id, "order_id": order_id, "payment_amount_minor": payment_amount_minor, "currency": currency, "provider_transaction_id": provider_transaction_id}
        if extra_payload:
            payload.update(extra_payload)
        data, _ = await self._post(path, payload)
        return data, self._status(data, order_id=order_id, payment_amount_minor=payment_amount_minor, currency=currency, provider_transaction_id=provider_transaction_id)

    def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> WebhookResult:
        self._check_signed_headers(headers, raw_body)
        normalized_headers = {str(key).casefold(): str(value) for key, value in headers.items()}
        timestamp = normalized_headers.get("x-kunlun-timestamp", "")
        nonce = normalized_headers.get("x-kunlun-nonce", "")
        digest = hashlib.sha256(raw_body).hexdigest()
        with self._seen_lock:
            if nonce in self._seen_nonces:
                if self._seen_nonces[nonce] != digest:
                    raise PaymentBridgeError("同一 nonce 的回调正文不一致", code="webhook_replay_conflict")
                duplicate = True
            else:
                duplicate = False
            try:
                event = json.loads(raw_body)
            except (ValueError, UnicodeDecodeError) as exc:
                raise PaymentBridgeError("支付回调不是有效 JSON", code="invalid_webhook_json") from exc
            if not isinstance(event, dict):
                raise PaymentBridgeError("支付回调结构无效", code="invalid_webhook")
            event_id = self._validate_order(event.get("event_id", ""))
            order_id = self._validate_order(event.get("order_id", "")); amount = self._validate_amount(event.get("payment_amount_minor")); currency = self._validate_currency(event.get("currency", "")); txn = self._validate_txn(event.get("provider_transaction_id", ""))
            event_type = event.get("type")
            if event_type not in {
                "payment.succeeded", "payment.failed", "payment.refunded",
                "payment.pending", "payment.closed",
                "payment.charged_back", "payment.chargeback_returned",
            }:
                raise PaymentBridgeError("支付事件类型无效", code="invalid_webhook_type")
            status = event.get("status")
            if status not in _STATUSES | {"charged_back", "chargeback_returned"}:
                raise PaymentBridgeError("支付状态无效", code="invalid_status")
            provider_refund_id: str | None = None
            if event_type == "payment.refunded":
                try:
                    provider_refund_id = self._validate_txn(event.get("provider_refund_id", ""))
                except PaymentBridgeError as exc:
                    raise PaymentBridgeError("支付退款号格式无效", code="invalid_provider_refund") from exc
            elif event.get("provider_refund_id") is not None:
                raise PaymentBridgeError("非退款事件不得携带退款号", code="invalid_provider_refund")
            provider_dispute_id: str | None = None
            if event_type in {"payment.charged_back", "payment.chargeback_returned"}:
                if event.get("merchant_id") != self.merchant_id:
                    raise PaymentBridgeError("拒付事件商户不匹配", code="invalid_merchant")
                if status != event_type.removeprefix("payment."):
                    raise PaymentBridgeError("拒付事件状态无效", code="invalid_status")
                provider_dispute_id = self._validate_txn(event.get("provider_dispute_id", ""))
            elif event.get("provider_dispute_id") is not None or status in {"charged_back", "chargeback_returned"}:
                raise PaymentBridgeError("非拒付事件不得携带争议标识或状态", code="invalid_provider_dispute")
            provider_return_id: str | None = None
            if event_type == "payment.chargeback_returned":
                provider_return_id = self._validate_txn(event.get("provider_return_id", ""))
            elif event.get("provider_return_id") is not None:
                raise PaymentBridgeError("非返还事件不得携带返还标识", code="invalid_provider_return")
            old = self._seen_events.get(event_id)
            if old is not None and old != (digest, nonce):
                raise PaymentBridgeError("同一事件编号的回调正文不一致", code="webhook_replay_conflict")
            self._seen_nonces[nonce] = digest
            self._seen_events[event_id] = (digest, nonce)
            # Process-local defense-in-depth only; durable uniqueness is in
            # PostgreSQL. Keep these maps bounded to avoid an unauthenticated
            # memory-growth vector if the bridge secret is ever compromised.
            while len(self._seen_nonces) > 10_000:
                self._seen_nonces.pop(next(iter(self._seen_nonces)))
            while len(self._seen_events) > 10_000:
                self._seen_events.pop(next(iter(self._seen_events)))
        return WebhookResult(
            order_id, amount, currency, status, txn, event_id, event_type, nonce,
            f"payment:{event_id}", duplicate or old is not None, provider_refund_id, provider_dispute_id, provider_return_id,
        )
