"""Atomic UTC-day supply cost holds, independent from customer selling prices."""

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..models import PlatformDailyBudget
from ..security import utcnow


def reserve_platform_cost(session, limit, amount):
    if limit < 1 or amount < 1:
        raise ValueError("平台成本预算未配置")
    period = utcnow().date().isoformat()
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    session.execute(insert(PlatformDailyBudget).values(period=period, limit_microusd=limit,
                    spent_microusd=0, reserved_microusd=0).on_conflict_do_nothing(index_elements=["period"]))
    # The first admitted request freezes that day's durable ceiling. A process
    # with a stricter local limit also respects it; a config bump cannot raise
    # an existing day's DB ceiling by simply restarting a worker.
    total = PlatformDailyBudget.spent_microusd + PlatformDailyBudget.reserved_microusd + amount
    changed = session.execute(update(PlatformDailyBudget).where(
        PlatformDailyBudget.period == period, total <= PlatformDailyBudget.limit_microusd, total <= limit,
    ).values(reserved_microusd=PlatformDailyBudget.reserved_microusd + amount))
    if changed.rowcount != 1:
        raise ValueError("平台当日成本预算不足，已停止新请求")
    return period


def finalize_platform_cost(session, request, actual_cost):
    if request.billing_mode != "managed_gateway":
        return
    changed = session.execute(update(PlatformDailyBudget).where(
        PlatformDailyBudget.period == request.platform_budget_period,
        PlatformDailyBudget.reserved_microusd >= request.platform_reserved_microusd,
    ).values(reserved_microusd=PlatformDailyBudget.reserved_microusd - request.platform_reserved_microusd,
             spent_microusd=PlatformDailyBudget.spent_microusd + actual_cost))
    if changed.rowcount != 1:
        raise ValueError("平台成本预授权异常")
