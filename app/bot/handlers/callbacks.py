import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import TtsCb, voice_offer_kb
from app.db.models import Message, User
from app.services.dialog import DialogService, SpeakStatus

logger = logging.getLogger(__name__)

router = Router(name="callbacks")


async def _safe_answer(
    callback: CallbackQuery, text: str | None = None, alert: bool = False
) -> None:
    try:
        await callback.answer(text, show_alert=alert)
    except TelegramBadRequest:
        # Например, «query is too old» после долгой озвучки.
        logger.debug("Callback answer failed", exc_info=True)


@router.callback_query(TtsCb.filter())
async def on_tts(
    callback: CallbackQuery,
    callback_data: TtsCb,
    bot: Bot,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
) -> None:
    message = await session.get(Message, callback_data.message_id)
    if message is None or message.user_id != user.id or message.role != "assistant":
        await _safe_answer(callback, texts.TTS_NOT_FOUND)
        return
    chat_id = callback.message.chat.id
    status = await dialog.speak(bot, chat_id, session, user, message)
    if status is SpeakStatus.LIMIT:
        await _safe_answer(callback, texts.TTS_LIMIT, alert=True)
    elif status is SpeakStatus.FAILED:
        await _safe_answer(callback, texts.TTS_FAILED)
    else:
        await _safe_answer(callback)
        if dialog.should_offer_voice(user):
            # Второе нажатие 🔊 за день — предлагаем озвучивать без кнопки.
            await bot.send_message(chat_id, texts.VOICE_OFFER, reply_markup=voice_offer_kb())
