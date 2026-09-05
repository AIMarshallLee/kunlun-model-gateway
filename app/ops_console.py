"""Private operator projections and key controls; no credential serialization."""

import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import Field
from sqlalchemy import func, select

from .auth import require_operator_scope
from .models import ApiKey, ModelRequest, OperatorAction, PaymentOrder, PaymentRefund, ProviderAttempt, User, Wallet
from .schemas import StrictModel
from .security import token_digest
from .services.gateway_billing import key_usage

router = APIRouter()
ACCOUNT_FIELDS = ("id", "email", "status", "email_verified_at", "created_at")
ORDER_FIELDS = ("id", "user_id", "status", "provider", "credit_amount_microusd", "payment_amount_minor",
                "payment_currency", "provider_transaction_id", "risk_reason", "created_at", "paid_at", "refunded_at")
REQUEST_FIELDS = ("id", "user_id", "api_key_id", "status", "cost_state", "billing_mode", "requested_model", "final_provider",
                  "reserved_microusd", "charged_microusd", "upstream_cost_microusd", "input_tokens", "output_tokens",
                  "usage_estimated", "failure_category", "final_attempt_id", "created_at", "completed_at")


def project(record, fields):
    return {field: getattr(record, field) for field in fields}


def page(db, query, fields, limit, offset):
    total = int(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)
    return {"items": [project(row, fields) for row in db.scalars(query.offset(offset).limit(limit))],
            "pagination": {"limit": limit, "offset": offset, "total": total}}


@router.get("/ops/console", include_in_schema=False)
def console():
    # Static shell only. Production /ops ingress remains protected; every
    # data/action endpoint separately checks a short-lived scoped credential.
    return FileResponse(Path(__file__).parent / "static" / "ops.html", headers={
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    })


@router.get("/ops/session")
def session_info(request: Request, operator=Depends(require_operator_scope("console:read"))):
    settings = request.app.state.settings
    return {"subject": operator.subject, "scopes": sorted(operator.scopes), "expires_at": operator.expires_at,
            "mode": settings.gateway_mode, "environment": settings.environment,
            "live_payments": settings.live_payments}


@router.get("/ops/accounts")
def accounts(request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=1_000_000),
             _operator=Depends(require_operator_scope("accounts:read"))):
    with request.app.state.SessionLocal() as db:
        return page(db, select(User).order_by(User.created_at.desc(), User.id), ACCOUNT_FIELDS, limit, offset)


@router.get("/ops/accounts/{user_id}")
def account(user_id: str, request: Request, key_limit: int = Query(100, ge=1, le=200),
            key_offset: int = Query(0, ge=0, le=1_000_000),
            key_id: str | None = Query(None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
            _operator=Depends(require_operator_scope("accounts:read"))):
    with request.app.state.SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(404, "Account not found")
        wallet = db.get(Wallet, user_id)
        query = select(ApiKey).where(ApiKey.user_id == user_id)
        if key_id is not None:
            query = query.where(ApiKey.id == key_id)
        total = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)
        keys = db.scalars(query.order_by(ApiKey.created_at.desc(), ApiKey.id).offset(key_offset).limit(key_limit)).all()
        usage = key_usage(db, user_id, key_id)
        return {"account": project(user, ACCOUNT_FIELDS),
                "wallet": project(wallet, ("balance_microusd", "reserved_microusd", "currency")) if wallet else None,
                "keys": [{**project(key, ("id", "name", "last_four", "status", "max_output_tokens", "spend_limit_microusd")),
                          "allowed_models": json.loads(key.allowed_models_json) if key.allowed_models_json else None,
                          **usage.get(key.id, {"spent_microusd": 0, "reserved_microusd": 0})} for key in keys],
                "keys_truncated": key_offset + len(keys) < total,
                "keys_pagination": {"limit": key_limit, "offset": key_offset, "total": total}}


@router.get("/ops/orders")
def orders(request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=1_000_000),
           _operator=Depends(require_operator_scope("payments:read"))):
    with request.app.state.SessionLocal() as db:
        return page(db, select(PaymentOrder).order_by(PaymentOrder.created_at.desc(), PaymentOrder.id), ORDER_FIELDS, limit, offset)


@router.get("/ops/orders/{order_id}")
def order(order_id: str, request: Request, _operator=Depends(require_operator_scope("payments:read"))):
    with request.app.state.SessionLocal() as db:
        row = db.get(PaymentOrder, order_id)
        if row is None:
            raise HTTPException(404, "Order not found")
        refunds = db.scalars(select(PaymentRefund).where(PaymentRefund.order_id == order_id)).all()
        return {"order": project(row, ORDER_FIELDS), "refunds": [project(refund, (
            "id", "order_id", "status", "risk_reason", "idempotency_key", "created_at",
        )) for refund in refunds]}


@router.get("/ops/requests/{request_id}")
def model_request(request_id: str, request: Request, _operator=Depends(require_operator_scope("reconciliation:read"))):
    with request.app.state.SessionLocal() as db:
        row = db.get(ModelRequest, request_id)
        if row is None:
            raise HTTPException(404, "Request not found")
        attempts = db.scalars(select(ProviderAttempt).where(ProviderAttempt.request_id == request_id).order_by(ProviderAttempt.ordinal).limit(100)).all()
        return {"request": project(row, REQUEST_FIELDS), "attempts": [project(attempt, (
            "id", "ordinal", "provider", "model", "status", "billing_status", "is_final", "input_tokens", "output_tokens",
            "upstream_cost_microusd", "failure_category", "started_at", "completed_at",
        )) for attempt in attempts]}


@router.get("/ops/audit")
def audit(request: Request, target_id: str | None = Query(None, min_length=1, max_length=120),
          limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=1_000_000),
          _operator=Depends(require_operator_scope("audit:read"))):
    query = select(OperatorAction).order_by(OperatorAction.created_at.desc(), OperatorAction.id)
    if target_id is not None:
        query = query.where(OperatorAction.target_id == target_id)
    with request.app.state.SessionLocal() as db:
        return page(db, query, ("id", "target_type", "target_id", "action", "reason", "actor", "operation_id",
                               "before_status", "after_status", "created_at"), limit, offset)


class KeyStatusRequest(StrictModel):
    action: Literal["freeze", "unfreeze"]
    expected_status: Literal["active", "frozen"]
    reason: str = Field(min_length=12, max_length=500)


@router.post("/ops/keys/{key_id}/status")
def key_status(key_id: str, payload: KeyStatusRequest, request: Request,
               operator=Depends(require_operator_scope("accounts:write"))):
    with request.app.state.SessionLocal() as db:
        key = db.get(ApiKey, key_id)
        if key is None:
            raise HTTPException(404, "Key not found")
        # Same order as request admission, account freeze and password reset.
        user = db.get(User, key.user_id, with_for_update=True, populate_existing=True)
        key = db.get(ApiKey, key_id, with_for_update=True, populate_existing=True)
        before = "active" if payload.action == "freeze" else "frozen"
        if key.status != before or payload.expected_status != before:
            raise HTTPException(409, "Key state changed; refresh before preparing another action")
        if payload.action == "unfreeze" and (user.status != "active" or user.email_verified_at is None):
            raise HTTPException(409, "Account must be active and verified")
        key.status = "frozen" if payload.action == "freeze" else "active"
        operation_id = str(uuid4())
        db.add(OperatorAction(id=str(uuid4()), target_type="api_key", target_id=key.id,
            action="key_" + payload.action, reason=payload.reason, actor=operator.subject,
            scopes=" ".join(sorted(operator.scopes)), token_id=operator.token_id, operation_id=operation_id,
            source_ip_digest=token_digest(request.client.host if request.client else "unknown", request.app.state.settings.session_pepper),
            before_status=before, after_status=key.status))
        db.commit()
        return {"key_id": key.id, "status": key.status, "operation_id": operation_id}
