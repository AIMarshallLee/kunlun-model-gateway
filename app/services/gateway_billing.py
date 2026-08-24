"""Atomic pre-authorization, settlement and release for model requests."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import ApiKey, Budget, ModelPrice, ModelRequest, ProviderAttempt, User, Wallet
from ..security import utcnow
from .budget import active_budget
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
    return math.ceil(tokens * price_per_million / 1_000_000)


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
        if user is None or user.status != "active":
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
        request_id = str(uuid.uuid4())
        request = ModelRequest(
            id=request_id,
            user_id=user_id,
            api_key_id=api_key_id,
            budget_id=budget.id if budget else None,
            idempotency_key=idempotency_key,
            requested_model=model,
            status="reserved",
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
        raise BillingError("并发请求冲突，请使用新的幂等键重试", 409) from exc
    return Reservation(
        request_id=request_id,
        amount=reserve_amount,
        estimated_input_tokens=estimated_input,
        input_price=price.input_microusd_per_million,
        output_price=price.output_microusd_per_million,
    )


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
) -> None:
    session.add(ProviderAttempt(
        id=str(uuid.uuid4()),
        request_id=request_id,
        ordinal=ordinal,
        provider=provider,
        model=model,
        status=status,
        status_code=status_code,
        failure_category=failure_category,
        completed_at=utcnow(),
    ))
    session.commit()


def _usage_from_response(response: dict[str, Any], fallback_input: int) -> tuple[int, int, bool]:
    usage = response.get("usage")
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
        if isinstance(prompt, int) and prompt >= 0 and isinstance(completion, int) and completion >= 0:
            return prompt, completion, False
    choices = response.get("choices")
    output_value: Any = choices if isinstance(choices, list) else ""
    return fallback_input, estimate_tokens(output_value), True


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
    allowed_statuses: tuple[str, ...] = ("reserved",),
) -> tuple[int, bool]:
    request = session.get(ModelRequest, request_id)
    if request is None or request.status not in allowed_statuses:
        raise BillingError("请求不处于可结算状态", 409)
    input_tokens, output_tokens, usage_estimated = _usage_from_response(response, request.input_tokens)
    usage_estimated = usage_estimated or force_usage_estimated
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
) -> None:
    request = session.get(ModelRequest, request_id)
    if request is None or request.status not in allowed_statuses:
        return
    reserved = request.reserved_microusd
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
    request.completed_at = utcnow()
    session.commit()


def mark_pending_reconciliation(
    session: Session,
    request_id: str,
    failure_category: str,
    *,
    provider: str | None = None,
    fallback_count: int | None = None,
) -> None:
    request = session.get(ModelRequest, request_id)
    if request is None or request.status != "reserved":
        return
    request.status = "pending_reconciliation"
    request.failure_category = failure_category
    if provider is not None:
        request.final_provider = provider
    if fallback_count is not None:
        request.fallback_count = fallback_count
    request.completed_at = utcnow()
    session.commit()
