"""Private customer onboarding and metadata-only request recovery."""

from datetime import timedelta
import secrets
from typing import Literal
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .auth import Principal, require_api_key, require_operator_scope, require_session
from .models import ModelRequest, OperatorAction, PasswordResetToken, ProviderAttempt, User, Wallet
from .schemas import StrictModel
from .security import hash_password, normalize_email, token_digest, utcnow
from .services.ops_tokens import OperatorClaims

router = APIRouter()


@router.get("/v1/provider-catalog")
def provider_catalog(request: Request, _principal: Principal = Depends(require_session)):
    return {"data": [{"provider": item["name"], "models": item["models"]}
                     for item in request.app.state.settings.providers]}


class RecoveryRequest(StrictModel):
    operation_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    identity_confirmed: Literal[True]
    reason: str = Field(min_length=10, max_length=500)


class InvitationRequest(RecoveryRequest):
    email: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalize_email(value)


def issue_activation(request, payload, operator, *, user_id=None):
    settings = request.app.state.settings
    if not settings.identity_token_pepper_persisted or len(settings.identity_token_pepper) < 32:
        raise HTTPException(503, "开通服务需要持久化 KUNLUN_IDENTITY_TOKEN_PEPPER")
    now = utcnow()
    raw = "reset_" + secrets.token_urlsafe(32)
    with request.app.state.SessionLocal() as db:
        if user_id is None:
            user = User(id=str(uuid.uuid4()), email=payload.email,
                        password_hash=hash_password(secrets.token_urlsafe(48)),
                        status="active", email_verified_at=now)
            db.add(user)
            # Flush parent before dependants; the session disables autoflush.
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                raise HTTPException(409, "账户已存在；请使用受控恢复流程") from None
            db.add(Wallet(user_id=user.id))
        else:
            user = db.scalar(select(User).where(User.id == user_id).with_for_update())
            if user is None or user.status != "active":
                raise HTTPException(404, "账户不存在或不可恢复")
        db.execute(update(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.consumed_at.is_(None),
        ).values(consumed_at=now))
        db.add(PasswordResetToken(id=str(uuid.uuid4()), user_id=user.id,
               token_digest=token_digest(raw, settings.identity_token_pepper),
               expires_at=now + timedelta(hours=1)))
        db.add(OperatorAction(
            id=str(uuid.uuid4()), target_type="account", target_id=user.id,
            action="account_invitation" if user_id is None else "account_recovery",
            reason=payload.reason, actor=operator.subject,
            scopes=",".join(sorted(operator.scopes)), token_id=operator.token_id,
            operation_id=payload.operation_id, after_status=user.status,
        ))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "该操作已执行；链接只返回一次，请勿自动重复开通") from None
        return JSONResponse(status_code=201, headers={"Cache-Control": "no-store"}, content={
            "user_id": user.id, "activation_path": f"/reset-password#token={raw}",
            "expires_in": 3600,
            "delivery": "仅通过已核验的客户渠道传递；请勿放入工单或日志",
        })


@router.post("/ops/accounts/invitations", status_code=201)
def invite(payload: InvitationRequest, request: Request,
           operator: OperatorClaims = Depends(require_operator_scope("accounts:invite"))):
    return issue_activation(request, payload, operator)


@router.post("/ops/accounts/{user_id}/recovery", status_code=201)
def recover(user_id: str, payload: RecoveryRequest, request: Request,
            operator: OperatorClaims = Depends(require_operator_scope("accounts:invite"))):
    return issue_activation(request, payload, operator, user_id=user_id)


def request_metadata(item: ModelRequest) -> dict:
    if item.status == "reserved":
        action = "wait_for_completion"
    elif item.status == "pending_reconciliation":
        action = "contact_operator_for_reconciliation"
    elif item.status in {"settled", "under_reserved"}:
        action = "check_client_output_before_explicit_new_task"
    else:
        action = "review_failure_before_explicit_new_task"
    return {
        "request_id": item.id, "status": item.status, "cost_state": item.cost_state,
        "model": item.requested_model, "provider": item.final_provider,
        "upstream_cost_microusd": item.upstream_cost_microusd,
        "reserved_microusd": item.reserved_microusd,
        "input_tokens": item.input_tokens, "output_tokens": item.output_tokens,
        "usage_estimated": item.usage_estimated, "failure_category": item.failure_category,
        "response_retained": False, "automatic_resubmit_allowed": False,
        "next_action": action,
    }


def recorded_request_response(db, user_id, idempotency_key):
    item = db.scalar(select(ModelRequest).where(
        ModelRequest.user_id == user_id, ModelRequest.idempotency_key == idempotency_key,
    ))
    if item is None:
        return None
    return JSONResponse(status_code=409, headers={"X-Request-Id": item.id, "Cache-Control": "no-store"}, content={
        "error": {"type": "kunlun_gateway_error", "code": "request_already_recorded",
                  "request_id": item.id,
                  "message": "请求已记录，未再次调用模型。请查询任务状态；网关不保存回答正文，不能重放原回答。"},
        "request": request_metadata(item),
    })


def owned_request(request, principal, request_id):
    with request.app.state.SessionLocal() as db:
        item = db.scalar(select(ModelRequest).where(
            ModelRequest.id == request_id, ModelRequest.user_id == principal.user_id,
        ))
        if item is None:
            raise HTTPException(404, "请求不存在")
        result = request_metadata(item)
        attempts = db.scalars(select(ProviderAttempt).where(
            ProviderAttempt.request_id == item.id,
        ).order_by(ProviderAttempt.ordinal)).all()
        result["attempts"] = [{
            "ordinal": a.ordinal, "provider": a.provider, "model": a.model,
            "status": a.status, "billing_status": a.billing_status,
            "is_final": a.is_final, "failure_category": a.failure_category,
            "input_tokens": a.input_tokens, "output_tokens": a.output_tokens,
            "upstream_cost_microusd": a.upstream_cost_microusd,
        } for a in attempts]
        return JSONResponse(content=result, headers={"Cache-Control": "no-store"})


@router.post("/v1/requests/lookup")
def lookup_request(request: Request, principal: Principal = Depends(require_api_key),
                   idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=120,
                                                pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")):
    # Header avoids placing a business idempotency identifier in access logs.
    with request.app.state.SessionLocal() as db:
        item_id = db.scalar(select(ModelRequest.id).where(
            ModelRequest.user_id == principal.user_id, ModelRequest.idempotency_key == idempotency_key,
        ))
    if item_id is None:
        raise HTTPException(404, "请求不存在；不代表另一个在途请求不会随后被受理")
    return owned_request(request, principal, item_id)


@router.get("/v1/requests/{request_id}")
def api_request_status(request_id: str, request: Request, principal: Principal = Depends(require_api_key)):
    return owned_request(request, principal, request_id)


@router.get("/requests/{request_id}")
def console_request_status(request_id: str, request: Request, principal: Principal = Depends(require_session)):
    return owned_request(request, principal, request_id)
