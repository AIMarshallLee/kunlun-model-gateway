from __future__ import annotations

import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app import create_app
from app.models import (
    ApiKey, LedgerEntry, LedgerTransaction, OperatorAction, PaymentOrder,
    PaymentRefund, User, Wallet,
)
from app.services.live_payments import CheckoutResult, PaymentBridgeError, RefundResult, WebhookResult
from app.services.ops_tokens import mint_operator_token
from app.services.payment_domain import PaymentDomainService
from app.security import utcnow


OPS_SECRET = "payment-operator-secret-with-at-least-thirty-two-bytes"


class FakeLiveBridge:
    def __init__(self):
        self.checkout_calls = []
        self.refund_calls = []
        self.reconcile_calls = []
        self.webhook = WebhookResult(
            order_id="placeholder",
            payment_amount_minor=1999,
            currency="CNY",
            status="paid",
            provider_transaction_id="wx_tx_1",
            event_id="wx_evt_1",
            event_type="payment.succeeded",
            nonce="wx_nonce_1",
            idempotency_key="payment:wx_evt_1",
        )

    async def create_checkout(self, **kwargs):
        self.checkout_calls.append(kwargs)
        call_number = len(self.checkout_calls)
        return CheckoutResult(
            order_id=kwargs["order_id"],
            payment_amount_minor=kwargs["payment_amount_minor"],
            currency=kwargs["currency"],
            status="pending",
            checkout_url="https://pay.example.test/checkout/1",
            provider_transaction_id=f"wx_tx_{call_number}",
            request_timestamp="1700000000",
            request_nonce="request_nonce_1",
        )

    def verify_webhook(self, raw_body: bytes, headers):
        assert raw_body == b"signed-provider-event"
        return self.webhook

    async def refund_payment(self, **kwargs):
        self.refund_calls.append(kwargs)
        return RefundResult(
            order_id=kwargs["order_id"],
            payment_amount_minor=kwargs["payment_amount_minor"],
            currency=kwargs["currency"],
            status="refunded",
            provider_transaction_id=kwargs["provider_transaction_id"],
            provider_refund_id="wx_refund_1",
        )

    async def reconcile_payment(self, **kwargs):
        self.reconcile_calls.append(kwargs)
        return RefundResult(
            order_id=kwargs["order_id"],
            payment_amount_minor=kwargs["payment_amount_minor"],
            currency=kwargs["currency"],
            status="paid",
            provider_transaction_id=kwargs.get("provider_transaction_id") or "wx_tx_reconciled",
            provider_refund_id="",
        )


class FailingLiveBridge(FakeLiveBridge):
    async def create_checkout(self, **kwargs):
        raise PaymentBridgeError("支付桥接网络请求失败", code="network_failure")


class NonPendingCheckoutBridge(FakeLiveBridge):
    def __init__(self, status: str):
        super().__init__()
        self.non_pending_status = status

    async def create_checkout(self, **kwargs):
        result = await super().create_checkout(**kwargs)
        return replace(result, status=self.non_pending_status)


class ClosedReconcileBridge(FakeLiveBridge):
    async def reconcile_payment(self, **kwargs):
        self.reconcile_calls.append(kwargs)
        return RefundResult(
            order_id=kwargs["order_id"],
            payment_amount_minor=kwargs["payment_amount_minor"],
            currency=kwargs["currency"],
            status="closed",
            provider_transaction_id=kwargs.get("provider_transaction_id") or "wx_tx_closed",
            provider_refund_id="",
        )


class BlockingCheckoutBridge(FakeLiveBridge):
    def __init__(self):
        super().__init__()
        self.started = Event()
        self.release = Event()

    async def create_checkout(self, **kwargs):
        self.checkout_calls.append(kwargs)
        self.started.set()
        released = await asyncio.to_thread(self.release.wait, 5)
        if not released:
            raise AssertionError("test checkout release timed out")
        return CheckoutResult(
            order_id=kwargs["order_id"],
            payment_amount_minor=kwargs["payment_amount_minor"],
            currency=kwargs["currency"],
            status="pending",
            checkout_url="https://pay.example.test/checkout/blocked",
            provider_transaction_id="wx_tx_blocked",
            request_timestamp="1700000000",
            request_nonce="request_nonce_blocked",
        )

    async def reconcile_payment(self, **kwargs):
        raise PaymentBridgeError("支付桥接网络请求失败", code="network_failure")


class BlockingReconcileBridge(FakeLiveBridge):
    def __init__(self):
        super().__init__()
        self.reconcile_started = Event()
        self.reconcile_release = Event()

    async def reconcile_payment(self, **kwargs):
        self.reconcile_calls.append(kwargs)
        self.reconcile_started.set()
        released = await asyncio.to_thread(self.reconcile_release.wait, 5)
        if not released:
            raise AssertionError("test reconciliation release timed out")
        return RefundResult(
            order_id=kwargs["order_id"],
            payment_amount_minor=kwargs["payment_amount_minor"],
            currency=kwargs["currency"],
            status="paid",
            provider_transaction_id=kwargs.get("provider_transaction_id") or "wx_tx_reconcile_claim",
            provider_refund_id="",
        )


class WebhookWinningRefundBridge(FakeLiveBridge):
    app = None

    async def refund_payment(self, **kwargs):
        self.refund_calls.append(kwargs)
        with self.app.state.SessionLocal() as session:
            PaymentDomainService(session).apply_webhook(
                provider="wechatpay",
                event_id="refund-webhook-won",
                raw_digest="a" * 64,
                order_id=kwargs["order_id"],
                event_type="payment.refunded",
                status="refunded",
                payment_amount_minor=kwargs["payment_amount_minor"],
                payment_currency=kwargs["currency"],
                provider_transaction_id=kwargs["provider_transaction_id"],
                provider_refund_id="wx_refund_race",
            )
        return RefundResult(
            order_id=kwargs["order_id"],
            payment_amount_minor=kwargs["payment_amount_minor"],
            currency=kwargs["currency"],
            status="refunded",
            provider_transaction_id=kwargs["provider_transaction_id"],
            provider_refund_id="wx_refund_race",
        )


def _client(tmp_path, bridge, **overrides):
    packages = {
        "starter": {
            "payment_amount_minor": 1999,
            "payment_currency": "CNY",
            "credit_amount_microusd": 250_000,
        }
    }
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'live-routes.sqlite3'}",
        public_signup=True,
        live_payment_bridge=bridge,
        payment_provider="wechatpay",
        topup_packages=packages,
        operator_signing_secret=OPS_SECRET,
        public_base_url="https://gateway.example",
        provider_clients=[],
        **overrides,
    )
    client = TestClient(app)
    client.__enter__()
    identity = {"email": "payer@example.com", "password": "correct horse battery staple"}
    assert client.post("/auth/register", json=identity).status_code == 201
    token = client.post("/auth/login", json=identity).json()["access_token"]
    return app, client, {"Authorization": f"Bearer {token}"}


def test_live_checkout_and_webhook_keep_cash_and_credit_separate(tmp_path):
    bridge = FakeLiveBridge()
    app, client, headers = _client(tmp_path, bridge)
    try:
        created = client.post(
            "/billing/checkout",
            headers={**headers, "Idempotency-Key": "checkout-1"},
            json={"sku": "starter", "return_url": "https://gateway.example/billing"},
        )
        assert created.status_code == 201
        assert created.json()["payment_amount_minor"] == 1999
        assert created.json()["credit_amount_microusd"] == 250_000
        assert created.json()["payment_currency"] == "CNY"
        order_id = created.json()["id"]
        bridge.webhook = replace(bridge.webhook, order_id=order_id)

        callback = client.post(
            "/billing/live/webhook",
            content=b"signed-provider-event",
            headers={"X-Kunlun-Timestamp": "1"},
        )
        assert callback.status_code == 200
        assert callback.json() == {"received": True, "duplicate": False}
        assert client.post(
            "/billing/live/webhook",
            content=b"signed-provider-event",
            headers={"X-Kunlun-Timestamp": "1"},
        ).json()["duplicate"] is True
        assert client.get("/billing/balance", headers=headers).json()["balance"] == 250_000

        with app.state.SessionLocal() as session:
            order = session.scalar(select(PaymentOrder))
            assert order is not None
            assert order.payment_amount_minor == 1999
            assert order.credit_amount_microusd == 250_000
            assert order.status == "paid"
    finally:
        client.__exit__(None, None, None)


def test_checkout_rejects_cross_origin_return_url_before_payment_bridge(tmp_path):
    bridge = FakeLiveBridge()
    _app, client, headers = _client(tmp_path, bridge)
    try:
        response = client.post(
            "/billing/checkout",
            headers={**headers, "Idempotency-Key": "checkout-evil-return"},
            json={"sku": "starter", "return_url": "https://evil.example/phish"},
        )
        assert response.status_code == 422
        assert bridge.checkout_calls == []
    finally:
        client.__exit__(None, None, None)


def test_checkout_rejects_invalid_idempotency_key_before_creating_order(tmp_path):
    bridge = FakeLiveBridge()
    app, client, headers = _client(tmp_path, bridge)
    try:
        response = client.post(
            "/billing/checkout",
            headers={**headers, "Idempotency-Key": "contains space"},
            json={"sku": "starter"},
        )
        assert response.status_code == 422
        assert bridge.checkout_calls == []
        with app.state.SessionLocal() as session:
            assert session.scalar(select(PaymentOrder)) is None
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("provider_status", ["paid", "closed", "failed"])
def test_non_pending_checkout_response_is_held_for_reconciliation(tmp_path, provider_status):
    bridge = NonPendingCheckoutBridge(provider_status)
    app, client, headers = _client(tmp_path, bridge)
    try:
        response = client.post(
            "/billing/checkout",
            headers={**headers, "Idempotency-Key": f"checkout-provider-{provider_status}"},
            json={"sku": "starter"},
        )
        assert response.status_code == 503
        assert "对账" in response.json()["detail"]
        assert len(bridge.checkout_calls) == 1
        with app.state.SessionLocal() as session:
            order = session.scalar(select(PaymentOrder))
            assert order is not None
            assert order.status == "pending_reconciliation"
            assert order.provider_transaction_id == "wx_tx_1"
            assert order.checkout_url == "https://pay.example.test/checkout/1"
            assert order.checkout_claim_started_at is None
            assert order.risk_reason == f"checkout_provider_status:{provider_status}"
            wallet = session.scalar(select(Wallet).where(Wallet.user_id == order.user_id))
            assert wallet is not None and wallet.balance_microusd == 0
    finally:
        client.__exit__(None, None, None)


def test_checkout_rate_limit_bounds_orders_and_bridge_calls(tmp_path):
    bridge = FakeLiveBridge()
    app, client, headers = _client(
        tmp_path,
        bridge,
        checkout_rate_limit_per_minute=2,
        max_open_checkout_orders=10,
    )
    try:
        responses = [
            client.post(
                "/billing/checkout",
                headers={**headers, "Idempotency-Key": f"checkout-rate-{index}"},
                json={"sku": "starter"},
            )
            for index in range(3)
        ]
        assert [response.status_code for response in responses] == [201, 201, 429]
        assert len(bridge.checkout_calls) == 2
        with app.state.SessionLocal() as session:
            assert len(session.scalars(select(PaymentOrder)).all()) == 2
    finally:
        client.__exit__(None, None, None)


def test_checkout_open_order_cap_prevents_unbounded_payment_intents(tmp_path):
    bridge = FakeLiveBridge()
    app, client, headers = _client(
        tmp_path,
        bridge,
        checkout_rate_limit_per_minute=10,
        max_open_checkout_orders=2,
    )
    try:
        responses = [
            client.post(
                "/billing/checkout",
                headers={**headers, "Idempotency-Key": f"checkout-open-{index}"},
                json={"sku": "starter"},
            )
            for index in range(3)
        ]
        assert [response.status_code for response in responses] == [201, 201, 409]
        assert len(bridge.checkout_calls) == 2
        with app.state.SessionLocal() as session:
            assert len(session.scalars(select(PaymentOrder)).all()) == 2
    finally:
        client.__exit__(None, None, None)


def test_provider_confirmed_closed_order_releases_open_order_capacity(tmp_path):
    bridge = ClosedReconcileBridge()
    app, client, headers = _client(
        tmp_path,
        bridge,
        checkout_rate_limit_per_minute=10,
        max_open_checkout_orders=1,
    )
    try:
        first = client.post(
            "/billing/checkout",
            headers={**headers, "Idempotency-Key": "checkout-abandoned-first"},
            json={"sku": "starter"},
        )
        assert first.status_code == 201
        blocked = client.post(
            "/billing/checkout",
            headers={**headers, "Idempotency-Key": "checkout-abandoned-blocked"},
            json={"sku": "starter"},
        )
        assert blocked.status_code == 409
        ops_token = mint_operator_token(
            OPS_SECRET, subject="finance-oncall@example.com", scopes={"payments:write"},
        )
        reconciled = client.post(
            f"/ops/payments/{first.json()['id']}/reconcile",
            headers={"X-Kunlun-Ops-Token": ops_token},
            json={"reason": "provider confirmed abandoned checkout closed"},
        )
        assert reconciled.status_code == 200
        assert reconciled.json()["status"] == "closed"
        replacement = client.post(
            "/billing/checkout",
            headers={**headers, "Idempotency-Key": "checkout-after-close"},
            json={"sku": "starter"},
        )
        assert replacement.status_code == 201
        with app.state.SessionLocal() as session:
            assert session.get(PaymentOrder, first.json()["id"]).status == "closed"
    finally:
        client.__exit__(None, None, None)


def test_ambiguous_checkout_failure_is_held_for_reconciliation(tmp_path):
    app, client, headers = _client(tmp_path, FailingLiveBridge())
    try:
        response = client.post(
            "/billing/checkout",
            headers={**headers, "Idempotency-Key": "checkout-unknown"},
            json={"sku": "starter"},
        )
        assert response.status_code == 503
        with app.state.SessionLocal() as session:
            order = session.scalar(select(PaymentOrder))
            assert order is not None and order.status == "pending_reconciliation"
            assert order.risk_reason == "network_failure"
    finally:
        client.__exit__(None, None, None)


def test_concurrent_same_checkout_key_calls_payment_bridge_once(tmp_path):
    bridge = BlockingCheckoutBridge()
    app, client, headers = _client(tmp_path, bridge)
    request_headers = {**headers, "Idempotency-Key": "checkout-concurrent-one"}
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                client.post,
                "/billing/checkout",
                headers=request_headers,
                json={"sku": "starter"},
            )
            assert bridge.started.wait(5)
            second = client.post(
                "/billing/checkout", headers=request_headers, json={"sku": "starter"},
            )
            assert second.status_code == 409
            bridge.release.set()
            first_response = first.result(timeout=5)
        assert first_response.status_code == 201
        assert len(bridge.checkout_calls) == 1
        assert bridge.checkout_calls[0]["idempotency_key"] == "checkout-concurrent-one"
        repeated = client.post(
            "/billing/checkout", headers=request_headers, json={"sku": "starter"},
        )
        assert repeated.status_code == 201
        assert repeated.json()["checkout_url"].endswith("/blocked")
        assert len(bridge.checkout_calls) == 1
        with app.state.SessionLocal() as session:
            order = session.scalar(select(PaymentOrder))
            assert order is not None
            assert order.status == "pending"
            assert order.checkout_claim_started_at is None
    finally:
        bridge.release.set()
        client.__exit__(None, None, None)


def test_scoped_operator_can_apply_full_refund_after_provider_confirmation(tmp_path):
    bridge = FakeLiveBridge()
    app, client, headers = _client(tmp_path, bridge)
    try:
        created = client.post(
            "/billing/checkout",
            headers={**headers, "Idempotency-Key": "checkout-refund"},
            json={"sku": "starter"},
        ).json()
        bridge.webhook = replace(bridge.webhook, order_id=created["id"])
        assert client.post("/billing/live/webhook", content=b"signed-provider-event").status_code == 200
        ops_token = mint_operator_token(
            OPS_SECRET,
            subject="finance-oncall@example.com",
            scopes={"payments:write"},
        )
        refunded = client.post(
            f"/ops/payments/{created['id']}/refund",
            headers={"X-Kunlun-Ops-Token": ops_token},
            json={
                "reason": "customer request approved by finance",
                "idempotency_key": "refund-1",
            },
        )
        assert refunded.status_code == 200
        assert refunded.json()["status"] == "refunded"
        assert bridge.refund_calls[0]["idempotency_key"] == "refund-1"
        repeated = client.post(
            f"/ops/payments/{created['id']}/refund",
            headers={"X-Kunlun-Ops-Token": ops_token},
            json={
                "reason": "customer request approved by finance",
                "idempotency_key": "refund-1",
            },
        )
        assert repeated.status_code == 200
        assert repeated.json()["refund_id"] == refunded.json()["refund_id"]
        assert len(bridge.refund_calls) == 1
        assert client.get("/billing/balance", headers=headers).json()["balance"] == 0
        with app.state.SessionLocal() as session:
            order = session.get(PaymentOrder, created["id"])
            assert order is not None and order.status == "refunded"
    finally:
        client.__exit__(None, None, None)


def test_refund_webhook_winning_race_still_commits_operator_audit(tmp_path):
    bridge = WebhookWinningRefundBridge()
    app, client, headers = _client(tmp_path, bridge)
    bridge.app = app
    try:
        created = client.post(
            "/billing/checkout",
            headers={**headers, "Idempotency-Key": "checkout-refund-race"},
            json={"sku": "starter"},
        ).json()
        bridge.webhook = replace(bridge.webhook, order_id=created["id"])
        assert client.post("/billing/live/webhook", content=b"signed-provider-event").status_code == 200
        ops_token = mint_operator_token(
            OPS_SECRET, subject="finance-oncall@example.com", scopes={"payments:write"},
        )
        response = client.post(
            f"/ops/payments/{created['id']}/refund",
            headers={"X-Kunlun-Ops-Token": ops_token},
            json={
                "reason": "provider webhook may win the refund response race",
                "idempotency_key": "refund-race",
            },
        )
        assert response.status_code == 200
        with app.state.SessionLocal() as session:
            actions = session.scalars(select(OperatorAction).where(
                OperatorAction.action == "payment_refund",
            )).all()
            assert len(actions) == 1
            assert actions[0].actor == "finance-oncall@example.com"
            assert (actions[0].target_type, actions[0].target_id) == (
                "payment_refund", response.json()["refund_id"],
            )
    finally:
        client.__exit__(None, None, None)


def test_risk_refund_reclaims_available_credit_and_blocks_unfreeze(tmp_path):
    bridge = FakeLiveBridge()
    app, client, headers = _client(tmp_path, bridge)
    try:
        created = client.post(
            "/billing/checkout",
            headers={**headers, "Idempotency-Key": "checkout-risk-refund"},
            json={"sku": "starter"},
        ).json()
        bridge.webhook = replace(bridge.webhook, order_id=created["id"])
        assert client.post("/billing/live/webhook", content=b"signed-provider-event").status_code == 200
        with app.state.SessionLocal() as session:
            user_id = session.scalar(select(User.id).where(User.email == "payer@example.com"))
            session.get(Wallet, user_id).balance_microusd = 40_000
            session.commit()
        payment_token = mint_operator_token(
            OPS_SECRET, subject="finance-oncall@example.com", scopes={"payments:write"},
        )
        refunded = client.post(
            f"/ops/payments/{created['id']}/refund",
            headers={"X-Kunlun-Ops-Token": payment_token},
            json={
                "reason": "cash refund confirmed with partial credit remaining",
                "idempotency_key": "refund-risk-partial",
            },
        )
        assert refunded.status_code == 200
        assert refunded.json()["status"] == "risk"
        account_token = mint_operator_token(
            OPS_SECRET, subject="risk-oncall@example.com", scopes={"accounts:write"},
        )
        blocked = client.post(
            f"/ops/accounts/{user_id}/status",
            headers={"X-Kunlun-Ops-Token": account_token},
            json={"action": "unfreeze", "reason": "attempt before refund debt disposition"},
        )
        assert blocked.status_code == 409
        with app.state.SessionLocal() as session:
            assert session.get(User, user_id).status == "frozen"
            assert session.get(Wallet, user_id).balance_microusd == 0
            risk = session.scalar(select(PaymentRefund).where(PaymentRefund.order_id == created["id"]))
            assert risk is not None and risk.status == "risk"
            risk_id = risk.id
        wrong_scope = client.post(
            f"/ops/refunds/{risk_id}/risk-disposition",
            headers={"X-Kunlun-Ops-Token": payment_token},
            json={
                "action": "write_off",
                "reason": "finance approved residual loss after investigation",
                "idempotency_key": "risk-disposition-1",
            },
        )
        assert wrong_scope.status_code == 401
        risk_token = mint_operator_token(
            OPS_SECRET,
            subject="risk-finance@example.com",
            scopes={"payments:risk:write"},
        )
        disposition_payload = {
            "action": "write_off",
            "reason": "finance approved residual loss after investigation",
            "idempotency_key": "risk-disposition-1",
        }
        disposed = client.post(
            f"/ops/refunds/{risk_id}/risk-disposition",
            headers={"X-Kunlun-Ops-Token": risk_token},
            json=disposition_payload,
        )
        assert disposed.status_code == 200
        assert disposed.json()["status"] == "resolved"
        assert disposed.json()["written_off_microusd"] == 210_000
        assert disposed.json()["account_unfrozen"] is False
        repeated = client.post(
            f"/ops/refunds/{risk_id}/risk-disposition",
            headers={"X-Kunlun-Ops-Token": risk_token},
            json=disposition_payload,
        )
        assert repeated.status_code == 200 and repeated.json()["duplicate"] is True
        different_key = client.post(
            f"/ops/refunds/{risk_id}/risk-disposition",
            headers={"X-Kunlun-Ops-Token": risk_token},
            json={**disposition_payload, "idempotency_key": "risk-disposition-2"},
        )
        assert different_key.status_code == 409
        with app.state.SessionLocal() as session:
            assert session.get(User, user_id).status == "frozen"
            assert session.get(PaymentRefund, risk_id).status == "resolved"
            action = session.scalar(select(OperatorAction).where(
                OperatorAction.action == "refund_risk_write_off",
            ))
            assert action is not None and action.actor == "risk-finance@example.com"
            assert (action.target_type, action.target_id) == ("payment_refund", risk_id)
            tx_ids = session.scalars(select(LedgerTransaction.id)).all()
            for tx_id in tx_ids:
                total = sum(session.scalars(select(LedgerEntry.amount_microusd).where(
                    LedgerEntry.transaction_id == tx_id,
                )).all())
                assert total == 0
        unfreezed = client.post(
            f"/ops/accounts/{user_id}/status",
            headers={"X-Kunlun-Ops-Token": account_token},
            json={"action": "unfreeze", "reason": "refund risk disposition is complete"},
        )
        assert unfreezed.status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_operator_can_reconcile_ambiguous_checkout_and_audit_action(tmp_path):
    bridge = FakeLiveBridge()
    app, client, headers = _client(tmp_path, bridge)
    try:
        created = client.post(
            "/billing/checkout",
            headers={**headers, "Idempotency-Key": "checkout-reconcile"},
            json={"sku": "starter"},
        ).json()
        with app.state.SessionLocal() as session:
            session.query(PaymentOrder).filter(PaymentOrder.id == created["id"]).update({
                PaymentOrder.status: "pending_reconciliation",
                PaymentOrder.provider_transaction_id: None,
            })
            session.commit()
        ops_token = mint_operator_token(
            OPS_SECRET, subject="finance-oncall@example.com", scopes={"payments:write"},
        )
        response = client.post(
            f"/ops/payments/{created['id']}/reconcile",
            headers={"X-Kunlun-Ops-Token": ops_token},
            json={"reason": "provider query confirmed settlement"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "paid"
        assert client.get("/billing/balance", headers=headers).json()["balance"] == 250_000
        with app.state.SessionLocal() as session:
            assert session.get(PaymentOrder, created["id"]).provider_transaction_id == "wx_tx_reconciled"
            action = session.scalar(select(OperatorAction).where(
                OperatorAction.action == "payment_reconcile",
            ))
            assert action is not None and action.actor == "finance-oncall@example.com"
            assert (action.target_type, action.target_id) == ("payment_order", created["id"])
    finally:
        client.__exit__(None, None, None)


def test_private_payment_queue_discovers_orders_and_refunds_with_pagination(tmp_path):
    bridge = FakeLiveBridge()
    app, client, _headers = _client(tmp_path, bridge)
    try:
        with app.state.SessionLocal() as session:
            user_id = session.scalar(select(User.id).where(User.email == "payer@example.com"))
            service = PaymentDomainService(session)
            pending_order = service.create_order(
                user_id=user_id, provider="wechatpay", payment_amount_minor=1999,
                payment_currency="CNY", credit_amount_microusd=250_000,
                quote_id="queue-pending", quote_numerator=250_000, quote_denominator=1999,
                idempotency_key="queue-pending",
            )
            pending_order.status = "pending_reconciliation"
            pending_order.risk_reason = "network_failure"
            session.commit()

            paid_order = service.create_order(
                user_id=user_id, provider="wechatpay", payment_amount_minor=1999,
                payment_currency="CNY", credit_amount_microusd=250_000,
                quote_id="queue-refund", quote_numerator=250_000, quote_denominator=1999,
                idempotency_key="queue-refund-order",
            )
            service.apply_webhook(
                provider="wechatpay", event_id="queue-refund-paid", raw_digest="9" * 64,
                order_id=paid_order.id, event_type="payment.succeeded", status="paid",
                payment_amount_minor=1999, payment_currency="CNY",
                provider_transaction_id="queue-refund-txn",
            )
            refund, _ = service.prepare_refund(
                order_id=paid_order.id, idempotency_key="queue-refund-command",
            )
            refund.status = "pending_reconciliation"
            refund.risk_reason = "network_failure"
            refund.claim_started_at = utcnow() - timedelta(minutes=10)
            session.commit()
            pending_order_id = pending_order.id
            refund_id = refund.id

        token = mint_operator_token(
            OPS_SECRET, subject="finance-reader@example.com", scopes={"payments:read"},
        )
        response = client.get(
            "/ops/payments/reconciliation?limit=1&offset=0",
            headers={"X-Kunlun-Ops-Token": token},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["pagination"]["limit"] == 1
        assert payload["pagination"]["offset"] == 0
        assert payload["pagination"]["order_total"] >= 1
        assert payload["pagination"]["refund_total"] >= 1
        assert any(item["order_id"] == pending_order_id for item in payload["orders"])
        assert any(item["refund_id"] == refund_id for item in payload["refunds"])
        assert payload["refunds"][0]["idempotency_key"] == "queue-refund-command"
    finally:
        client.__exit__(None, None, None)


def test_payment_reconciliation_claim_lease_blocks_parallel_operator_calls(tmp_path):
    bridge = BlockingReconcileBridge()
    app, client, _headers = _client(tmp_path, bridge)
    try:
        with app.state.SessionLocal() as session:
            user_id = session.scalar(select(User.id).where(User.email == "payer@example.com"))
            order = PaymentDomainService(session).create_order(
                user_id=user_id, provider="wechatpay", payment_amount_minor=1999,
                payment_currency="CNY", credit_amount_microusd=250_000,
                quote_id="reconcile-claim", quote_numerator=250_000, quote_denominator=1999,
                idempotency_key="reconcile-claim",
            )
            order.status = "pending_reconciliation"
            order.risk_reason = "checkout_unknown"
            session.commit()
            order_id = order.id
        token = mint_operator_token(
            OPS_SECRET, subject="finance-oncall@example.com", scopes={"payments:write"},
        )
        request_headers = {"X-Kunlun-Ops-Token": token}
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                client.post,
                f"/ops/payments/{order_id}/reconcile",
                headers=request_headers,
                json={"reason": "first operator owns the reconciliation lease"},
            )
            assert bridge.reconcile_started.wait(5)
            second = client.post(
                f"/ops/payments/{order_id}/reconcile",
                headers=request_headers,
                json={"reason": "second operator must not query in parallel"},
            )
            assert second.status_code == 409
            bridge.reconcile_release.set()
            assert first.result(timeout=5).status_code == 200
        assert len(bridge.reconcile_calls) == 1
        with app.state.SessionLocal() as session:
            actions = session.scalars(select(OperatorAction).where(
                OperatorAction.target_id == order_id,
            ).order_by(OperatorAction.created_at)).all()
            assert [item.action for item in actions] == [
                "payment_reconcile_claim", "payment_reconcile",
            ]
    finally:
        bridge.reconcile_release.set()
        client.__exit__(None, None, None)


def test_operator_can_recover_stale_checkout_claim_without_customer_retry(tmp_path):
    bridge = FakeLiveBridge()
    app, client, _headers = _client(tmp_path, bridge)
    try:
        with app.state.SessionLocal() as session:
            user_id = session.scalar(select(User.id).where(User.email == "payer@example.com"))
            order = PaymentDomainService(session).create_order(
                user_id=user_id,
                provider="wechatpay",
                payment_amount_minor=1999,
                payment_currency="CNY",
                credit_amount_microusd=250_000,
                quote_id="starter",
                quote_numerator=250_000,
                quote_denominator=1999,
                idempotency_key="checkout-crashed-before-save",
            )
            claimed, _ = PaymentDomainService(session).prepare_checkout(order_id=order.id)
            claimed.checkout_claim_started_at = utcnow() - timedelta(minutes=10)
            session.commit()
            order_id = order.id
        ops_token = mint_operator_token(
            OPS_SECRET, subject="finance-oncall@example.com", scopes={"payments:write"},
        )
        response = client.post(
            f"/ops/payments/{order_id}/reconcile",
            headers={"X-Kunlun-Ops-Token": ops_token},
            json={"reason": "checkout lease expired without customer retry"},
        )
        assert response.status_code == 200
        assert len(bridge.reconcile_calls) == 1
        with app.state.SessionLocal() as session:
            recovered = session.get(PaymentOrder, order_id)
            assert recovered.status == "paid"
            action = session.scalar(select(OperatorAction).where(
                OperatorAction.action == "payment_reconcile",
            ))
            assert action is not None and action.before_status == "checkout_requesting"
    finally:
        client.__exit__(None, None, None)


def test_reconciliation_domain_commit_also_persists_operator_audit(tmp_path, monkeypatch):
    bridge = FakeLiveBridge()
    app, client, headers = _client(tmp_path, bridge)
    try:
        created = client.post(
            "/billing/checkout",
            headers={**headers, "Idempotency-Key": "checkout-audit-atomic"},
            json={"sku": "starter"},
        ).json()
        with app.state.SessionLocal() as session:
            order = session.get(PaymentOrder, created["id"])
            order.status = "pending_reconciliation"
            order.provider_transaction_id = None
            session.commit()
        original = PaymentDomainService.apply_webhook

        def commit_then_crash(self, **kwargs):
            original(self, **kwargs)
            raise RuntimeError("simulated process crash after domain commit")

        monkeypatch.setattr(PaymentDomainService, "apply_webhook", commit_then_crash)
        ops_token = mint_operator_token(
            OPS_SECRET, subject="finance-oncall@example.com", scopes={"payments:write"},
        )
        with pytest.raises(RuntimeError, match="simulated process crash"):
            client.post(
                f"/ops/payments/{created['id']}/reconcile",
                headers={"X-Kunlun-Ops-Token": ops_token},
                json={"reason": "verify audit shares payment commit"},
            )
        with app.state.SessionLocal() as session:
            assert session.get(PaymentOrder, created["id"]).status == "paid"
            actions = session.scalars(select(OperatorAction).where(
                OperatorAction.action == "payment_reconcile",
            )).all()
            assert len(actions) == 1
    finally:
        client.__exit__(None, None, None)


def test_paid_order_cannot_reenter_reconciliation_or_be_credited_twice(tmp_path):
    bridge = FailingLiveBridge()
    app, client, headers = _client(tmp_path, bridge)
    try:
        # Use the base implementation for checkout, then settle the order.
        created = FakeLiveBridge.create_checkout
        bridge.create_checkout = created.__get__(bridge, FailingLiveBridge)
        order = client.post(
            "/billing/checkout",
            headers={**headers, "Idempotency-Key": "checkout-paid-once"},
            json={"sku": "starter"},
        ).json()
        bridge.webhook = replace(bridge.webhook, order_id=order["id"])
        assert client.post("/billing/live/webhook", content=b"signed-provider-event").status_code == 200
        assert client.get("/billing/balance", headers=headers).json()["balance"] == 250_000
        ops_token = mint_operator_token(
            OPS_SECRET, subject="finance-oncall@example.com", scopes={"payments:write"},
        )
        blocked = client.post(
            f"/ops/payments/{order['id']}/reconcile",
            headers={"X-Kunlun-Ops-Token": ops_token},
            json={"reason": "must not reopen a completed payment"},
        )
        assert blocked.status_code == 409
        assert client.get("/billing/balance", headers=headers).json()["balance"] == 250_000
        with app.state.SessionLocal() as session:
            assert session.get(PaymentOrder, order["id"]).status == "paid"
    finally:
        client.__exit__(None, None, None)


def test_operator_freeze_revokes_keys_and_sessions_without_public_route(tmp_path):
    bridge = FakeLiveBridge()
    app, client, headers = _client(tmp_path, bridge)
    try:
        key = client.post("/v1/keys", headers=headers, json={"name": "to-revoke"})
        assert key.status_code == 201
        with app.state.SessionLocal() as session:
            user_id = session.scalar(select(User.id).where(User.email == "payer@example.com"))
        ops_token = mint_operator_token(
            OPS_SECRET, subject="risk-oncall@example.com", scopes={"accounts:write"},
        )
        frozen = client.post(
            f"/ops/accounts/{user_id}/status",
            headers={"X-Kunlun-Ops-Token": ops_token},
            json={"action": "freeze", "reason": "confirmed payment abuse investigation"},
        )
        assert frozen.status_code == 200
        with app.state.SessionLocal() as session:
            assert session.get(User, user_id).status == "frozen"
            assert session.scalar(select(ApiKey.status).where(ApiKey.user_id == user_id)) == "revoked"
            action = session.scalar(select(OperatorAction).where(
                OperatorAction.action == "account_freeze",
            ))
            assert action is not None
            assert (action.target_type, action.target_id) == ("user", user_id)
        assert client.get("/billing/balance", headers=headers).status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_operator_audit_rows_are_append_only_in_local_runtime(tmp_path):
    bridge = FakeLiveBridge()
    app, client, _headers = _client(tmp_path, bridge)
    try:
        with app.state.SessionLocal() as session:
            user_id = session.scalar(select(User.id).where(User.email == "payer@example.com"))
        ops_token = mint_operator_token(
            OPS_SECRET, subject="risk-oncall@example.com", scopes={"accounts:write"},
        )
        assert client.post(
            f"/ops/accounts/{user_id}/status",
            headers={"X-Kunlun-Ops-Token": ops_token},
            json={"action": "freeze", "reason": "create immutable operator audit row"},
        ).status_code == 200
        with app.state.SessionLocal() as session:
            action_id = session.scalar(select(OperatorAction.id))
            with pytest.raises(DBAPIError, match="append-only"):
                session.execute(OperatorAction.__table__.update().where(
                    OperatorAction.id == action_id,
                ).values(reason="tampered"))
                session.commit()
            session.rollback()
            with pytest.raises(DBAPIError, match="append-only"):
                session.execute(OperatorAction.__table__.delete().where(
                    OperatorAction.id == action_id,
                ))
                session.commit()
    finally:
        client.__exit__(None, None, None)


def test_account_status_change_and_operator_audit_are_atomic(tmp_path, monkeypatch):
    bridge = FakeLiveBridge()
    app, client, _headers = _client(tmp_path, bridge)
    try:
        with app.state.SessionLocal() as session:
            user_id = session.scalar(select(User.id).where(User.email == "payer@example.com"))
        app_module = importlib.import_module("app")

        def fail_audit(*_args, **_kwargs):
            raise RuntimeError("simulated audit construction crash")

        monkeypatch.setattr(app_module, "OperatorAction", fail_audit)
        ops_token = mint_operator_token(
            OPS_SECRET, subject="risk-oncall@example.com", scopes={"accounts:write"},
        )
        with pytest.raises(RuntimeError, match="audit construction crash"):
            client.post(
                f"/ops/accounts/{user_id}/status",
                headers={"X-Kunlun-Ops-Token": ops_token},
                json={"action": "freeze", "reason": "verify status and audit atomicity"},
            )
        with app.state.SessionLocal() as session:
            assert session.get(User, user_id).status == "active"
            assert session.scalar(select(OperatorAction).where(
                OperatorAction.action == "account_freeze",
            )) is None
    finally:
        client.__exit__(None, None, None)


def test_unfreeze_cannot_bypass_pending_email_verification(tmp_path):
    bridge = FakeLiveBridge()
    app, client, _headers = _client(tmp_path, bridge)
    try:
        with app.state.SessionLocal() as session:
            user = session.scalar(select(User).where(User.email == "payer@example.com"))
            user.status = "pending_email"
            user.email_verified_at = None
            session.commit()
            user_id = user.id
        ops_token = mint_operator_token(
            OPS_SECRET, subject="risk-oncall@example.com", scopes={"accounts:write"},
        )
        frozen = client.post(
            f"/ops/accounts/{user_id}/status",
            headers={"X-Kunlun-Ops-Token": ops_token},
            json={"action": "freeze", "reason": "freeze unverified account for review"},
        )
        assert frozen.status_code == 200
        unfreeze = client.post(
            f"/ops/accounts/{user_id}/status",
            headers={"X-Kunlun-Ops-Token": ops_token},
            json={"action": "unfreeze", "reason": "must not bypass email verification"},
        )
        assert unfreeze.status_code == 409
        with app.state.SessionLocal() as session:
            assert session.get(User, user_id).status == "frozen"
    finally:
        client.__exit__(None, None, None)
