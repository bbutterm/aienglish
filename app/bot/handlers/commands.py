from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message as TgMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.after_turn import finish_mission
from app.bot.handlers.common import send_opener
from app.bot.keyboards import webapp_kb
from app.bot.onboarding import welcome
from app.config import Settings
from app.db import repo
from app.db.models import User
from app.services.dialog import DialogService
from app.services.plans import is_premium, limits_for
from app.services.review import build_review, format_mistakes
from app.services.usage import collect_stats, format_stats

router = Router(name="commands")

MISTAKES_LIMIT = 10


@router.message(Command("help"))
async def cmd_help(message: TgMessage, user: User, settings: Settings) -> None:
    limits = limits_for(user, settings)
    text = texts.HELP.format(
        max_voice=settings.max_voice_seconds, msg_limit=limits.messages, tts_limit=limits.tts
    )
    if is_premium(user):
        text += "\n" + texts.PREMIUM_ACTIVE.format(until=f"{user.premium_until:%d.%m.%Y}")
    await message.answer(text)


@router.message(Command("mistakes"))
async def cmd_mistakes(message: TgMessage, session: AsyncSession, user: User) -> None:
    mistakes = await repo.recent_mistakes(session, user.id, MISTAKES_LIMIT)
    if not mistakes:
        await message.answer(texts.NO_MISTAKES)
        return
    await message.answer(format_mistakes(texts.MISTAKES_TITLE, mistakes))


@router.message(Command("review"))
async def cmd_review(message: TgMessage, session: AsyncSession, user: User) -> None:
    report = await build_review(session, user)
    await message.answer(report or texts.REVIEW_EMPTY)


@router.message(Command("reset"))
async def cmd_reset(
    message: TgMessage,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
    settings: Settings,
    bot: Bot,
) -> None:
    if not user.onboarded:
        await welcome(message, session, user, settings)
    await dialog.memory.reset(session, user)
    await message.answer(texts.RESET_DONE)
    # Новая тема — это LLM-вызов: уважаем дневной лимит и расходуем из него одну реплику.
    async with dialog.locks.get(user.id):
        if await dialog.messages_left(session, user) <= 0:
            limit = limits_for(user, settings).messages
            await message.answer(texts.LIMIT_REACHED.format(limit=limit))
            return
        await send_opener(bot, message.chat.id, session, user, dialog, reason="reset")


@router.message(Command("stop"))
async def cmd_stop(
    message: TgMessage, session: AsyncSession, user: User, dialog: DialogService, bot: Bot
) -> None:
    if not user.active_mission:
        await message.answer(texts.NO_MISSION)
        return
    await finish_mission(bot, message.chat.id, session, user, dialog, completed=False)


@router.message(Command("app"))
async def cmd_app(message: TgMessage, settings: Settings) -> None:
    if not settings.webapp_url:
        await message.answer(texts.APP_NOT_CONFIGURED)
        return
    await message.answer(
        texts.APP_TEXT, reply_markup=webapp_kb(settings.webapp_url, texts.APP_BUTTON)
    )


@router.message(Command("admin"))
async def cmd_admin(message: TgMessage, settings: Settings) -> None:
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer(texts.ADMIN_ONLY)
        return
    if not settings.webapp_url:
        await message.answer(texts.APP_NOT_CONFIGURED)
        return
    await message.answer(
        texts.ADMIN_TEXT, reply_markup=webapp_kb(settings.webapp_url, texts.ADMIN_BUTTON, "admin")
    )


@router.message(Command("stats"))
async def cmd_stats(message: TgMessage, session: AsyncSession, settings: Settings) -> None:
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer(texts.ADMIN_ONLY)
        return
    await message.answer(format_stats(await collect_stats(session)))
