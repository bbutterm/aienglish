from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.db.models import User
from app.personas import PERSONAS, get_persona

LEVEL_CODES = ("A1", "A2", "B1", "B2", "C1")


class TtsCb(CallbackData, prefix="tts"):
    message_id: int


class RewriteCb(CallbackData, prefix="rw"):
    message_id: int  # ответ собеседника; переписываем реплику пользователя перед ним


class PingCb(CallbackData, prefix="ping"):
    on: bool


class LevelCb(CallbackData, prefix="lv"):
    level: str
    src: str  # w — кнопки приветствия, s — экран настроек, j — подсказка «проверка уровня»


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
    return reply_kb(message_id, tts=True, rewrite=False)


def reply_kb(message_id: int, *, tts: bool, rewrite: bool) -> InlineKeyboardMarkup | None:
    """Кнопки под ответом собеседника: 🔊 (если он текстом) и «Как сказать лучше»."""
    builder = InlineKeyboardBuilder()
    if tts:
        builder.button(text=texts.TTS_BUTTON, callback_data=TtsCb(message_id=message_id))
    if rewrite:
        builder.button(text=texts.REWRITE_BUTTON, callback_data=RewriteCb(message_id=message_id))
    if not tts and not rewrite:
        return None
    return builder.as_markup()


def level_hint_kb(estimated: str, current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=texts.HINT_LEVEL_YES.format(level=estimated),
        callback_data=LevelCb(level=estimated, src="j"),
    )
    builder.button(
        text=texts.HINT_LEVEL_NO.format(level=current),
        callback_data=LevelCb(level=current, src="j"),
    )
    builder.adjust(1)
    return builder.as_markup()


def quick_level_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for level, label in texts.QUICK_LEVELS.items():
        builder.button(text=label, callback_data=LevelCb(level=level, src="w"))
    builder.adjust(2)
    return builder.as_markup()


def _mark(selected: bool, label: str) -> str:
    return f"✓ {label}" if selected else label


def settings_kb(user: User, ping: bool = False) -> InlineKeyboardMarkup:
    """Все настройки на одном экране: каждый ряд — одна настройка, выбранное отмечено ✓.

    ping — показывать ли ряд «Друг пишет первым» (если фича включена).
    """
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
    rows = [len(texts.VOICE_BUTTONS), len(PERSONAS), len(LEVEL_CODES), len(texts.MODE_BUTTONS)]
    if ping:
        for on, label in texts.PING_BUTTONS.items():
            builder.button(text=_mark(user.ping_enabled == on, label), callback_data=PingCb(on=on))
        rows.append(len(texts.PING_BUTTONS))
    builder.adjust(*rows)
    return builder.as_markup()


def voice_offer_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.VOICE_OFFER_YES, callback_data=VoiceModeCb(mode="always", src="o"))
    builder.button(text=texts.VOICE_OFFER_AUTO, callback_data=VoiceModeCb(mode="auto", src="o"))
    builder.adjust(1)
    return builder.as_markup()
