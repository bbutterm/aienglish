"""«Разбор пересланного»: английский пост или сообщение → суть по-русски и слова в карточки."""

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.types import Message as TgMessage
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.after_turn import app_button
from app.bot.middlewares import DialogGateMiddleware
from app.db import repo
from app.db.models import User
from app.llm.openrouter import LLMError
from app.services.dialog import DialogService
from app.services.extract import looks_english
from app.services.plans import limits_for
from app.services.usage import record_llm

logger = logging.getLogger(__name__)

router = Router(name="forward")

FORWARDS_PER_MESSAGES = 5  # разборов в день — пятая часть дневного лимита реплик
router.message.middleware(DialogGateMiddleware())


async def forwarded_english(message: TgMessage, dialog: DialogService) -> bool:
    """Пересланный английский текст при включённой фиче; иначе — обычная реплика в чат."""
    if message.forward_origin is None or not looks_english(message.text or message.caption):
        return False
    return await dialog.features.enabled("forward_words")


@router.message(F.forward_origin, forwarded_english)
async def on_forward(
    message: TgMessage, bot: Bot, session: AsyncSession, user: User, dialog: DialogService
) -> None:
    text = message.text or message.caption or ""
    since = repo.day_start_utc(user.timezone)
    used = await repo.count_usage_since(session, user.id, "llm_forward", since)
    if used >= max(1, limits_for(user, dialog.settings).messages // FORWARDS_PER_MESSAGES):
        await message.reply(texts.FORWARD_LIMIT)
        return
    await session.commit()  # запрос к модели — без открытой транзакции
    try:
        async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
            extracted = await dialog.extractor.from_text(user, text)
    except (LLMError, ValueError):
        logger.exception("Forward extraction failed for user %s", user.id)
        await message.reply(texts.FORWARD_FAILED)
        return
    await record_llm(session, user.id, "llm_forward", extracted.llm)
    await repo.save_vocab(session, user.id, extracted.words)
    await session.commit()
    summary = escape(extracted.summary_ru or "—")
    if not extracted.words:
        await message.reply(texts.FORWARD_NO_WORDS.format(summary=summary))
        return
    words = "\n".join(
        f"• <b>{escape(w.term)}</b> — {escape(w.translation_ru or '')}" for w in extracted.words
    )
    await message.reply(
        texts.FORWARD_WORDS.format(summary=summary, words=words),
        reply_markup=app_button(dialog, texts.WORDS_BUTTON, "words"),
    )
