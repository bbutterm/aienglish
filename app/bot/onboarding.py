"""Быстрый старт: без анкеты. Имя — из Telegram, собеседник — по умолчанию, уровень — по желанию."""

from html import escape

from aiogram.types import Message as TgMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import quick_level_kb
from app.config import Settings
from app.db.models import User
from app.personas import get_persona


async def welcome(
    message: TgMessage, session: AsyncSession, user: User, settings: Settings
) -> None:
    """Знакомство одним сообщением; пользователь сразу может писать, кнопки уровня необязательны."""
    user.onboarded = True
    await session.commit()
    persona = get_persona(user.persona)
    await message.answer(
        texts.WELCOME.format(
            name=escape(user.name),
            persona=persona.name,
            flag=persona.flag,
            app=texts.WELCOME_APP if settings.webapp_url else "",
        ),
        reply_markup=quick_level_kb(),
    )
