from aiogram import Bot, F, Router
from aiogram.types import Message as TgMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.common import respond
from app.bot.middlewares import DialogGateMiddleware
from app.db.models import User
from app.services.dialog import DialogService

router = Router(name="photo")
router.message.middleware(DialogGateMiddleware())


@router.message(F.photo)
async def on_photo(
    message: TgMessage, bot: Bot, session: AsyncSession, user: User, dialog: DialogService
) -> None:
    # Самый крупный размер (Telegram сам ужимает до ~1280px — для vision достаточно).
    image = await bot.download(message.photo[-1])
    await respond(
        message, bot, session, user, dialog, kind="photo", text=message.caption, image=image.read()
    )
