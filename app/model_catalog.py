"""Versioned retail catalog. Supplier prices stay in the controlled adapter catalog."""

from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .auth import require_operator_scope
from .models import ModelPrice, OperatorAction
from .ops_console import project
from .schemas import StrictModel
from .security import token_digest

router = APIRouter()
FIELDS = ("id", "model", "version", "active", "input_microusd_per_million", "output_microusd_per_million", "max_output_tokens", "effective_at")


def require_managed(request):
    if request.app.state.settings.gateway_mode != "managed_gateway":
        raise HTTPException(404, "Retail catalog management requires managed gateway mode")


def latest(db, model):
    return db.scalar(select(ModelPrice).where(ModelPrice.model == model).order_by(ModelPrice.version.desc())
                     .execution_options(populate_existing=True))


def anchor_for(db, anchor_id):
    row = db.get(ModelPrice, anchor_id)
    if row is None:
        raise HTTPException(404, "Model catalog entry not found")
    anchor = db.scalar(select(ModelPrice).where(ModelPrice.model == row.model).order_by(ModelPrice.version).limit(1))
    if anchor.id != anchor_id:
        raise HTTPException(404, "Use the stable catalog entry ID, not a historical price ID")
    return anchor


@router.get("/ops/models")
def list_models(request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=1_000_000),
                _operator=Depends(require_operator_scope("models:read"))):
    require_managed(request)
    with request.app.state.SessionLocal() as db:
        models = db.scalars(select(ModelPrice.model).distinct().order_by(ModelPrice.model).offset(offset).limit(limit)).all()
        total = db.scalar(select(func.count(func.distinct(ModelPrice.model))))
        items = []
        for model in models:
            row = latest(db, model)
            anchor_id = db.scalar(select(ModelPrice.id).where(ModelPrice.model == model).order_by(ModelPrice.version).limit(1))
            items.append({**project(row, FIELDS), "id": anchor_id, "price_id": row.id})
        return {"items": items, "pagination": {"limit": limit, "offset": offset, "total": total}}


@router.get("/ops/models/{anchor_id}")
def model_detail(anchor_id: str, request: Request, _operator=Depends(require_operator_scope("models:read"))):
    require_managed(request)
    with request.app.state.SessionLocal() as db:
        anchor = anchor_for(db, anchor_id)
        versions = db.scalars(select(ModelPrice).where(ModelPrice.model == anchor.model).order_by(ModelPrice.version.desc()).limit(101)).all()
        return {"model": {**project(versions[0], FIELDS), "id": anchor_id, "price_id": versions[0].id},
                "history": [project(row, FIELDS) for row in versions[:100]], "history_truncated": len(versions) > 100}


class PriceChange(StrictModel):
    action: Literal["publish", "unpublish"]
    expected_version: int = Field(strict=True, ge=1, le=2_147_483_646)
    operation_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    reason: str = Field(min_length=12, max_length=500)
    input_microusd_per_million: int | None = Field(None, strict=True, ge=1, le=10_000_000_000)
    output_microusd_per_million: int | None = Field(None, strict=True, ge=1, le=10_000_000_000)
    max_output_tokens: int | None = Field(None, strict=True, ge=1, le=1_000_000)

    @field_validator("reason", mode="before")
    @classmethod
    def trim_reason(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def price_fields(self):
        fields = (self.input_microusd_per_million, self.output_microusd_per_million, self.max_output_tokens)
        if self.action == "publish" and any(value is None for value in fields):
            raise ValueError("Publishing requires both retail prices and the output limit")
        if self.action == "unpublish" and any(value is not None for value in fields):
            raise ValueError("Unpublishing must not change prices")
        return self


@router.post("/ops/models/{anchor_id}/price", status_code=201)
def change_price(anchor_id: str, payload: PriceChange, request: Request, operator=Depends(require_operator_scope("models:write"))):
    require_managed(request)
    settings = request.app.state.settings
    with request.app.state.SessionLocal() as db:
        anchor = anchor_for(db, anchor_id)
        # Only the anchor is locked; subsequent versions can never become a
        # different serialization point. SQLite uses a write to emulate it.
        db.get(ModelPrice, anchor_id, with_for_update=True, populate_existing=True)
        if db.get_bind().dialect.name == "sqlite":
            db.execute(update(ModelPrice).where(ModelPrice.id == anchor_id).values(active=ModelPrice.active))
        if db.scalar(select(OperatorAction.id).where(OperatorAction.operation_id == payload.operation_id)) is not None:
            raise HTTPException(409, "Operation already recorded; inspect its audit before another command")
        current = latest(db, anchor.model)
        if current.version != payload.expected_version:
            raise HTTPException(409, "Price version changed; refresh before preparing another command")
        values = {field: getattr(current, field) for field in ("input_microusd_per_million", "output_microusd_per_million", "max_output_tokens")}
        if payload.action == "publish":
            if anchor.model not in settings.models:
                raise HTTPException(422, "Model is not in this deployment's supported catalog")
            routes = [item for item in settings.providers if anchor.model in item.get("models", [])]
            if not routes:
                raise HTTPException(422, "No configured supplier adapter for this model")
            for field in ("input_microusd_per_million", "output_microusd_per_million"):
                if getattr(payload, field) < max(item["pricing"][anchor.model][field] for item in routes):
                    raise HTTPException(422, "Retail price is below a configured supplier price; no loss-leader override in this release")
            if payload.max_output_tokens > settings.max_output_tokens:
                raise HTTPException(422, "Output limit exceeds platform policy")
            values = {field: getattr(payload, field) for field in values}
        elif not current.active:
            raise HTTPException(409, "Model is already unpublished")
        before = f"{'listed' if current.active else 'unlisted'}:v{current.version}"
        db.execute(update(ModelPrice).where(ModelPrice.model == anchor.model, ModelPrice.active.is_(True)).values(active=False))
        new = ModelPrice(id=str(uuid4()), model=anchor.model, version=current.version + 1,
                         active=payload.action == "publish", **values)
        db.add(new)
        db.add(OperatorAction(id=str(uuid4()), target_type="model_catalog", target_id=anchor_id,
            action="model_" + payload.action, reason=payload.reason, actor=operator.subject,
            scopes=" ".join(sorted(operator.scopes)), token_id=operator.token_id, operation_id=payload.operation_id,
            source_ip_digest=token_digest(request.client.host if request.client else "unknown", settings.session_pepper),
            before_status=before, after_status=f"{'listed' if new.active else 'unlisted'}:v{new.version}"))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Concurrent catalog command; inspect current catalog and audit") from None
        return {"model": {**project(new, FIELDS), "id": anchor_id, "price_id": new.id}, "operation_id": payload.operation_id}
