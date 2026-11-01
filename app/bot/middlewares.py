from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message as TgMessage
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.bot.onboarding import welcome
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
        paid = isinstance(event, TgMessage) and event.successful_payment is not None
        if user.blocked and not paid:
            # Заблокированных админом молча игнорируем. Кроме оплаты: продление подписки
            # приходит без pre-checkout, звёзды уже списаны — платёж обязаны записать.
            return None
        data["user"] = user
        return await handler(event, data)


class UserLockMiddleware(BaseMiddleware):
    """Платные кнопки (🔊, «Как сказать лучше») одного пользователя — по очереди."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with data["dialog"].locks.get(data["user"].id):
            return await handler(event, data)


class DialogGateMiddleware(BaseMiddleware):
    """Для диалоговых хэндлеров: знакомство (без анкеты) и дневной лимит реплик.

    Реплики одного пользователя идут по очереди: проверка лимита видит уже записанные.
    """

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with data["dialog"].locks.get(data["user"].id):
            return await self._gate(handler, event, data)

    @staticmethod
    async def _gate(handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        assert isinstance(event, TgMessage)
        user = data["user"]
        if not user.onboarded:
            # Написал, не нажимая /start: здороваемся и сразу отвечаем, ничего не спрашивая.
            await welcome(event, data["session"], user, data["settings"])
        if await data["dialog"].messages_left(data["session"], user) <= 0:
            limit = limits_for(user, data["settings"]).messages
            text = texts.LIMIT_REACHED.format(limit=limit)
            if await data["features"].enabled("payments_stars"):
                text += texts.LIMIT_UPSELL
            await event.answer(text)
            return None
        # Дальше скачивание файла, STT и модель — соединение с БД не держим.
        await data["session"].commit()
        return await handler(event, data)
