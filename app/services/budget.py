"""Calendar-month budget controls used by both UI and model requests."""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Budget, User, Wallet
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


def create_budget(session: Session, user_id: str, amount: int, *, kind: str = "prepaid_credit") -> Budget:
    if amount <= 0:
        raise BudgetError("预算必须大于 0")
    if kind not in {"prepaid_credit", "provider_spend_cap"}:
        raise BudgetError("预算类型无效")
    start, end = month_bounds()
    try:
        # This is the same first serialization point used by request
        # reservation. It prevents a new hold from racing an active-budget
        # replacement on PostgreSQL; the partial unique index is the durable
        # backstop for every dialect and failed lock acquisition.
        user = session.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if user is None:
            session.rollback()
            raise BudgetError("账户不存在", 409)
        # Prepaid budget replacement also serializes with the wallet mutation
        # path. Keep the same User -> Wallet -> Budget order as prepaid
        # reservation; provider-spend caps never touch the customer wallet.
        if kind == "prepaid_credit":
            wallet = session.scalar(
                select(Wallet)
                .where(Wallet.user_id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if wallet is None:
                session.rollback()
                raise BudgetError("钱包不存在", 409)
        current = session.scalar(
            select(Budget)
            .where(
                Budget.user_id == user_id,
                Budget.kind == kind,
                Budget.status == "active",
                Budget.period_start == start,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
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
            kind=kind,
            limit_microusd=amount,
            spent_microusd=carried_spend,
            period_start=start,
            period_end=end,
        )
        session.add(budget)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise BudgetError("预算已更新或并发替换，请重试", 409) from exc
    return budget


def get_owned_budget(session: Session, user_id: str, budget_id: str) -> Budget | None:
    return session.scalar(select(Budget).where(Budget.id == budget_id, Budget.user_id == user_id))


def active_budget(session: Session, user_id: str, *, kind: str | None = None) -> Budget | None:
    now = utcnow()
    filters = [
        Budget.user_id == user_id,
        Budget.status == "active",
        Budget.period_start <= now,
        Budget.period_end > now,
    ]
    if kind is not None:
        filters.append(Budget.kind == kind)
    return session.scalar(select(Budget).where(*filters).order_by(Budget.created_at.desc()))
