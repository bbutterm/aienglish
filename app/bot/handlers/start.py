from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery
from aiogram.types import Message as TgMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers.common import drop_keyboard, edit_callback_text, send_opener
from app.bot.handlers.settings import settings_text
from app.bot.keyboards import LevelCb, settings_kb
from app.bot.onboarding import welcome
from app.config import Settings
from app.db.models import User
from app.features import FeatureFlags
from app.services import journey
from app.services.dialog import DialogService
from app.services.plans import extend_premium

TRIAL_DAYS = 7

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(
    message: TgMessage,
    command: CommandObject,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
    settings: Settings,
    features: FeatureFlags,
    bot: Bot,
) -> None:
    if user.onboarded:
        ping = await features.enabled("friend_ping")
        await message.answer(
            texts.SETTINGS_BACK + settings_text(user, settings, ping),
            reply_markup=settings_kb(user, ping),
        )
        return
    # Ссылка с меткой (реклама, лендинг, приглашение друга) — запоминаем источник.
    invited = await journey.apply_start(session, user, journey.parse_start(command.args))
    prefix = texts.REFERRAL_WELCOME if invited and await features.enabled("referrals") else ""
    if await features.enabled("trial_week") and user.premium_until is None:
        # Пробная неделя: новичок сразу видит продукт целиком — живой голос, подкаст, сцены.
        extend_premium(user, TRIAL_DAYS)
        await session.commit()
        prefix = texts.TRIAL_WELCOME + prefix
    # Без анкеты: приветствие и сразу первая реплика собеседника.
    await welcome(message, session, user, settings, prefix)
    await send_opener(bot, message.chat.id, session, user, dialog, reason="onboarding")


@router.callback_query(LevelCb.filter(F.src == "w"))
async def on_quick_level(
    callback: CallbackQuery, callback_data: LevelCb, session: AsyncSession, user: User
) -> None:
    if callback_data.level in texts.QUICK_LEVELS:
        user.level = callback_data.level
        await session.commit()
    await callback.answer(texts.LEVEL_SET.format(level=user.level))
    await drop_keyboard(callback)  # кнопки уровня больше не нужны, текст приветствия остаётся


@router.callback_query(LevelCb.filter(F.src == "j"))
async def on_level_hint(
    callback: CallbackQuery, callback_data: LevelCb, session: AsyncSession, user: User
) -> None:
    """Подсказка «судя по речи, твой уровень ближе к …»: подстроиться или оставить."""
    if callback_data.level in journey.LEVELS:
        user.level = callback_data.level
        await session.commit()
    await callback.answer()
    await edit_callback_text(callback, texts.LEVEL_SET.format(level=user.level))
