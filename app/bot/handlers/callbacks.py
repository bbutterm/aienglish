import logging
from html import escape

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import RewriteCb, TtsCb, voice_offer_kb
from app.db import repo
from app.db.models import Message, User
from app.llm.openrouter import LLMError
from app.services.dialog import DialogService, SpeakStatus
from app.services.plans import limits_for
from app.services.usage import record_llm

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


@router.callback_query(RewriteCb.filter())
async def on_rewrite(
    callback: CallbackQuery,
    callback_data: RewriteCb,
    bot: Bot,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
) -> None:
    """«Как сказать лучше»: реплика пользователя перед этим ответом — как сказал бы носитель."""
    if not await dialog.features.enabled("native_rewrite"):
        await _safe_answer(callback)
        return
    reply = await session.get(Message, callback_data.message_id)
    original = None
    if reply is not None and reply.user_id == user.id:
        original = await session.scalar(
            select(Message)
            .where(Message.user_id == user.id, Message.role == "user", Message.id < reply.id)
            .order_by(Message.id.desc())
            .limit(1)
        )
    if original is None or not (original.text or "").strip():
        await _safe_answer(callback, texts.REWRITE_NOT_FOUND)
        return
    since = repo.day_start_utc(user.timezone)
    used = await repo.count_usage_since(session, user.id, "llm_rewrite", since)
    if used >= limits_for(user, dialog.settings).messages:
        await _safe_answer(callback, texts.REWRITE_LIMIT, alert=True)
        return
    await _safe_answer(callback)
    prev_bot = await session.scalar(
        select(Message.text)
        .where(Message.user_id == user.id, Message.role == "assistant", Message.id < original.id)
        .order_by(Message.id.desc())
        .limit(1)
    )
    await session.commit()  # запрос к модели — без открытой транзакции
    try:
        rewrite, llm = await dialog.rewriter.rewrite(user, original.text, prev_bot)
    except (LLMError, ValueError):
        logger.exception("Rewrite failed for user %s", user.id)
        await bot.send_message(callback.message.chat.id, texts.REWRITE_FAILED)
        return
    await record_llm(session, user.id, "llm_rewrite", llm)
    await session.commit()
    text = texts.REWRITE_RESULT.format(
        original=escape(original.text[:600]), native=escape(rewrite.native)
    )
    if rewrite.is_same(original.text):
        text += texts.REWRITE_FINE
    else:
        text += "\n\n" + "\n".join(
            f"• <s>{escape(c['from'])}</s> → <b>{escape(c['to'])}</b>"
            + (f" — {escape(c['why_ru'])}" if c["why_ru"] else "")
            for c in rewrite.changes
        )
    await bot.send_message(
        callback.message.chat.id,
        text,
        reply_to_message_id=original.tg_message_id,
        allow_sending_without_reply=True,
    )
