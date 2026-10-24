from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import Message as TgMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.handlers.common import edit_callback_text, send_opener
from app.bot.keyboards import LevelCb, OnboardCb, PersonaCb, levels_kb, personas_kb, restart_kb
from app.bot.onboarding import Onboarding, start_onboarding
from app.db.models import User
from app.personas import PERSONAS
from app.services.dialog import DialogService

router = Router(name="start")

MAX_NAME_LEN = 32


@router.message(CommandStart())
async def cmd_start(message: TgMessage, state: FSMContext, user: User) -> None:
    if user.onboarded:
        await state.clear()
        await message.answer(
            texts.WELCOME_BACK.format(name=escape(user.name)), reply_markup=restart_kb()
        )
        return
    await start_onboarding(message, state, user)


@router.callback_query(OnboardCb.filter(F.action == "restart"))
async def on_restart(callback: CallbackQuery, state: FSMContext, user: User) -> None:
    await callback.answer()
    await start_onboarding(callback.message, state, user)


async def _save_name(
    message: TgMessage, state: FSMContext, session: AsyncSession, user: User, name: str
) -> None:
    user.display_name = name
    await session.commit()
    await state.set_state(Onboarding.level)
    await message.answer(texts.ASK_LEVEL.format(name=escape(name)), reply_markup=levels_kb())


@router.message(Onboarding.name, F.text, ~F.text.startswith("/"))
async def on_name(message: TgMessage, state: FSMContext, session: AsyncSession, user: User) -> None:
    name = message.text.strip().splitlines()[0].strip() if message.text.strip() else ""
    if not name or len(name) > MAX_NAME_LEN:
        await message.answer(texts.NAME_INVALID)
        return
    await _save_name(message, state, session, user, name)


@router.callback_query(Onboarding.name, OnboardCb.filter(F.action == "use_name"))
async def on_use_tg_name(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, user: User
) -> None:
    await callback.answer()
    name = (user.first_name or "friend")[:MAX_NAME_LEN]
    await edit_callback_text(callback, texts.WELCOME)
    await _save_name(callback.message, state, session, user, name)


@router.message(Onboarding.name, ~F.text.startswith("/"))
async def on_name_invalid(message: TgMessage) -> None:
    await message.answer(texts.NAME_INVALID)


@router.callback_query(Onboarding.level, LevelCb.filter())
async def on_level(
    callback: CallbackQuery,
    callback_data: LevelCb,
    state: FSMContext,
    session: AsyncSession,
    user: User,
) -> None:
    if callback_data.level not in texts.LEVELS:
        await callback.answer()
        return
    user.level = callback_data.level
    await session.commit()
    await state.set_state(Onboarding.persona)
    await callback.answer()
    await edit_callback_text(
        callback, texts.LEVEL_CHOSEN.format(level=texts.LEVELS[callback_data.level])
    )
    await callback.message.answer(texts.ASK_PERSONA, reply_markup=personas_kb())


@router.message(Onboarding.level, ~F.text.startswith("/"))
async def on_level_text(message: TgMessage) -> None:
    await message.answer(texts.ASK_LEVEL_AGAIN, reply_markup=levels_kb())


@router.callback_query(Onboarding.persona, PersonaCb.filter(F.onboarding))
async def on_persona(
    callback: CallbackQuery,
    callback_data: PersonaCb,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
    bot: Bot,
) -> None:
    persona = PERSONAS.get(callback_data.key)
    if persona is None:
        await callback.answer()
        return
    user.persona = persona.key
    user.onboarded = True
    await session.commit()
    await state.clear()
    await callback.answer()
    await edit_callback_text(
        callback, texts.PERSONA_CHOSEN.format(flag=persona.flag, name=persona.name)
    )
    await callback.message.answer(texts.ONBOARDING_DONE.format(name=persona.name))
    await send_opener(bot, callback.message.chat.id, session, user, dialog, reason="onboarding")


@router.message(Onboarding.persona, ~F.text.startswith("/"))
async def on_persona_text(message: TgMessage) -> None:
    await message.answer(texts.ASK_PERSONA_AGAIN, reply_markup=personas_kb())


@router.callback_query(LevelCb.filter())
@router.callback_query(PersonaCb.filter(F.onboarding))
@router.callback_query(OnboardCb.filter(F.action == "use_name"))
async def on_stale_onboarding_button(callback: CallbackQuery) -> None:
    await callback.answer(texts.STALE_MENU, show_alert=True)
