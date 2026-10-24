"""Премиум за звёзды: /premium, проверка перед оплатой, успешная оплата, /paysupport и /terms."""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, PreCheckoutQuery
from aiogram.types import Message as TgMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.config import Settings
from app.db.models import User
from app.features import FeatureFlags
from app.personas import get_persona
from app.services import payments
from app.services.plans import is_premium

logger = logging.getLogger(__name__)

router = Router(name="payments")


@router.message(Command("premium"))
async def cmd_premium(
    message: TgMessage, bot: Bot, user: User, settings: Settings, features: FeatureFlags
) -> None:
    if not await features.enabled("payments_stars"):
        await message.answer(texts.PREMIUM_OFF)
        return
    text = texts.PREMIUM_PITCH.format(
        persona=get_persona(user.persona).name, messages=settings.premium_daily_msg_limit
    )
    if is_premium(user):
        text += texts.PREMIUM_UNTIL.format(until=f"{user.premium_until:%d.%m.%Y}")
    rows = []
    for plan in payments.PLANS.values():
        link = await payments.invoice_link(bot, settings, user.id, plan)
        label = texts.PREMIUM_BUTTON.format(title=plan.title, stars=plan.stars(settings))
        rows.append([InlineKeyboardButton(text=label, url=link)])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery, settings: Settings) -> None:
    # Ответить нужно за 10 секунд — только сверяем счёт с пользователем и ценой.
    problem = payments.check_checkout(
        query.invoice_payload, query.from_user.id, query.total_amount, settings
    )
    await query.answer(ok=problem is None, error_message=problem)


@router.message(F.successful_payment)
async def on_paid(message: TgMessage, session: AsyncSession, user: User) -> None:
    record = await payments.apply_payment(session, user, message.successful_payment)
    if record is None:
        logger.warning("Duplicate or unknown payment from %s", user.id)
        return
    logger.info("Payment %s from %s: %s ⭐", record.plan, user.id, record.amount)
    await message.answer(texts.PREMIUM_THANKS.format(until=f"{user.premium_until:%d.%m.%Y}"))


@router.message(Command("paysupport"))
async def cmd_paysupport(message: TgMessage) -> None:
    await message.answer(texts.PAYSUPPORT)


@router.message(Command("terms"))
async def cmd_terms(message: TgMessage) -> None:
    await message.answer(texts.TERMS)
