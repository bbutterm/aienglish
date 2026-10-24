import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers.commands import mode_menu_text, voice_menu_text
from app.bot.handlers.common import edit_callback_text
from app.bot.keyboards import ModeCb, PersonaCb, TtsCb, VoiceCb, modes_kb, voice_kb
from app.config import Settings
from app.db.models import Message, User
from app.personas import PERSONAS
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
    status = await dialog.speak(bot, callback.message.chat.id, session, user, message)
    if status is SpeakStatus.LIMIT:
        await _safe_answer(callback, texts.TTS_LIMIT, alert=True)
    elif status is SpeakStatus.FAILED:
        await _safe_answer(callback, texts.TTS_FAILED)
    else:
        await _safe_answer(callback)


@router.callback_query(ModeCb.filter())
async def on_mode(
    callback: CallbackQuery,
    callback_data: ModeCb,
    session: AsyncSession,
    user: User,
    settings: Settings,
) -> None:
    if callback_data.mode not in texts.MODES:
        await _safe_answer(callback)
        return
    if callback_data.mode != user.mode:
        user.mode = callback_data.mode
        if user.mode == "review":
            # Отсчёт реплик до автоматического разбора начинается с момента включения.
            user.last_review_at = datetime.now(UTC)
        await session.commit()
    await _safe_answer(callback, texts.MODE_SET.format(mode=texts.MODES[user.mode]))
    await edit_callback_text(callback, mode_menu_text(user, settings), modes_kb(user.mode))


@router.callback_query(VoiceCb.filter(F.action == "toggle"))
async def on_voice_toggle(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    user.voice_always = not user.voice_always
    await session.commit()
    await _safe_answer(callback)
    await edit_callback_text(callback, voice_menu_text(user), voice_kb(user))


@router.callback_query(PersonaCb.filter(~F.onboarding))
async def on_persona_change(
    callback: CallbackQuery, callback_data: PersonaCb, session: AsyncSession, user: User
) -> None:
    if callback_data.key in PERSONAS and callback_data.key != user.persona:
        user.persona = callback_data.key
        await session.commit()
    await _safe_answer(callback)
    await edit_callback_text(callback, voice_menu_text(user), voice_kb(user))
