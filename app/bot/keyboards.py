from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.db.models import User
from app.personas import PERSONAS, get_persona


class TtsCb(CallbackData, prefix="tts"):
    message_id: int


class OnboardCb(CallbackData, prefix="ob"):
    action: str  # use_name | restart


class LevelCb(CallbackData, prefix="lvl"):
    level: str


class PersonaCb(CallbackData, prefix="per"):
    key: str
    onboarding: bool


class ModeCb(CallbackData, prefix="mode"):
    mode: str


class VoiceCb(CallbackData, prefix="voice"):
    action: str  # toggle


def tts_kb(message_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.TTS_BUTTON, callback_data=TtsCb(message_id=message_id))
    return builder.as_markup()


def name_kb(first_name: str | None) -> InlineKeyboardMarkup | None:
    if not first_name:
        return None
    builder = InlineKeyboardBuilder()
    builder.button(text=f"Зови меня {first_name[:32]}", callback_data=OnboardCb(action="use_name"))
    return builder.as_markup()


def restart_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.RESTART_BUTTON, callback_data=OnboardCb(action="restart"))
    return builder.as_markup()


def levels_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for level, label in texts.LEVELS.items():
        builder.button(text=label, callback_data=LevelCb(level=level))
    builder.adjust(1)
    return builder.as_markup()


def personas_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for persona in PERSONAS.values():
        builder.button(
            text=f"{persona.flag} {persona.label_ru}",
            callback_data=PersonaCb(key=persona.key, onboarding=True),
        )
    builder.adjust(1)
    return builder.as_markup()


def modes_kb(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for mode, label in texts.MODES.items():
        mark = "✅ " if mode == current else ""
        builder.button(text=f"{mark}{label}", callback_data=ModeCb(mode=mode))
    builder.adjust(1)
    return builder.as_markup()


def voice_kb(user: User) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=texts.VOICE_ALWAYS_ON if user.voice_always else texts.VOICE_ALWAYS_OFF,
        callback_data=VoiceCb(action="toggle"),
    )
    current = get_persona(user.persona).key
    for persona in PERSONAS.values():
        mark = "✅ " if persona.key == current else ""
        builder.button(
            text=f"{mark}{persona.flag} {persona.name}",
            callback_data=PersonaCb(key=persona.key, onboarding=False),
        )
    builder.adjust(1, len(PERSONAS))
    return builder.as_markup()
