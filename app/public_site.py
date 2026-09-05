"""Public commercial catalog: selling prices only, never supply credentials or costs."""

from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select

from .models import ModelPrice
from .security import utcnow
from .services.purchase_supply import has_configured_supply

router = APIRouter()
STATIC = Path(__file__).resolve().parent / "static"


def require_managed(request):
    if request.app.state.settings.gateway_mode != "managed_gateway":
        raise HTTPException(404, "Not Found")


def public_https_url(value):
    try:
        url = urlparse(value)
        if url.scheme == "https" and url.hostname and not url.username and not url.password:
            return value
    except ValueError:
        pass
    return None


@router.get("/api-guide", include_in_schema=False)
def api_guide(request: Request):
    require_managed(request)
    return FileResponse(STATIC / "api-guide.html", media_type="text/html")


@router.get("/service-info", include_in_schema=False)
def service_info(request: Request):
    require_managed(request)
    return FileResponse(STATIC / "service-info.html", media_type="text/html")


@router.get("/public/catalog")
def public_catalog(request: Request):
    require_managed(request)
    settings = request.app.state.settings
    with request.app.state.SessionLocal() as db:
        rows = db.scalars(select(ModelPrice).where(
            ModelPrice.active.is_(True), ModelPrice.model.in_(settings.models),
        ).order_by(ModelPrice.model, ModelPrice.version.desc()).limit(256)).all()
        models, seen = [], set()
        for row in rows:
            if row.model in seen:
                continue
            seen.add(row.model)
            models.append({"id": row.model, "price_version": row.version,
                "input_microusd_per_million": row.input_microusd_per_million,
                "output_microusd_per_million": row.output_microusd_per_million,
                "max_output_tokens": row.max_output_tokens,
                "effective_at": row.effective_at.isoformat()})
    return JSONResponse({"environment": settings.environment, "currency": "USD", "fetched_at": utcnow().isoformat(),
        "registration_enabled": settings.public_signup,
        "purchasing_enabled": bool(settings.is_production and settings.live_payments and settings.topup_packages
                                    and request.app.state.live_payment_bridge is not None and settings.payment_provider
                                    and has_configured_supply(settings, request.app.state.platform_vault, request.app.state.SessionLocal)),
        "models": models, "rate_limit_per_minute": settings.rate_limit_per_minute,
        "terms_url": public_https_url(settings.terms_url), "privacy_url": public_https_url(settings.privacy_url),
        "support_email": settings.complaint_email or None}, headers={"Cache-Control": "no-store"})
