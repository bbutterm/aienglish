from aiogram import Router
from aiogram.types import Message as TgMessage

from app.bot import texts

router = Router(name="fallback")


@router.message()
async def on_unsupported(message: TgMessage) -> None:
    await message.answer(texts.UNSUPPORTED)
