"""Calendar-month budget controls used by both UI and model requests."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import Budget, Wallet
from ..security import utcnow


class BudgetError(RuntimeError):
    def __init__(self, message: str, status_code: int = 422) -> None:
        self.status_code = status_code
        super().__init__(message)


def month_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    value = now or utcnow()
    start = datetime(value.year, value.month, 1, tzinfo=timezone.utc)
    if value.month == 12:
        end = datetime(value.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(value.year, value.month + 1, 1, tzinfo=timezone.utc)
    return start, end


def create_budget(session: Session, user_id: str, amount: int) -> Budget:
    if amount <= 0:
        raise BudgetError("预算必须大于 0")
    start, end = month_bounds()
    # Serialize budget replacement with reservation/settlement on the wallet
    # row. A no-op UPDATE also obtains SQLite's write lock for the local MVP.
    lock_result = session.execute(update(Wallet).where(
        Wallet.user_id == user_id,
    ).values(balance_microusd=Wallet.balance_microusd))
    if lock_result.rowcount != 1:
        session.rollback()
        raise BudgetError("钱包不存在", 409)
    current = session.scalar(select(Budget).where(
        Budget.user_id == user_id,
        Budget.status == "active",
        Budget.period_start == start,
    ).order_by(Budget.created_at.desc()))
    carried_spend = 0
    if current is not None:
        if current.reserved_microusd > 0:
            session.rollback()
            raise BudgetError("仍有模型请求待结算，暂不能替换预算", 409)
        carried_spend = current.spent_microusd
        if amount < carried_spend:
            session.rollback()
            raise BudgetError("新预算不能低于本月已发生支出", 409)
        current.status = "superseded"
    budget = Budget(
        id=str(uuid.uuid4()),
        user_id=user_id,
        limit_microusd=amount,
        spent_microusd=carried_spend,
        period_start=start,
        period_end=end,
    )
    session.add(budget)
    session.commit()
    return budget


def get_owned_budget(session: Session, user_id: str, budget_id: str) -> Budget | None:
    return session.scalar(select(Budget).where(Budget.id == budget_id, Budget.user_id == user_id))


def active_budget(session: Session, user_id: str) -> Budget | None:
    now = utcnow()
    return session.scalar(select(Budget).where(
        Budget.user_id == user_id,
        Budget.status == "active",
        Budget.period_start <= now,
        Budget.period_end > now,
    ).order_by(Budget.created_at.desc()))
