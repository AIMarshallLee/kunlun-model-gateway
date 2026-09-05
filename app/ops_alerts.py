"""Current operational conditions and audited receipts, never financial resolution."""

from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .auth import require_operator_scope
from .models import ModelPrice, ModelRequest, OperatorAction, PaymentOrder, PaymentRefund, PlatformDailyBudget
from .schemas import StrictModel
from .security import as_utc, token_digest, utcnow
from .services.credentials import SecretUnavailable

router = APIRouter()


def collect_alerts(db, settings, vault, *, now=None):
    now = now or utcnow()
    items = []

    def add(kind, severity, count, evidence, destination):
        if not count:
            return
        # This hashes only aggregate operational metadata, never model input,
        # response, customer identity, or a credential. Receipt != incident ID.
        revision = hashlib.sha256(json.dumps([kind, severity, count, evidence], sort_keys=True).encode()).hexdigest()[:32]
        items.append(dict(id=kind, status="attention", severity=severity, count=count, evidence=evidence,
                          destination=destination, revision=revision, acknowledgement=None))

    def group(kind, severity, entity, condition, destination):
        count, oldest, newest, last_id = db.execute(select(func.count(entity.id), func.min(entity.created_at),
            func.max(entity.created_at), func.max(entity.id)).where(condition)).one()
        add(kind, severity, count, {"oldest_at": as_utc(oldest).isoformat() if oldest else None,
            "newest_at": as_utc(newest).isoformat() if newest else None, "sample_record_id": last_id}, destination)

    managed = ModelRequest.billing_mode == "managed_gateway"
    group("model_reconciliation", "warning", ModelRequest,
          managed & (ModelRequest.status == "pending_reconciliation"), "requests")
    group("stale_reservations", "critical", ModelRequest, managed & (ModelRequest.status == "reserved") &
          (ModelRequest.created_at <= now - timedelta(seconds=settings.model_reservation_lease_seconds)), "requests")
    group("payment_reconciliation", "warning", PaymentOrder,
          or_(PaymentOrder.status == "pending_reconciliation", (PaymentOrder.status == "checkout_requesting") &
              or_(PaymentOrder.checkout_claim_started_at.is_(None), PaymentOrder.checkout_claim_started_at <= now - timedelta(minutes=5))), "orders")
    group("refund_reconciliation", "warning", PaymentRefund,
          or_(PaymentRefund.status == "pending_reconciliation", PaymentRefund.status.in_(("requesting", "retrying")) &
              (PaymentRefund.claim_started_at <= now - timedelta(minutes=5))), "orders")
    group("payment_risk", "warning", PaymentOrder, PaymentOrder.risk_reason.is_not(None), "orders")
    group("refund_risk", "critical", PaymentRefund, PaymentRefund.status == "risk", "orders")
    period = now.date().isoformat()
    budget = db.get(PlatformDailyBudget, period)
    limit = min(budget.limit_microusd, settings.platform_daily_budget_microusd) if budget else settings.platform_daily_budget_microusd
    spent, reserved = (budget.spent_microusd, budget.reserved_microusd) if budget else (0, 0)
    if (spent + reserved) * 100 >= limit * 80:
        add("platform_budget", "critical" if spent + reserved >= limit else "warning", 1,
            {"period": period, "limit_microusd": limit, "spent_microusd": spent, "reserved_microusd": reserved}, "budget")

    # No credential resolution, health-check inference or billable probe.
    try:
        channels = {row["provider"] for row in vault.list() if row["active"]}
    except SecretUnavailable:
        channels = None
        add("supply_observation_failed", "critical", 1, {"state": "unknown"}, "channels")
    missing, below_floor = [], []
    for row in db.scalars(select(ModelPrice).where(ModelPrice.active.is_(True), ModelPrice.model.in_(settings.models))):
        routes = [route for route in settings.providers if row.model in route["models"]]
        if channels is not None and not any(route["name"] in channels for route in routes):
            missing.append(row.model)
        if routes and any(getattr(row, field) < max(route["pricing"][row.model][field] for route in routes)
                          for field in ("input_microusd_per_million", "output_microusd_per_million")):
            below_floor.append(row.model)
    add("supply_unavailable", "critical", len(missing), {"models": sorted(missing)}, "channels")
    add("price_below_supply", "critical", len(below_floor), {"models": sorted(below_floor)}, "models")

    # Each rule is bounded and stable; read only its latest matching receipt.
    for item in items:
        receipt = db.scalar(select(OperatorAction).where(OperatorAction.target_type == "ops_alert",
            OperatorAction.target_id == item["id"], OperatorAction.before_status == item["revision"],
            OperatorAction.action == "alert_ack").order_by(OperatorAction.created_at.desc(), OperatorAction.id.desc()).limit(1))
        if receipt:
            item["acknowledgement"] = {"actor": receipt.actor, "at": as_utc(receipt.created_at).isoformat(), "operation_id": receipt.operation_id}
    items.sort(key=lambda item: (item["severity"] != "critical", item["id"]))
    return {"observed_at": now.isoformat(), "items": items,
            "coverage": "Configured operational rules only; not readiness, supplier health, or notification-delivery proof."}


def snapshot(request, db):
    settings = request.app.state.settings
    if settings.gateway_mode != "managed_gateway":
        raise HTTPException(404, "Not Found")
    try:
        return collect_alerts(db, settings, request.app.state.platform_vault)
    except SQLAlchemyError:
        raise HTTPException(503, "Alert observation unavailable; operational state is unknown") from None


@router.get("/ops/alerts")
def alerts(request: Request, _operator=Depends(require_operator_scope("alerts:read"))):
    with request.app.state.SessionLocal() as db:
        return snapshot(request, db)


@router.get("/ops/alerts/{kind}")
def alert_detail(kind: str, request: Request, _operator=Depends(require_operator_scope("alerts:read"))):
    with request.app.state.SessionLocal() as db:
        data = snapshot(request, db)
        item = next((row for row in data["items"] if row["id"] == kind), None)
        if item is None:
            raise HTTPException(404, "No active observation for this rule; refresh the alert list")
        return {"alert": item, "observed_at": data["observed_at"]}


class AlertReceipt(StrictModel):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{32}$")
    operation_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    reason: str = Field(min_length=12, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value):
        return value.strip() if isinstance(value, str) else value


@router.post("/ops/alerts/{kind}/ack", status_code=201)
def acknowledge(kind: str, payload: AlertReceipt, request: Request, operator=Depends(require_operator_scope("alerts:write"))):
    with request.app.state.SessionLocal() as db:
        item = next((row for row in snapshot(request, db)["items"] if row["id"] == kind), None)
        if item is None or item["revision"] != payload.expected_revision:
            raise HTTPException(409, "Observation changed or no longer active; refresh before acknowledging")
        if db.scalar(select(OperatorAction.id).where(OperatorAction.operation_id == payload.operation_id)):
            raise HTTPException(409, "Operation already recorded; inspect the audit")
        receipt = OperatorAction(id=str(uuid4()), target_type="ops_alert", target_id=kind,
            action="alert_ack", reason=payload.reason, actor=operator.subject, scopes=" ".join(sorted(operator.scopes)),
            token_id=operator.token_id, operation_id=payload.operation_id,
            source_ip_digest=token_digest(request.client.host if request.client else "unknown", request.app.state.settings.session_pepper),
            before_status=item["revision"], after_status="acknowledged")
        db.add(receipt)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Receipt already recorded; inspect the audit") from None
        return {"id": kind, "revision": item["revision"], "operation_id": payload.operation_id,
                "status": "attention", "acknowledged_at": receipt.created_at.isoformat(),
                "note": "Receipt only; no financial, supply or incident state was changed."}
