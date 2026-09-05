"""Commercial control plane; no public endpoint ever accepts a platform credential."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr
from sqlalchemy import select, func
from urllib.parse import urlsplit

from .auth import require_operator_scope
from .schemas import StrictModel
from .services.credentials import SecretUnavailable
from .services.ops_tokens import OperatorClaims
from .models import ModelRequest, PlatformDailyBudget
from .security import utcnow

router = APIRouter()


class ChannelOperation(StrictModel):
    operation_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    reason: str = Field(min_length=10, max_length=500)


class ChannelSecret(ChannelOperation):
    secret: SecretStr = Field(min_length=1, max_length=8192)


def channel_vault(request):
    if request.app.state.settings.gateway_mode != "managed_gateway":
        raise HTTPException(404, "Not Found")
    return request.app.state.platform_vault


@router.get("/ops/channels")
def channels(request: Request, _operator=Depends(require_operator_scope("channels:read"))):
    try:
        data = channel_vault(request).list()
    except SecretUnavailable:
        raise HTTPException(503, "平台密钥服务不可用") from None
    return JSONResponse({"channels": data, "catalog": channel_catalog(request, data)}, headers={"Cache-Control": "no-store"})


def channel_catalog(request, stored):
    indexed = {row["provider"]: row for row in stored}
    result = []
    for priority, provider in enumerate(request.app.state.settings.providers, 1):
        row = indexed.get(provider["name"], {})
        active, cleanup = bool(row.get("active")), bool(row.get("pending_cleanup"))
        result.append({"id": row.get("id"), "provider": provider["name"], "version": row.get("version", 0),
            "active": active, "pending_cleanup": cleanup,
            "status": "pending_cleanup" if cleanup else "enabled" if active else "disabled" if row else "unconfigured",
            "priority": priority, "models": provider["models"], "upstream_host": urlsplit(provider["base_url"]).hostname})
    return result


@router.get("/ops/channels/{provider}")
def channel_detail(provider: str, request: Request, _operator=Depends(require_operator_scope("channels:read"))):
    vault = channel_vault(request)
    if provider not in {item["name"] for item in request.app.state.settings.providers}:
        raise HTTPException(404, "渠道不在允许目录")
    try:
        row = next(item for item in channel_catalog(request, vault.list()) if item["provider"] == provider)
    except SecretUnavailable:
        raise HTTPException(503, "平台密钥服务不可用") from None
    return JSONResponse({"channel": row}, headers={"Cache-Control": "no-store"})


@router.get("/ops/channel-operations/{operation_id}")
def channel_operation(operation_id: str, request: Request, _operator=Depends(require_operator_scope("channels:read"))):
    if not 1 <= len(operation_id) <= 64:
        raise HTTPException(404, "Not Found")
    try:
        data = channel_vault(request).operation(operation_id)
    except SecretUnavailable:
        raise HTTPException(503, "操作记录暂不可用；结果仍未确认") from None
    if data is None:
        raise HTTPException(404, "未发现已提交记录；在途操作仍需核查")
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


@router.get("/ops/platform-budget")
def platform_budget(request: Request, _operator=Depends(require_operator_scope("metrics:read"))):
    channel_vault(request)
    settings = request.app.state.settings
    period = utcnow().date().isoformat()
    with request.app.state.SessionLocal() as db:
        budget = db.get(PlatformDailyBudget, period)
        limit = min(budget.limit_microusd, settings.platform_daily_budget_microusd) if budget else settings.platform_daily_budget_microusd
        spent = budget.spent_microusd if budget else 0
        reserved = budget.reserved_microusd if budget else 0
        pending = db.scalar(select(func.count()).select_from(ModelRequest).where(
            ModelRequest.billing_mode == "managed_gateway", ModelRequest.status == "pending_reconciliation"))
    return JSONResponse({"period": period, "timezone": "UTC", "currency": "microUSD",
        "limit": limit, "spent": spent, "reserved": reserved, "available": max(0, limit - spent - reserved),
        "pending_reconciliation_count": pending,
        "note": "Reserved includes uncertain cost; this is not a supplier invoice or profit report."},
        headers={"Cache-Control": "no-store"})


def write_channel(request, provider, payload, operator, secret):
    vault = channel_vault(request)
    if provider not in {p["name"] for p in request.app.state.settings.providers}:
        raise HTTPException(404, "渠道不在允许目录")
    try:
        result = vault.write(provider=provider, secret=secret, operation_id=payload.operation_id,
                             actor=f"{operator.subject}:{operator.token_id}", reason=payload.reason)
    except SecretUnavailable:
        raise HTTPException(503, "操作结果需按原 operation-id 核查；禁止自动重试") from None
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.put("/ops/channels/{provider}")
def put_channel(provider: str, payload: ChannelSecret, request: Request,
                operator: OperatorClaims = Depends(require_operator_scope("channels:write"))):
    return write_channel(request, provider, payload, operator, payload.secret.get_secret_value())


@router.post("/ops/channels/{provider}/revoke")
def revoke_channel(provider: str, payload: ChannelOperation, request: Request,
                   operator: OperatorClaims = Depends(require_operator_scope("channels:write"))):
    return write_channel(request, provider, payload, operator, None)
