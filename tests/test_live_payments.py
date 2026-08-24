from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest

from app.services.live_payments import (
    LivePaymentBridge,
    PaymentBridgeError,
    sign_bridge_body,
)


SECRET = "s" * 32


def signed_headers(body: bytes, *, timestamp: int | None = None, nonce: str = "n-1") -> dict[str, str]:
    ts = int(time.time()) if timestamp is None else timestamp
    return {
        "X-Kunlun-Timestamp": str(ts),
        "X-Kunlun-Nonce": nonce,
        "X-Kunlun-Signature": sign_bridge_body(body, timestamp=ts, nonce=nonce, secret=SECRET),
    }


def response_json(payload: dict, *, nonce: str = "response-1") -> httpx.Response:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return httpx.Response(200, content=body, headers={
        "content-type": "application/json",
        **signed_headers(body, nonce=nonce),
    })


def bridge(handler) -> LivePaymentBridge:
    return LivePaymentBridge(
        endpoint="https://pay.example.test/api",
        merchant_id="merchant-1",
        secret=SECRET,
        allowed_hosts={"pay.example.test"},
        transport=httpx.MockTransport(handler),
    )


def test_create_checkout_signs_request_and_validates_signed_response():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["signature"] = request.headers["X-Kunlun-Signature"]
        seen["body"] = request.content.decode()
        assert request.url.path == "/api/v1/payments/checkout"
        assert request.headers["X-Kunlun-Merchant"] == "merchant-1"
        assert json.loads(request.content)["idempotency_key"] == "checkout-command-123"
        return response_json({
            "order_id": "ord_123",
            "payment_amount_minor": 12345,
            "currency": "CNY",
            "status": "pending",
            "checkout_url": "https://pay.example.test/checkout/abc",
            "provider_transaction_id": "txn_123",
        })

    result = asyncio.run(bridge(handler).create_checkout(
        order_id="ord_123", payment_amount_minor=12345, currency="CNY",
        idempotency_key="checkout-command-123",
    ))
    assert result.order_id == "ord_123"
    assert result.provider_transaction_id == "txn_123"
    assert hmac.compare_digest(
        seen["signature"],
        sign_bridge_body(seen["body"].encode(),
                         timestamp=int(result.request_timestamp),
                         nonce=result.request_nonce, secret=SECRET),
    )


def test_create_checkout_rejects_unsigned_or_mismatched_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"order_id": "ord_1"})

    with pytest.raises(PaymentBridgeError, match="响应签名"):
        asyncio.run(bridge(handler).create_checkout(
            order_id="ord_1", payment_amount_minor=1, idempotency_key="checkout-command-1",
        ))

    def mismatch(request: httpx.Request) -> httpx.Response:
        return response_json({
            "order_id": "ord_other", "payment_amount_minor": 1, "currency": "CNY",
            "status": "pending", "checkout_url": "https://pay.example.test/c",
            "provider_transaction_id": "txn",
        })

    with pytest.raises(PaymentBridgeError, match="订单不一致"):
        asyncio.run(bridge(mismatch).create_checkout(
            order_id="ord_1", payment_amount_minor=1, idempotency_key="checkout-command-1",
        ))


def test_create_checkout_rejects_invalid_idempotency_key_before_network():
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    with pytest.raises(PaymentBridgeError, match="幂等键"):
        asyncio.run(bridge(handler).create_checkout(
            order_id="ord_1", payment_amount_minor=1, idempotency_key="contains space",
        ))
    assert called is False


def test_webhook_signature_window_and_normalized_idempotency():
    payload = {
        "event_id": "evt_1", "type": "payment.succeeded", "order_id": "ord_1",
        "payment_amount_minor": 100, "currency": "CNY", "provider_transaction_id": "txn_1",
        "status": "paid",
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    b = bridge(lambda request: httpx.Response(500))
    first = b.verify_webhook(raw, signed_headers(raw, nonce="webhook-1"))
    duplicate = b.verify_webhook(raw, signed_headers(raw, nonce="webhook-1"))
    assert first.idempotency_key == "payment:evt_1"
    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert duplicate.payment_amount_minor == 100

    with pytest.raises(PaymentBridgeError, match="时间窗口"):
        b.verify_webhook(raw, signed_headers(raw, timestamp=int(time.time()) - 601, nonce="old"))
    with pytest.raises(PaymentBridgeError, match="正文不一致"):
        changed = raw.replace(b"100", b"101")
        b.verify_webhook(changed, signed_headers(changed, nonce="webhook-1"))


def test_refund_webhook_requires_and_exposes_provider_refund_id():
    payload = {
        "event_id": "evt_refund_1", "type": "payment.refunded", "order_id": "ord_1",
        "payment_amount_minor": 100, "currency": "CNY", "provider_transaction_id": "txn_1",
        "provider_refund_id": "refund_1", "status": "refunded",
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    result = bridge(lambda request: httpx.Response(500)).verify_webhook(
        raw, signed_headers(raw, nonce="refund-webhook-1"),
    )
    assert result.provider_refund_id == "refund_1"

    payload.pop("provider_refund_id")
    missing = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    with pytest.raises(PaymentBridgeError, match="退款号"):
        bridge(lambda request: httpx.Response(500)).verify_webhook(
            missing, signed_headers(missing, nonce="refund-webhook-2"),
        )


def test_closed_webhook_is_authenticated_and_normalized():
    payload = {
        "event_id": "evt_closed_1", "type": "payment.closed", "order_id": "ord_1",
        "payment_amount_minor": 100, "currency": "CNY", "provider_transaction_id": "txn_1",
        "status": "closed",
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    result = bridge(lambda request: httpx.Response(500)).verify_webhook(
        raw, signed_headers(raw, nonce="closed-webhook-1"),
    )
    assert result.status == "closed"
    assert result.event_type == "payment.closed"


def test_reconcile_can_query_by_merchant_order_when_provider_transaction_is_unknown():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/payments/reconcile"
        assert "provider_transaction_id" not in json.loads(request.content)
        return response_json({
            "order_id": "ord_unknown", "payment_amount_minor": 100, "currency": "CNY",
            "status": "paid", "provider_transaction_id": "txn_discovered",
        })

    result = asyncio.run(bridge(handler).reconcile_payment(
        order_id="ord_unknown", payment_amount_minor=100, currency="CNY",
        provider_transaction_id=None,
    ))
    assert result.provider_transaction_id == "txn_discovered"


@pytest.mark.parametrize("method,path", [("query_payment", "/v1/payments/query"),
                                           ("refund_payment", "/v1/payments/refund"),
                                           ("reconcile_payment", "/v1/payments/reconcile")])
def test_query_refund_reconcile_use_signed_protocol(method, path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api" + path
        return response_json({
            "order_id": "ord_1", "payment_amount_minor": 100, "currency": "CNY",
            "status": "paid", "provider_transaction_id": "txn_1",
            **({"provider_refund_id": "refund_1"} if method == "refund_payment" else {}),
        })

    result = asyncio.run(getattr(bridge(handler), method)(
        order_id="ord_1", payment_amount_minor=100, currency="CNY",
        provider_transaction_id="txn_1",
        **({"idempotency_key": "refund-command-1"} if method == "refund_payment" else {}),
    ))
    assert result.order_id == "ord_1"


def test_strict_amount_currency_and_provider_transaction_validation():
    b = bridge(lambda request: response_json({}))
    with pytest.raises(PaymentBridgeError, match="金额"):
        asyncio.run(b.query_payment(order_id="ord_1", payment_amount_minor=0,
                                    provider_transaction_id="txn"))
    with pytest.raises(PaymentBridgeError, match="币种"):
        asyncio.run(b.query_payment(order_id="ord_1", payment_amount_minor=1,
                                    currency="usd", provider_transaction_id="txn"))
    with pytest.raises(PaymentBridgeError, match="交易号"):
        asyncio.run(b.refund_payment(order_id="ord_1", payment_amount_minor=1,
                                     provider_transaction_id="", idempotency_key="refund-1"))


def test_refund_sends_validated_idempotency_key_to_sidecar():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["idempotency_key"] == "refund-command-1"
        return response_json({
            "order_id": "ord_1", "payment_amount_minor": 100, "currency": "CNY",
            "status": "refunded", "provider_transaction_id": "txn_1",
            "provider_refund_id": "refund_1",
        })

    result = asyncio.run(bridge(handler).refund_payment(
        order_id="ord_1", payment_amount_minor=100, currency="CNY",
        provider_transaction_id="txn_1", idempotency_key="refund-command-1",
    ))
    assert result.provider_refund_id == "refund_1"
    with pytest.raises(PaymentBridgeError, match="幂等"):
        asyncio.run(bridge(handler).refund_payment(
            order_id="ord_1", payment_amount_minor=100, currency="CNY",
            provider_transaction_id="txn_1", idempotency_key="bad key with spaces",
        ))


def test_transport_and_invalid_json_fail_closed_without_body_leak():
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret-body-provider")

    with pytest.raises(PaymentBridgeError) as exc:
        asyncio.run(bridge(broken).query_payment(order_id="ord_1", payment_amount_minor=1,
                                                 provider_transaction_id="txn"))
    assert "secret-body-provider" not in str(exc.value)
    assert exc.value.safe_to_retry is False

    def invalid(request: httpx.Request) -> httpx.Response:
        body = b"not-json-secret"
        return httpx.Response(200, content=body, headers=signed_headers(body))

    with pytest.raises(PaymentBridgeError, match="JSON"):
        asyncio.run(bridge(invalid).query_payment(order_id="ord_1", payment_amount_minor=1,
                                                  provider_transaction_id="txn"))


@pytest.mark.parametrize("length,accepted", [(120, True), (121, False), (128, False)])
def test_provider_transaction_id_matches_database_length_limit(length: int, accepted: bool):
    transaction_id = "t" + "x" * (length - 1)
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return response_json({
            "order_id": "ord_1", "payment_amount_minor": 1, "currency": "CNY",
            "status": "paid", "provider_transaction_id": transaction_id,
        })

    action = bridge(handler).query_payment(
        order_id="ord_1", payment_amount_minor=1, provider_transaction_id=transaction_id,
    )
    if accepted:
        assert asyncio.run(action).provider_transaction_id == transaction_id
    else:
        with pytest.raises(PaymentBridgeError, match="交易号"):
            asyncio.run(action)
        assert called is False


@pytest.mark.parametrize("length,accepted", [(120, True), (121, False), (128, False)])
def test_webhook_event_refund_and_nonce_match_database_length_limit(length: int, accepted: bool):
    value = "e" + "x" * (length - 1)
    payload = {
        "event_id": value,
        "type": "payment.refunded",
        "order_id": "ord_1",
        "payment_amount_minor": 1,
        "currency": "CNY",
        "provider_transaction_id": "txn_1",
        "provider_refund_id": "r" + "x" * (length - 1),
        "status": "refunded",
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    headers = signed_headers(raw, nonce="n" + "x" * (length - 1))
    if accepted:
        result = bridge(lambda request: httpx.Response(500)).verify_webhook(raw, headers)
        assert result.event_id == value
        assert result.provider_refund_id == "r" + "x" * (length - 1)
    else:
        with pytest.raises(PaymentBridgeError):
            bridge(lambda request: httpx.Response(500)).verify_webhook(raw, headers)


@pytest.mark.parametrize("header,value,match", [
    ("X-Kunlun-Timestamp", "9" * 128, "时间戳"),
    ("X-Kunlun-Timestamp", "not-an-int", "时间戳"),
    ("X-Kunlun-Nonce", "n" * 121, "nonce"),
    ("X-Kunlun-Signature", "s" * 128, "签名"),
])
def test_malformed_signed_headers_fail_closed_without_int_or_length_leaks(header: str, value: str, match: str):
    raw = b'{"event_id":"evt_1"}'
    headers = signed_headers(raw)
    headers[header] = value
    with pytest.raises(PaymentBridgeError, match=match) as exc:
        bridge(lambda request: httpx.Response(500)).verify_webhook(raw, headers)
    assert "invalid literal" not in str(exc.value)
