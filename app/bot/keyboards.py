from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.db.models import User
from app.personas import PERSONAS, get_persona

LEVEL_CODES = ("A1", "A2", "B1", "B2", "C1")


class TtsCb(CallbackData, prefix="tts"):
    message_id: int


class LevelCb(CallbackData, prefix="lv"):
    level: str
    src: str  # w — кнопки приветствия, s — экран настроек


class PersonaCb(CallbackData, prefix="pr"):
    key: str


class ModeCb(CallbackData, prefix="mode"):
    mode: str


class VoiceModeCb(CallbackData, prefix="vm"):
    mode: str
    src: str  # s — экран настроек, o — предложение после нажатий 🔊


def webapp_kb(url: str, text: str, page: str | None = None) -> InlineKeyboardMarkup:
    # Раздел передаём через ?page=: hash Telegram занимает своими параметрами запуска.
    target = f"{url.rstrip('/')}/" + (f"?page={page}" if page else "")
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=target))]]
    )


def tts_kb(message_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.TTS_BUTTON, callback_data=TtsCb(message_id=message_id))
    return builder.as_markup()


def quick_level_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for level, label in texts.QUICK_LEVELS.items():
        builder.button(text=label, callback_data=LevelCb(level=level, src="w"))
    builder.adjust(2)
    return builder.as_markup()


def _mark(selected: bool, label: str) -> str:
    return f"✓ {label}" if selected else label


def settings_kb(user: User) -> InlineKeyboardMarkup:
    """Все настройки на одном экране: каждый ряд — одна настройка, выбранное отмечено ✓."""
    builder = InlineKeyboardBuilder()
    for mode, label in texts.VOICE_BUTTONS.items():
        builder.button(
            text=_mark(user.voice_mode == mode, label),
            callback_data=VoiceModeCb(mode=mode, src="s"),
        )
    current = get_persona(user.persona).key
    for persona in PERSONAS.values():
        builder.button(
            text=_mark(persona.key == current, f"{persona.flag} {persona.name}"),
            callback_data=PersonaCb(key=persona.key),
        )
    for level in LEVEL_CODES:
        builder.button(
            text=_mark(user.level == level, level), callback_data=LevelCb(level=level, src="s")
        )
    for mode, label in texts.MODE_BUTTONS.items():
        builder.button(text=_mark(user.mode == mode, label), callback_data=ModeCb(mode=mode))
    builder.adjust(
        len(texts.VOICE_BUTTONS), len(PERSONAS), len(LEVEL_CODES), len(texts.MODE_BUTTONS)
    )
    return builder.as_markup()


def voice_offer_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.VOICE_OFFER_YES, callback_data=VoiceModeCb(mode="always", src="o"))
    builder.button(text=texts.VOICE_OFFER_AUTO, callback_data=VoiceModeCb(mode="auto", src="o"))
    builder.adjust(1)
    return builder.as_markup()
