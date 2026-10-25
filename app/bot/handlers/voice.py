import logging

from aiogram import Bot, F, Router
from aiogram.types import Message as TgMessage
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers.common import respond
from app.bot.handlers.text import MAX_TEXT_CHARS
from app.bot.middlewares import DialogGateMiddleware
from app.config import Settings
from app.db.models import User
from app.services.dialog import DialogService
from app.services.stt import STTError

logger = logging.getLogger(__name__)

router = Router(name="voice")
# Голосовое Telegram (Opus) — 2–4 КБ в секунду; 16 КБ/с — с большим запасом.
VOICE_BYTES_PER_SECOND = 16_000
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
    # Длительность сообщает клиент отправителя; размер файла подделать сложнее.
    too_big = (voice.file_size or 0) > settings.max_voice_seconds * VOICE_BYTES_PER_SECOND
    if voice.duration > settings.max_voice_seconds or too_big:
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
    text = text[:MAX_TEXT_CHARS]  # как и у текстовых реплик

    # Транскрипт не отдельным сообщением, а цитатой в ответе (см. deliver_reply).
    await respond(message, bot, session, user, dialog, kind="voice", text=text)
