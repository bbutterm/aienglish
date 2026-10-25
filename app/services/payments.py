"""Премиум за звёзды Telegram: месячная подписка (продлевается сама), пропуск на 90 дней и год.

Цифровые товары в ботах Telegram продаются только за звёзды (XTR), provider token не нужен.
"""

from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import LabeledPrice, SuccessfulPayment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Payment, User
from app.services.plans import extend_premium

CURRENCY = "XTR"
SUBSCRIPTION_PERIOD = 30 * 24 * 3600  # Telegram принимает только 30 дней


@dataclass(frozen=True)
class Plan:
    key: str
    title: str
    description: str
    days: int
    subscription: bool

    def stars(self, settings: Settings) -> int:
        return getattr(settings, f"stars_{self.key}")


PLANS: dict[str, Plan] = {
    p.key: p
    for p in (
        Plan(
            "month",
            "Премиум на месяц",
            "Живой голос без дневного лимита, 3 урока в день, подкаст и миссии. Продлевается "
            "каждые 30 дней, отменить можно в любой момент.",
            30,
            True,
        ),
        Plan(
            "quarter",
            "Премиум на 90 дней",
            "Всё из премиума на три месяца, без автопродления.",
            90,
            False,
        ),
        Plan(
            "year", "Премиум на год", "Всё из премиума на 12 месяцев — выгоднее всего.", 365, False
        ),
    )
}


def make_payload(user_id: int, plan: Plan) -> str:
    return f"premium:{plan.key}:{user_id}"


def parse_payload(payload: str) -> tuple[Plan, int] | None:
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != "premium" or parts[1] not in PLANS or not parts[2].isdigit():
        return None
    return PLANS[parts[1]], int(parts[2])


async def invoice_link(bot: Bot, settings: Settings, user_id: int, plan: Plan) -> str:
    return await bot.create_invoice_link(
        title=plan.title,
        description=plan.description,
        payload=make_payload(user_id, plan),
        currency=CURRENCY,
        prices=[LabeledPrice(label=plan.title, amount=plan.stars(settings))],
        subscription_period=SUBSCRIPTION_PERIOD if plan.subscription else None,
    )


def check_checkout(payload: str, user_id: int, amount: int, settings: Settings) -> str | None:
    """Проверка перед оплатой: None — всё в порядке, иначе текст ошибки для пользователя."""
    parsed = parse_payload(payload)
    if parsed is None or parsed[1] != user_id:
        return "Этот счёт устарел — открой /premium ещё раз."
    plan, _ = parsed
    if amount != plan.stars(settings):
        return "Цена изменилась — открой /premium ещё раз."
    return None


async def apply_payment(
    session: AsyncSession, user: User, payment: SuccessfulPayment
) -> Payment | None:
    """Продлевает премиум. Повтор того же платежа (Telegram может прислать дважды) — None."""
    parsed = parse_payload(payment.invoice_payload)
    if parsed is None:
        return None
    plan, _ = parsed
    charge = payment.telegram_payment_charge_id
    if await session.scalar(select(Payment.id).where(Payment.charge_id == charge)):
        return None
    # Каждый платёж (и каждое продление подписки) добавляет оплаченные дни к текущему концу
    # премиума — поверх пропуска, бонуса за друга или выдачи админом.
    extend_premium(user, plan.days)
    record = Payment(
        user_id=user.id,
        plan=plan.key,
        currency=payment.currency,
        amount=payment.total_amount,
        charge_id=charge,
        recurring=bool(payment.is_recurring),
        premium_until=user.premium_until,
    )
    session.add(record)
    await session.commit()
    return record
