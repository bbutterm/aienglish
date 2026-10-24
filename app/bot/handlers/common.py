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
from app.bot.after_turn import after_turn, photo_words
from app.bot.keyboards import reply_kb
from app.db import repo
from app.db.models import Message, User
from app.llm.openrouter import LLMError
from app.services.companion import OpenerReason
from app.services.dialog import DialogService, SpeakStatus, wants_voice
from app.services.review import build_review

logger = logging.getLogger(__name__)

ANALYST_WAIT_SECONDS = 20
FALLBACK_OPENER = "Hey! So, how's your day going?"
MAX_REPLY_CHARS = 3500  # с запасом под HTML-экранирование до лимита Telegram в 4096
CAPTION_LIMIT = 1024  # лимит подписи к голосовому
MAX_QUOTE = 400
MIN_QUOTE = 60


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


async def drop_keyboard(callback: CallbackQuery) -> None:
    if not isinstance(callback.message, TgMessage):
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        logger.debug("Keyboard removal failed", exc_info=True)


def transcript_quote(transcript: str, limit: int) -> str:
    """Что пользователь сказал голосом — цитатой над ответом."""
    text = transcript if len(transcript) <= limit else transcript[: limit - 1].rstrip() + "…"
    return f"<blockquote>🗣 {escape(text)}</blockquote>\n"


def voice_caption(reply_text: str, transcript: str | None) -> str | None:
    """Подпись к голосовому ответу; None — ответ не влезает в подпись, нужен обычный текст."""
    if len(reply_text) > CAPTION_LIMIT - 24:
        return None
    body = format_reply_html(reply_text)
    room = CAPTION_LIMIT - len(reply_text) - 8
    if not transcript or room < MIN_QUOTE:
        return body
    return transcript_quote(transcript, min(room, MAX_QUOTE)) + body


async def deliver_reply(
    bot: Bot,
    chat_id: int,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
    reply: Message,
    *,
    voice: bool = False,
    transcript: str | None = None,
    rewrite: bool = False,
) -> None:
    """Один ответ — одно сообщение: голосовое с текстом в подписи или текст с кнопкой 🔊.

    rewrite — добавить кнопку «Как сказать лучше» (переписать реплику пользователя).
    """
    reply_text = (reply.text or "")[:MAX_REPLY_CHARS]
    if voice:
        caption = voice_caption(reply_text, transcript)
        if caption is not None:
            status = await dialog.speak(
                bot,
                chat_id,
                session,
                user,
                reply,
                caption=caption,
                notify_limit=True,
                reply_markup=reply_kb(reply.id, tts=False, rewrite=rewrite),
            )
            if status is SpeakStatus.SENT:
                return
    # Текстом: если голос не нужен, исчерпан лимит озвучки или TTS не ответил.
    text = format_reply_html(reply_text)
    if transcript:
        text = transcript_quote(transcript, MAX_QUOTE) + text
    sent = await bot.send_message(
        chat_id, text, reply_markup=reply_kb(reply.id, tts=True, rewrite=rewrite)
    )
    reply.tg_message_id = sent.message_id
    await session.commit()


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

    transcript = text if kind == "voice" and dialog.settings.show_transcript else None
    flags = await dialog.features.snapshot()
    await deliver_reply(
        bot,
        chat_id,
        session,
        user,
        dialog,
        turn.reply,
        voice=wants_voice(user, kind),
        transcript=transcript,
        rewrite=flags["native_rewrite"] and bool(text and text.strip()),
    )
    if kind == "photo" and image is not None and flags["photo_words"]:
        dialog.tasks.spawn(
            photo_words(bot, chat_id, user.id, dialog, image), name=f"photo-words:{user.id}"
        )
    await after_turn(bot, chat_id, session, user, dialog, turn)

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
    reason: OpenerReason,
    topic: str | None = None,
) -> None:
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
            reply = await dialog.opener(session, user, reason, topic)
    except LLMError:
        logger.exception("Opener failed for user %s", user.id)
        reply = await repo.add_message(
            session, user_id=user.id, role="assistant", kind="text", text=FALLBACK_OPENER
        )
    await deliver_reply(bot, chat_id, session, user, dialog, reply, voice=wants_voice(user, None))
