import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.types import Message as TgMessage
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers.common import respond
from app.bot.middlewares import DialogGateMiddleware
from app.config import Settings
from app.db.models import User
from app.services.dialog import DialogService
from app.services.stt import STTError

logger = logging.getLogger(__name__)

router = Router(name="voice")
router.message.middleware(DialogGateMiddleware())


@router.message(F.voice)
async def on_voice(
    message: TgMessage,
    bot: Bot,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
    settings: Settings,
) -> None:
    voice = message.voice
    if voice.duration > settings.max_voice_seconds:
        await message.answer(texts.VOICE_TOO_LONG.format(limit=settings.max_voice_seconds))
        return

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        audio = await bot.download(voice)
        try:
            text = await dialog.transcribe(session, user, audio.read(), voice.duration)
        except STTError:
            logger.exception("STT failed for user %s", user.id)
            text = ""
    if not text:
        await message.reply(texts.STT_FAILED)
        return

    if settings.show_transcript:
        await message.reply(texts.TRANSCRIPT.format(text=escape(text)))
    await respond(message, bot, session, user, dialog, kind="voice", text=text)
