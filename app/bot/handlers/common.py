import asyncio
import logging
from html import escape
from typing import Literal

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.types import Message as TgMessage
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import tts_kb
from app.db import repo
from app.db.models import Message, User
from app.llm.openrouter import LLMError
from app.services.dialog import DialogService, SpeakStatus
from app.services.review import build_review

logger = logging.getLogger(__name__)

ANALYST_WAIT_SECONDS = 20
FALLBACK_OPENER = "Hey! So, how's your day going?"
MAX_REPLY_CHARS = 3500  # с запасом под HTML-экранирование до лимита Telegram в 4096


def format_reply_html(text: str) -> str:
    """Ответ собеседника → HTML: строка исправлений ✏️ курсивом, остальное экранируем."""
    lines = []
    for line in text.splitlines():
        if line.strip().startswith("✏"):
            lines.append(f"<i>{escape(line.strip())}</i>")
        else:
            lines.append(escape(line))
    return "\n".join(lines)


async def edit_callback_text(
    callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Правит сообщение с кнопками. Недоступное (старше 48 ч) и «not modified» — не ошибка."""
    if not isinstance(callback.message, TgMessage):
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        logger.debug("Message edit failed", exc_info=True)


async def deliver_reply(
    bot: Bot, chat_id: int, session: AsyncSession, user: User, dialog: DialogService, reply: Message
) -> None:
    markup = None if user.voice_always else tts_kb(reply.id)
    text = format_reply_html((reply.text or "")[:MAX_REPLY_CHARS])
    sent = await bot.send_message(chat_id, text, reply_markup=markup)
    reply.tg_message_id = sent.message_id
    await session.commit()
    if not user.voice_always:
        return
    status = await dialog.speak(bot, chat_id, session, user, reply, notify_limit=True)
    if status is SpeakStatus.FAILED:
        # Автоозвучка не вышла — даём кнопку, чтобы попробовать ещё раз.
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=sent.message_id, reply_markup=tts_kb(reply.id)
            )
        except TelegramBadRequest:
            logger.warning("Could not attach TTS button to message %s", sent.message_id)


async def respond(
    message: TgMessage,
    bot: Bot,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
    *,
    kind: Literal["text", "voice", "photo"],
    text: str | None,
    image: bytes | None = None,
) -> None:
    chat_id = message.chat.id
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
            turn = await dialog.user_turn(
                session,
                user,
                kind=kind,
                text=text,
                image=image,
                tg_message_id=message.message_id,
            )
    except LLMError:
        logger.exception("Companion failed for user %s", user.id)
        await message.answer(texts.LLM_FAILED)
        return

    await deliver_reply(bot, chat_id, session, user, dialog, turn.reply)

    if await dialog.review_due(session, user):
        if turn.analysis is not None:
            # Дождёмся аналитика, чтобы последняя реплика попала в разбор.
            await asyncio.wait({turn.analysis}, timeout=ANALYST_WAIT_SECONDS)
        report = await build_review(session, user)
        await bot.send_message(chat_id, report or texts.REVIEW_EMPTY)


async def send_opener(
    bot: Bot,
    chat_id: int,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
    reason: Literal["onboarding", "reset"],
) -> None:
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
            reply = await dialog.opener(session, user, reason)
    except LLMError:
        logger.exception("Opener failed for user %s", user.id)
        reply = await repo.add_message(
            session, user_id=user.id, role="assistant", kind="text", text=FALLBACK_OPENER
        )
    await deliver_reply(bot, chat_id, session, user, dialog, reply)
