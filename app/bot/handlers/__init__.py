import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import ErrorEvent

from app.bot import texts
from app.bot.handlers import callbacks, commands, fallback, photo, settings, start, text, voice

logger = logging.getLogger(__name__)


def build_router() -> Router:
    root = Router(name="root")
    root.message.filter(F.chat.type == ChatType.PRIVATE)
    # Порядок важен: старт → команды → настройки и кнопки → диалог → всё остальное.
    root.include_routers(
        start.router,
        commands.router,
        settings.router,
        callbacks.router,
        text.router,
        voice.router,
        photo.router,
        fallback.router,
    )
    root.errors.register(on_error)
    return root


async def on_error(event: ErrorEvent) -> None:
    logger.error("Update handling failed", exc_info=event.exception)
    message = event.update.message or (
        event.update.callback_query.message if event.update.callback_query else None
    )
    if message is None:
        return
    try:
        await message.answer(texts.ERROR)
    except Exception:
        logger.debug("Could not notify user about error", exc_info=True)
