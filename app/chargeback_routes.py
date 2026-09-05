"""Private chargeback records and explicitly audited risk disposition."""

from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .auth import require_operator_scope
from .models import OperatorAction, PaymentChargeback, PaymentChargebackReturn
from .schemas import RefundRiskDispositionRequest
from .security import as_utc, token_digest
from .services.chargebacks import resolve_chargeback_risk
from .services.payment_domain import PaymentDomainError

router = APIRouter()


def project_return(row):
    return {field: getattr(row, field) for field in (
        "id", "order_id", "user_id", "chargeback_id", "provider", "provider_dispute_id", "provider_return_id",
        "payment_amount_minor", "payment_currency", "restored_microusd", "canceled_risk_microusd",
        "reversed_loss_microusd", "status", "risk_reason",
    )} | {"created_at": as_utc(row.created_at).isoformat(),
         "applied_at": as_utc(row.applied_at).isoformat() if row.applied_at else None}


@router.get("/ops/chargeback-returns")
def list_returns(request: Request, limit: int = Query(50, ge=1, le=200),
                 offset: int = Query(0, ge=0, le=1_000_000), order_id: str | None = Query(None, max_length=36),
                 _operator=Depends(require_operator_scope("payments:read"))):
    with request.app.state.SessionLocal() as db:
        query = select(PaymentChargebackReturn)
        if order_id is not None:
            query = query.where(PaymentChargebackReturn.order_id == order_id)
        total = db.scalar(select(func.count()).select_from(query.subquery()))
        rows = db.scalars(query.order_by(PaymentChargebackReturn.created_at.desc(), PaymentChargebackReturn.id)
                          .limit(limit).offset(offset))
        return {"items": [project_return(row) for row in rows], "pagination": {"limit": limit, "offset": offset, "total": total}}


@router.get("/ops/chargeback-returns/{return_id}")
def get_return(return_id: str, request: Request,
               _operator=Depends(require_operator_scope("payments:read"))):
    with request.app.state.SessionLocal() as db:
        row = db.get(PaymentChargebackReturn, return_id)
        if row is None:
            raise HTTPException(404, "拒付返还记录不存在")
        return project_return(row)


def project(row):
    return {field: getattr(row, field) for field in (
        "id", "order_id", "user_id", "provider", "provider_dispute_id", "payment_amount_minor",
        "payment_currency", "credit_amount_microusd", "recovered_microusd", "outstanding_microusd",
        "written_off_microusd", "status", "risk_reason",
    )} | {"created_at": as_utc(row.created_at).isoformat(),
         "resolved_at": as_utc(row.resolved_at).isoformat() if row.resolved_at else None}


@router.get("/ops/chargebacks")
def list_chargebacks(request: Request, limit: int = Query(50, ge=1, le=200),
                     offset: int = Query(0, ge=0, le=1_000_000),
                     _operator=Depends(require_operator_scope("payments:read"))):
    with request.app.state.SessionLocal() as db:
        total = db.scalar(select(func.count(PaymentChargeback.id)))
        rows = db.scalars(select(PaymentChargeback).order_by(PaymentChargeback.created_at.desc(), PaymentChargeback.id)
                          .limit(limit).offset(offset))
        return {"items": [project(row) for row in rows], "pagination": {"limit": limit, "offset": offset, "total": total}}


@router.get("/ops/chargebacks/{chargeback_id}")
def get_chargeback(chargeback_id: str, request: Request,
                   _operator=Depends(require_operator_scope("payments:read"))):
    with request.app.state.SessionLocal() as db:
        row = db.get(PaymentChargeback, chargeback_id)
        if row is None:
            raise HTTPException(404, "拒付记录不存在")
        return project(row)


@router.post("/ops/chargebacks/{chargeback_id}/risk-disposition")
def dispose_chargeback(chargeback_id: str, payload: RefundRiskDispositionRequest, request: Request,
                       operator=Depends(require_operator_scope("payments:risk:write"))):
    with request.app.state.SessionLocal() as db:
        try:
            row, duplicate, recovered, written_off = resolve_chargeback_risk(db, chargeback_id,
                action=payload.action, idempotency_key=payload.idempotency_key)
            if not duplicate:
                db.add(OperatorAction(id=str(uuid5(NAMESPACE_URL, f"kunlun/chargeback/{row.id}/{payload.idempotency_key}")),
                    target_type="payment_chargeback", target_id=row.id,
                    action="chargeback_risk_recover" if payload.action == "recover_available" else "chargeback_risk_write_off",
                    actor=operator.subject, scopes=" ".join(sorted(operator.scopes)), token_id=operator.token_id,
                    source_ip_digest=token_digest(request.client.host if request.client else "unknown",
                                                  request.app.state.settings.session_pepper),
                    operation_id=str(uuid5(NAMESPACE_URL, f"kunlun/chargeback-command/{row.id}/{payload.idempotency_key}")),
                    reason=payload.reason, before_status="risk", after_status="resolved"))
            db.commit()
            return {"chargeback": project(row), "duplicate": duplicate, "recovered_microusd": recovered,
                    "written_off_microusd": written_off, "account_unfrozen": False}
        except PaymentDomainError as exc:
            db.rollback()
            raise HTTPException(exc.status_code, str(exc)) from exc
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(409, "拒付处置并发冲突，请查询原操作") from exc
