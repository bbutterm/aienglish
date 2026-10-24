from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message as TgMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers.common import send_opener
from app.bot.keyboards import modes_kb, voice_kb
from app.bot.onboarding import start_onboarding
from app.config import Settings
from app.db import repo
from app.db.models import User
from app.personas import get_persona
from app.services.dialog import DialogService
from app.services.review import build_review, format_mistakes
from app.services.usage import collect_stats, format_stats

router = Router(name="commands")

MISTAKES_LIMIT = 10


def mode_menu_text(user: User, settings: Settings) -> str:
    return texts.MODE_MENU.format(
        every=settings.review_every, current=texts.MODES.get(user.mode, user.mode)
    )


def voice_menu_text(user: User) -> str:
    persona = get_persona(user.persona)
    return texts.VOICE_MENU.format(
        always="вкл" if user.voice_always else "выкл", flag=persona.flag, name=persona.name
    )


@router.message(Command("help"))
async def cmd_help(message: TgMessage, settings: Settings) -> None:
    await message.answer(
        texts.HELP.format(
            max_voice=settings.max_voice_seconds,
            msg_limit=settings.daily_msg_limit,
            tts_limit=settings.daily_tts_limit,
        )
    )


@router.message(Command("mode"))
async def cmd_mode(message: TgMessage, user: User, settings: Settings) -> None:
    await message.answer(mode_menu_text(user, settings), reply_markup=modes_kb(user.mode))


@router.message(Command("voice"))
async def cmd_voice(message: TgMessage, user: User) -> None:
    await message.answer(voice_menu_text(user), reply_markup=voice_kb(user))


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
    state: FSMContext,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
    settings: Settings,
    bot: Bot,
) -> None:
    if not user.onboarded:
        await start_onboarding(message, state, user)
        return
    await dialog.memory.reset(session, user)
    await message.answer(texts.RESET_DONE)
    # Новая тема — это LLM-вызов, поэтому уважаем дневной лимит.
    if await dialog.messages_left(session, user) <= 0:
        await message.answer(texts.LIMIT_REACHED.format(limit=settings.daily_msg_limit))
        return
    await send_opener(bot, message.chat.id, session, user, dialog, reason="reset")


@router.message(Command("stats"))
async def cmd_stats(message: TgMessage, session: AsyncSession, settings: Settings) -> None:
    if message.from_user is None or message.from_user.id not in settings.admin_ids:
        await message.answer(texts.ADMIN_ONLY)
        return
    await message.answer(format_stats(await collect_stats(session)))


@router.message(F.text.startswith("/"))
async def unknown_command(message: TgMessage) -> None:
    await message.answer(texts.UNKNOWN_COMMAND)
