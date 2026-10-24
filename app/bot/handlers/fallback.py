from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.types import Message as TgMessage

from app.bot import texts

router = Router(name="fallback")


@router.message(F.text.startswith("/"))
async def unknown_command(message: TgMessage) -> None:
    await message.answer(texts.UNKNOWN_COMMAND)


@router.message()
async def on_unsupported(message: TgMessage) -> None:
    await message.answer(texts.UNSUPPORTED)


@router.callback_query()
async def on_stale_button(callback: CallbackQuery) -> None:
    # Кнопки из старых версий меню (например, прежней анкеты знакомства).
    await callback.answer(texts.STALE_MENU, show_alert=True)
