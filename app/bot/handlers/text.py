from aiogram import Bot, F, Router
from aiogram.types import Message as TgMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers.common import respond
from app.bot.middlewares import DialogGateMiddleware
from app.db.models import User
from app.services.dialog import DialogService

router = Router(name="text")
router.message.middleware(DialogGateMiddleware())

MAX_TEXT_CHARS = 2000


@router.message(F.text, ~F.text.startswith("/"))
async def on_text(
    message: TgMessage, bot: Bot, session: AsyncSession, user: User, dialog: DialogService
) -> None:
    if len(message.text) > MAX_TEXT_CHARS:
        await message.answer(texts.TEXT_TOO_LONG.format(limit=MAX_TEXT_CHARS))
        return
    await respond(message, bot, session, user, dialog, kind="text", text=message.text)
