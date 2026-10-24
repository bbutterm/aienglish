from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message as TgMessage
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.bot.onboarding import start_onboarding
from app.db import repo
from app.services.plans import limits_for

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DbSessionMiddleware(BaseMiddleware):
    """Сессия БД на каждый апдейт → data["session"]."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with self._session_factory() as session:
            data["session"] = session
            return await handler(event, data)


class UserMiddleware(BaseMiddleware):
    """Upsert пользователя по telegram id → data["user"]; заблокированных дальше не пускаем."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return None
        user = await repo.upsert_user(
            data["session"], tg_user.id, tg_user.username, tg_user.first_name
        )
        if user.blocked:
            return None  # заблокированных админом молча игнорируем
        data["user"] = user
        return await handler(event, data)


class DialogGateMiddleware(BaseMiddleware):
    """Для диалоговых хэндлеров: онбординг пройден и дневной лимит реплик не исчерпан."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        assert isinstance(event, TgMessage)
        user = data["user"]
        if not user.onboarded:
            await event.answer(texts.NEED_ONBOARDING)
            await start_onboarding(event, data["state"], user)
            return None
        if await data["dialog"].messages_left(data["session"], user) <= 0:
            limit = limits_for(user, data["settings"]).messages
            await event.answer(texts.LIMIT_REACHED.format(limit=limit))
            return None
        return await handler(event, data)
