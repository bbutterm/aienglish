"""Единый экран настроек: озвучка, собеседник, уровень, исправления — каждое в одно нажатие."""

from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery
from aiogram.types import Message as TgMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers.common import edit_callback_text
from app.bot.keyboards import LEVEL_CODES, LevelCb, ModeCb, PersonaCb, VoiceModeCb, settings_kb
from app.config import Settings
from app.db.models import User
from app.personas import PERSONAS, get_persona

router = Router(name="settings")


def settings_text(user: User, settings: Settings) -> str:
    persona = get_persona(user.persona)
    return texts.SETTINGS.format(
        voice=texts.VOICE_MODES.get(user.voice_mode, user.voice_mode),
        flag=persona.flag,
        persona=persona.name,
        level=user.level,
        mode=texts.MODES.get(user.mode, user.mode).format(every=settings.review_every),
    )


async def refresh(callback: CallbackQuery, user: User, settings: Settings) -> None:
    await callback.answer(texts.SAVED)
    await edit_callback_text(callback, settings_text(user, settings), settings_kb(user))


@router.message(Command("settings", "voice", "mode"))
async def cmd_settings(message: TgMessage, user: User, settings: Settings) -> None:
    await message.answer(settings_text(user, settings), reply_markup=settings_kb(user))


@router.callback_query(VoiceModeCb.filter(F.src == "s"))
async def on_voice_mode(
    callback: CallbackQuery,
    callback_data: VoiceModeCb,
    session: AsyncSession,
    user: User,
    settings: Settings,
) -> None:
    if callback_data.mode in texts.VOICE_MODES:
        user.voice_mode = callback_data.mode
        await session.commit()
    await refresh(callback, user, settings)


@router.callback_query(VoiceModeCb.filter(F.src == "o"))
async def on_voice_offer(
    callback: CallbackQuery, callback_data: VoiceModeCb, session: AsyncSession, user: User
) -> None:
    if callback_data.mode in texts.VOICE_MODES:
        user.voice_mode = callback_data.mode
        await session.commit()
    await callback.answer(texts.SAVED)
    await edit_callback_text(
        callback, texts.VOICE_OFFER_DONE.format(mode=texts.VOICE_MODES[user.voice_mode])
    )


@router.callback_query(PersonaCb.filter())
async def on_persona(
    callback: CallbackQuery,
    callback_data: PersonaCb,
    session: AsyncSession,
    user: User,
    settings: Settings,
) -> None:
    if callback_data.key in PERSONAS:
        user.persona = callback_data.key
        await session.commit()
    await refresh(callback, user, settings)


@router.callback_query(LevelCb.filter(F.src == "s"))
async def on_level(
    callback: CallbackQuery,
    callback_data: LevelCb,
    session: AsyncSession,
    user: User,
    settings: Settings,
) -> None:
    if callback_data.level in LEVEL_CODES:
        user.level = callback_data.level
        await session.commit()
    await refresh(callback, user, settings)


@router.callback_query(ModeCb.filter())
async def on_mode(
    callback: CallbackQuery,
    callback_data: ModeCb,
    session: AsyncSession,
    user: User,
    settings: Settings,
) -> None:
    if callback_data.mode in texts.MODES and callback_data.mode != user.mode:
        user.mode = callback_data.mode
        if user.mode == "review":
            # Отсчёт реплик до автоматического разбора начинается с момента включения.
            user.last_review_at = datetime.now(UTC)
        await session.commit()
    await refresh(callback, user, settings)
