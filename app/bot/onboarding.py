from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message as TgMessage

from app.bot import texts
from app.bot.keyboards import name_kb
from app.db.models import User


class Onboarding(StatesGroup):
    name = State()
    level = State()
    persona = State()


async def start_onboarding(message: TgMessage, state: FSMContext, user: User) -> None:
    await state.set_state(Onboarding.name)
    await message.answer(texts.WELCOME, reply_markup=name_kb(user.first_name))
