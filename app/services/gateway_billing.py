"""Atomic pre-authorization, settlement and release for model requests."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import ApiKey, Budget, ModelPrice, ModelRequest, ProviderAttempt, User, Wallet
from ..security import utcnow
from .budget import active_budget
from .platform_budget import reserve_platform_cost, finalize_platform_cost
from .ledger import (
    CUSTOMER_AVAILABLE,
    CUSTOMER_RESERVED,
    PLATFORM_REVENUE,
    PLATFORM_RISK,
    PLATFORM_PROVIDER_EXPENSE,
    PLATFORM_PROVIDER_PAYABLE,
    post_transaction,
)


class BillingError(RuntimeError):
    def __init__(self, message: str, status_code: int = 402) -> None:
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Reservation:
    request_id: str
    amount: int
    estimated_input_tokens: int
    input_price: int
    output_price: int


def estimate_tokens(value: Any) -> int:
    # Deliberately ephemeral: the serialized content is not stored or logged.
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return max(1, math.ceil(len(serialized) / 4))


def input_token_reservation_upper_bound(value: Any) -> int:
    """Return a conservative preauthorization bound for billable input.

    OpenAI-compatible tokenizers are byte-backed, so the normalized UTF-8
    request length bounds content tokens. The fixed and per-item margins cover
    provider chat templates and framing that are not present in JSON. Exact
    supplier tokenizer reconciliation still replaces this bound after usage is
    returned; the bound itself is never persisted as observed usage.
    """
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    protocol_items = 0
    if isinstance(value, dict):
        for field_name in ("messages", "tools"):
            field_value = value.get(field_name)
            if isinstance(field_value, list):
                protocol_items += len(field_value)
    return max(1, len(serialized.encode("utf-8")) + 4096 + 32 * protocol_items)


def token_cost(tokens: int, price_per_million: int) -> int:
    if tokens <= 0 or price_per_million <= 0:
        return 0
    return (tokens * price_per_million + 999_999) // 1_000_000


def get_price(session: Session, model: str) -> ModelPrice | None:
    return session.scalar(select(ModelPrice).where(
        ModelPrice.model == model,
        ModelPrice.active.is_(True),
    ).order_by(ModelPrice.version.desc()))


def reserve_model_request(
    session: Session,
    *,
    user_id: str,
    api_key_id: str,
    model: str,
    billable_payload: Any,
    max_output_tokens: int,
    idempotency_key: str | None,
    managed_cost_prices: tuple[int, int] | None = None,
    platform_daily_limit: int = 0,
) -> Reservation:
    price = get_price(session, model)
    if price is None:
        raise BillingError("模型不存在或未定价", 404)
    if max_output_tokens > price.max_output_tokens:
        raise BillingError("请求输出上限超过模型策略", 422)
    estimated_input = estimate_tokens(billable_payload)
    reserved_input = input_token_reservation_upper_bound(billable_payload)
    reserve_amount = max(1, token_cost(reserved_input, price.input_microusd_per_million) + token_cost(
        max_output_tokens, price.output_microusd_per_million,
    ))
    if idempotency_key:
        existing = session.scalar(select(ModelRequest.id).where(
            ModelRequest.user_id == user_id,
            ModelRequest.idempotency_key == idempotency_key,
        ))
        if existing is not None:
            raise BillingError("幂等键已用于其他请求", 409)
    budget = None
    try:
        # Account freeze and request admission share the User row as their
        # first lock. A request therefore linearizes either before the freeze
        # (and owns a durable reservation) or after it (and is rejected).
        user = session.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if user is None or user.status != "active" or (managed_cost_prices is not None and user.email_verified_at is None):
            session.rollback()
            raise BillingError("账户不可用", 403)
        key = session.scalar(
            select(ApiKey)
            .where(ApiKey.id == api_key_id, ApiKey.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if key is None or key.status != "active":
            session.rollback()
            raise BillingError("API Key 已失效", 401)
        # This row is the per-customer serialization point shared with budget
        # replacement. It prevents a request from slipping through while an
        # active budget is being superseded or created.
        lock_result = session.execute(update(Wallet).where(
            Wallet.user_id == user_id,
        ).values(balance_microusd=Wallet.balance_microusd))
        if lock_result.rowcount != 1:
            session.rollback()
            raise BillingError("钱包不存在", 402)
        budget = active_budget(session, user_id)
        wallet_result = session.execute(update(Wallet).where(
            Wallet.user_id == user_id,
            Wallet.balance_microusd >= reserve_amount,
        ).values(
            balance_microusd=Wallet.balance_microusd - reserve_amount,
            reserved_microusd=Wallet.reserved_microusd + reserve_amount,
            updated_at=utcnow(),
        ))
        if wallet_result.rowcount != 1:
            session.rollback()
            raise BillingError("余额不足", 402)
        if budget is not None:
            available = budget.limit_microusd - budget.spent_microusd - budget.reserved_microusd
            if available < reserve_amount:
                session.rollback()
                raise BillingError("预算不足", 402)
            now = utcnow()
            budget_result = session.execute(update(Budget).where(
                Budget.id == budget.id,
                Budget.user_id == user_id,
                Budget.status == "active",
                Budget.period_start <= now,
                Budget.period_end > now,
                Budget.limit_microusd - Budget.spent_microusd - Budget.reserved_microusd >= reserve_amount,
            ).values(
                reserved_microusd=Budget.reserved_microusd + reserve_amount,
            ).execution_options(synchronize_session=False))
            if budget_result.rowcount != 1:
                session.rollback()
                raise BillingError("预算已更新或并发占用，请重试", 409)
        platform_period = None
        platform_reserve = 0
        if managed_cost_prices is not None:
            platform_reserve = max(1, token_cost(reserved_input, managed_cost_prices[0]) + token_cost(max_output_tokens, managed_cost_prices[1]))
            try:
                platform_period = reserve_platform_cost(session, platform_daily_limit, platform_reserve)
            except ValueError as exc:
                session.rollback()
                raise BillingError(str(exc), 503) from exc
        request_id = str(uuid.uuid4())
        request = ModelRequest(
            id=request_id,
            user_id=user_id,
            api_key_id=api_key_id,
            budget_id=budget.id if budget else None,
            idempotency_key=idempotency_key,
            requested_model=model,
            status="reserved",
            billing_mode="managed_gateway" if managed_cost_prices is not None else "prepaid",
            platform_budget_period=platform_period,
            platform_reserved_microusd=platform_reserve,
            price_version=price.version,
            input_price=price.input_microusd_per_million,
            output_price=price.output_microusd_per_million,
            reserved_microusd=reserve_amount,
            input_tokens=estimated_input,
            usage_estimated=True,
        )
        session.add(request)
        post_transaction(
            session,
            user_id=user_id,
            kind="reserve",
            reference=request_id,
            idempotency_key=f"model:{request_id}:reserve",
            entries=[
                (CUSTOMER_AVAILABLE, -reserve_amount),
                (CUSTOMER_RESERVED, reserve_amount),
            ],
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise BillingError("并发请求冲突，请保留原幂等键并查询请求状态", 409) from exc
    return Reservation(
        request_id=request_id,
        amount=reserve_amount,
        estimated_input_tokens=estimated_input,
        input_price=price.input_microusd_per_million,
        output_price=price.output_microusd_per_million,
    )


def reserve_byok_model_request(
    session: Session,
    *,
    user_id: str,
    api_key_id: str,
    model: str,
    billable_payload: Any,
    max_output_tokens: int,
    idempotency_key: str | None,
    minimum_input_price: int | None = None,
    minimum_output_price: int | None = None,
) -> Reservation:
    """Reserve only a tenant's provider spend cap; never touch a wallet."""
    price = get_price(session, model)
    if price is None:
        raise BillingError("模型不存在或未定价", 404)
    # A rolling deployment can leave an older process with a provider catalog
    # whose prices are higher than the active DB catalog written by a newer
    # process. Do not reserve against that lower price and then send upstream.
    # The caller supplies the maximum price across every eligible BYOK route.
    if (
        (minimum_input_price is not None and price.input_microusd_per_million < minimum_input_price)
        or (minimum_output_price is not None and price.output_microusd_per_million < minimum_output_price)
    ):
        raise BillingError("活动模型价格低于可用 BYOK Provider 的预授权下限", 503)
    if max_output_tokens > price.max_output_tokens:
        raise BillingError("请求输出上限超过模型策略", 422)
    if idempotency_key and session.scalar(select(ModelRequest.id).where(
        ModelRequest.user_id == user_id,
        ModelRequest.idempotency_key == idempotency_key,
    )) is not None:
        raise BillingError("幂等键已用于其他请求", 409)
    estimated_input = estimate_tokens(billable_payload)
    reserve_amount = max(1, token_cost(
        input_token_reservation_upper_bound(billable_payload), price.input_microusd_per_million,
    ) + token_cost(max_output_tokens, price.output_microusd_per_million))
    try:
        user = session.scalar(select(User).where(User.id == user_id).with_for_update())
        key = session.scalar(select(ApiKey).where(
            ApiKey.id == api_key_id, ApiKey.user_id == user_id,
        ).with_for_update())
        if user is None or user.status != "active" or key is None or key.status != "active":
            session.rollback()
            raise BillingError("账户或 API Key 不可用", 403)
        budget = active_budget(session, user_id, kind="provider_spend_cap")
        if budget is None:
            session.rollback()
            raise BillingError("BYOK 请求必须配置有效的供应商支出上限", 402)
        now = utcnow()
        result = session.execute(update(Budget).where(
            Budget.id == budget.id,
            Budget.kind == "provider_spend_cap",
            Budget.status == "active",
            Budget.period_start <= now,
            Budget.period_end > now,
            Budget.limit_microusd - Budget.spent_microusd - Budget.reserved_microusd >= reserve_amount,
        ).values(
            reserved_microusd=Budget.reserved_microusd + reserve_amount,
        ).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            session.rollback()
            raise BillingError("供应商支出上限不足", 402)
        request_id = str(uuid.uuid4())
        session.add(ModelRequest(
            id=request_id, user_id=user_id, api_key_id=api_key_id, budget_id=budget.id,
            idempotency_key=idempotency_key, requested_model=model, status="reserved",
            billing_mode="byok", price_version=price.version,
            input_price=price.input_microusd_per_million,
            output_price=price.output_microusd_per_million, reserved_microusd=reserve_amount,
            input_tokens=estimated_input, usage_estimated=True,
        ))
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise BillingError("并发请求冲突，请保留原幂等键并查询请求状态", 409) from exc
    return Reservation(request_id, reserve_amount, estimated_input, price.input_microusd_per_million, price.output_microusd_per_million)


def record_attempt(
    session: Session,
    *,
    request_id: str,
    ordinal: int,
    provider: str,
    model: str,
    status: str,
    status_code: int | None = None,
    failure_category: str | None = None,
    credential_connection_id: str | None = None,
    credential_version: int | None = None,
    pricing_snapshot: dict[str, Any] | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    billing_status: str = "unsettled",
    is_final: bool = False,
) -> str:
    started = started_at or utcnow()
    completed = completed_at or utcnow()
    attempt_id = str(uuid.uuid4())
    session.add(ProviderAttempt(
        id=attempt_id,
        request_id=request_id,
        ordinal=ordinal,
        provider=provider,
        model=model,
        status=status,
        status_code=status_code,
        failure_category=failure_category,
        credential_connection_id=credential_connection_id,
        credential_version=credential_version,
        pricing_snapshot_json=json.dumps(pricing_snapshot or {}, sort_keys=True, separators=(",", ":")),
        billing_status=billing_status,
        is_final=is_final,
        started_at=started,
        completed_at=completed,
        duration_ms=max(0, int((completed - started).total_seconds() * 1000)),
    ))
    session.commit()
    return attempt_id


def _finalize_attempt(
    session: Session,
    *,
    request: ModelRequest,
    attempt_id: str | None,
    status: str,
    billing_status: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    upstream_cost: int = 0,
    usage_estimated: bool = False,
) -> None:
    if attempt_id is None:
        return
    attempt = session.get(ProviderAttempt, attempt_id)
    if attempt is None or attempt.request_id != request.id:
        raise BillingError("尝试记录不存在或不属于本请求", 409)
    completed = utcnow()
    attempt.status = status
    attempt.billing_status = billing_status
    attempt.is_final = True
    attempt.input_tokens = input_tokens
    attempt.output_tokens = output_tokens
    attempt.upstream_cost_microusd = upstream_cost
    attempt.usage_estimated = usage_estimated
    attempt.completed_at = completed
    started = attempt.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=completed.tzinfo)
    attempt.duration_ms = max(0, int((completed - started).total_seconds() * 1000))
    request.final_attempt_id = attempt.id


def _usage_from_response(response: dict[str, Any], fallback_input: int) -> tuple[int, int, bool]:
    usage = response.get("usage")
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
        if (
            isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0
            and isinstance(completion, int) and not isinstance(completion, bool) and completion >= 0
        ):
            return prompt, completion, False
    choices = response.get("choices")
    output_value: Any = choices if isinstance(choices, list) else ""
    return fallback_input, estimate_tokens(output_value), True


def _locked_model_request(session: Session, request_id: str) -> ModelRequest | None:
    """Refresh and lock a reservation before any state transition.

    ``populate_existing`` matters when an ASGI worker reused a session or
    preloaded a request for auditing: without it, SQLAlchemy can evaluate the
    old status before acquiring the row lock.
    """
    with session.no_autoflush:
        existing = session.get(ModelRequest, request_id)
        if existing is not None and existing.billing_mode == "managed_gateway":
            # Admission also locks User first. Without this, a settlement
            # holding the global row can deadlock against an admission holding
            # the same customer's wallet (including FK key-share locks).
            session.get(User, existing.user_id, populate_existing=True, with_for_update=True)
        return session.get(
            ModelRequest,
            request_id,
            populate_existing=True,
            with_for_update=True,
        )


def settle_model_request(
    session: Session,
    *,
    request_id: str,
    response: dict[str, Any],
    provider: str,
    fallback_count: int,
    force_usage_estimated: bool = False,
    upstream_input_price: int | None = None,
    upstream_output_price: int | None = None,
    upstream_cost_override: int | None = None,
    attempt_id: str | None = None,
    allowed_statuses: tuple[str, ...] = ("reserved",),
    allow_budget_overrun: bool = False,
) -> tuple[int, bool]:
    request = _locked_model_request(session, request_id)
    if request is None or request.status not in allowed_statuses:
        raise BillingError("请求不处于可结算状态", 409)
    input_tokens, output_tokens, usage_estimated = _usage_from_response(response, request.input_tokens)
    usage_estimated = usage_estimated or force_usage_estimated
    if request.billing_mode in {"byok", "managed_gateway"} and usage_estimated:
        # A provider charge cannot be inferred from output characters.
        # Keep its durable hold until an operator reconciles the
        # provider's authoritative usage and cost.
        session.rollback()
        raise BillingError("上游响应缺少完整有效 usage，已转人工对账", 409)
    customer_cost = token_cost(input_tokens, request.input_price) + token_cost(output_tokens, request.output_price)
    upstream_cost = (
        int(upstream_cost_override)
        if upstream_cost_override is not None
        else token_cost(
            input_tokens,
            request.input_price if upstream_input_price is None else upstream_input_price,
        ) + token_cost(
            output_tokens,
            request.output_price if upstream_output_price is None else upstream_output_price,
        )
    )
    if upstream_cost < 0:
        raise BillingError("上游成本不能为负数", 422)
    if request.billing_mode == "managed_gateway":
        if upstream_cost > request.platform_reserved_microusd and not (allow_budget_overrun and request.status == "pending_reconciliation"):
            session.rollback()
            raise BillingError("平台上游成本超过预授权，需人工对账", 409)
    if request.billing_mode == "byok":
        if not request.budget_id:
            raise BillingError("BYOK 请求缺少供应商预算", 409)
        if allow_budget_overrun and request.status != "pending_reconciliation":
            raise BillingError("仅人工对账可突破 BYOK 预算预授权", 409)
        if not allow_budget_overrun and upstream_cost > request.reserved_microusd:
            session.rollback()
            raise BillingError("上游实际成本超过 BYOK 预授权上限", 409)
        budget_filters = [
            Budget.id == request.budget_id,
            Budget.kind == "provider_spend_cap",
            Budget.reserved_microusd >= request.reserved_microusd,
        ]
        if not allow_budget_overrun:
            budget_filters.append(
                Budget.spent_microusd + upstream_cost <= Budget.limit_microusd
            )
        result = session.execute(update(Budget).where(
            *budget_filters,
        ).values(
            reserved_microusd=Budget.reserved_microusd - request.reserved_microusd,
            spent_microusd=Budget.spent_microusd + upstream_cost,
        ))
        if result.rowcount != 1:
            session.rollback()
            raise BillingError("供应商预算预授权状态异常", 409)
        request.status = "settled"
        request.final_model = str(response.get("model") or request.requested_model)
        request.final_provider = provider
        request.charged_microusd = 0
        request.upstream_cost_microusd = upstream_cost
        request.input_tokens = input_tokens
        request.output_tokens = output_tokens
        request.usage_estimated = usage_estimated
        request.fallback_count = fallback_count
        request.cost_state = "settled"
        _finalize_attempt(
            session, request=request, attempt_id=attempt_id, status="succeeded", billing_status="settled",
            input_tokens=input_tokens, output_tokens=output_tokens, upstream_cost=upstream_cost,
            usage_estimated=usage_estimated,
        )
        request.completed_at = utcnow()
        session.commit()
        return 0, usage_estimated
    reserved = request.reserved_microusd
    charged = min(customer_cost, reserved)
    release = reserved - charged
    entries: list[tuple[str, int]] = [(CUSTOMER_RESERVED, -reserved)]
    if release:
        entries.append((CUSTOMER_AVAILABLE, release))
    if customer_cost:
        entries.append((PLATFORM_REVENUE, customer_cost))
    if customer_cost > charged:
        entries.append((PLATFORM_RISK, -(customer_cost - charged)))
    if upstream_cost:
        entries.extend([
            (PLATFORM_PROVIDER_EXPENSE, upstream_cost),
            (PLATFORM_PROVIDER_PAYABLE, -upstream_cost),
        ])
    wallet_result = session.execute(update(Wallet).where(
        Wallet.user_id == request.user_id,
        Wallet.reserved_microusd >= reserved,
    ).values(
        reserved_microusd=Wallet.reserved_microusd - reserved,
        balance_microusd=Wallet.balance_microusd + release,
        updated_at=utcnow(),
    ))
    if wallet_result.rowcount != 1:
        session.rollback()
        raise BillingError("钱包预授权状态异常", 409)
    if request.budget_id:
        budget_result = session.execute(update(Budget).where(
            Budget.id == request.budget_id,
            Budget.reserved_microusd >= reserved,
        ).values(
            reserved_microusd=Budget.reserved_microusd - reserved,
            spent_microusd=Budget.spent_microusd + charged,
        ))
        if budget_result.rowcount != 1:
            session.rollback()
            raise BillingError("预算预授权状态异常", 409)
    try:
        finalize_platform_cost(session, request, upstream_cost)
    except ValueError as exc:
        session.rollback()
        raise BillingError(str(exc), 409) from exc
    post_transaction(
        session,
        user_id=request.user_id,
        kind="settle",
        reference=request.id,
        idempotency_key=f"model:{request.id}:settle",
        entries=entries,
    )
    request.status = "settled" if customer_cost <= reserved else "under_reserved"
    request.final_model = str(response.get("model") or request.requested_model)
    request.final_provider = provider
    request.charged_microusd = charged
    request.upstream_cost_microusd = upstream_cost
    request.input_tokens = input_tokens
    request.output_tokens = output_tokens
    request.usage_estimated = usage_estimated
    request.fallback_count = fallback_count
    request.cost_state = "settled" if customer_cost <= reserved else "under_reserved"
    _finalize_attempt(
        session, request=request, attempt_id=attempt_id, status="succeeded", billing_status="settled",
        input_tokens=input_tokens, output_tokens=output_tokens, upstream_cost=upstream_cost,
        usage_estimated=usage_estimated,
    )
    request.completed_at = utcnow()
    session.commit()
    return charged, usage_estimated


def release_model_request(
    session: Session,
    request_id: str,
    failure_category: str,
    *,
    allowed_statuses: tuple[str, ...] = ("reserved",),
    final_status: str = "failed",
    ledger_kind: str = "release",
    attempt_id: str | None = None,
) -> None:
    request = _locked_model_request(session, request_id)
    if request is None or request.status not in allowed_statuses:
        return
    reserved = request.reserved_microusd
    if request.billing_mode == "byok":
        if request.budget_id:
            result = session.execute(update(Budget).where(
                Budget.id == request.budget_id,
                Budget.kind == "provider_spend_cap",
                Budget.reserved_microusd >= reserved,
            ).values(reserved_microusd=Budget.reserved_microusd - reserved))
            if result.rowcount != 1:
                session.rollback()
                raise BillingError("供应商预算预授权状态异常", 409)
        request.status = final_status
        request.failure_category = failure_category
        request.cost_state = "released"
        _finalize_attempt(
            session, request=request, attempt_id=attempt_id, status="failed", billing_status="not_billed",
        )
        request.completed_at = utcnow()
        session.commit()
        return
    wallet_result = session.execute(update(Wallet).where(
        Wallet.user_id == request.user_id,
        Wallet.reserved_microusd >= reserved,
    ).values(
        reserved_microusd=Wallet.reserved_microusd - reserved,
        balance_microusd=Wallet.balance_microusd + reserved,
        updated_at=utcnow(),
    ))
    if wallet_result.rowcount != 1:
        session.rollback()
        raise BillingError("钱包预授权状态异常", 409)
    if request.budget_id:
        budget_result = session.execute(update(Budget).where(
            Budget.id == request.budget_id,
            Budget.reserved_microusd >= reserved,
        ).values(reserved_microusd=Budget.reserved_microusd - reserved))
        if budget_result.rowcount != 1:
            session.rollback()
            raise BillingError("预算预授权状态异常", 409)
    try:
        finalize_platform_cost(session, request, 0)
    except ValueError as exc:
        session.rollback()
        raise BillingError(str(exc), 409) from exc
    post_transaction(
        session,
        user_id=request.user_id,
        kind=ledger_kind,
        reference=request.id,
        idempotency_key=f"model:{request.id}:release",
        entries=[
            (CUSTOMER_RESERVED, -reserved),
            (CUSTOMER_AVAILABLE, reserved),
        ],
    )
    request.status = final_status
    request.failure_category = failure_category
    request.cost_state = "released"
    _finalize_attempt(
        session, request=request, attempt_id=attempt_id, status="failed", billing_status="not_billed",
    )
    request.completed_at = utcnow()
    session.commit()


def mark_pending_reconciliation(
    session: Session,
    request_id: str,
    failure_category: str,
    *,
    provider: str | None = None,
    fallback_count: int | None = None,
    attempt_id: str | None = None,
) -> None:
    request = _locked_model_request(session, request_id)
    if request is None or request.status != "reserved":
        return
    request.status = "pending_reconciliation"
    request.failure_category = failure_category
    if provider is not None:
        request.final_provider = provider
    if fallback_count is not None:
        request.fallback_count = fallback_count
    request.cost_state = "pending_reconciliation"
    _finalize_attempt(
        session, request=request, attempt_id=attempt_id, status="uncertain", billing_status="unknown",
    )
    request.completed_at = utcnow()
    session.commit()
