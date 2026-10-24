from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery
from aiogram.types import Message as TgMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers.common import drop_keyboard, send_opener
from app.bot.handlers.settings import settings_text
from app.bot.keyboards import LevelCb, settings_kb
from app.bot.onboarding import welcome
from app.config import Settings
from app.db.models import User
from app.services.dialog import DialogService

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(
    message: TgMessage,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
    settings: Settings,
    bot: Bot,
) -> None:
    if user.onboarded:
        await message.answer(
            texts.SETTINGS_BACK + settings_text(user, settings), reply_markup=settings_kb(user)
        )
        return
    # Без анкеты: приветствие и сразу первая реплика собеседника.
    await welcome(message, session, user, settings)
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
