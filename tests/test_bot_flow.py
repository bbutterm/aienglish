"""Сквозной сценарий: апдейты Telegram → диспетчер → сервисы → Postgres, с фейковыми API."""

import itertools
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetFile,
    SendMessage,
    SendVoice,
)
from aiogram.types import CallbackQuery, Chat, PhotoSize, Update, Voice
from aiogram.types import Message as TgMessage
from aiogram.types import User as TgUser
from sqlalchemy import func, select

from app.bot import texts
from app.bot.keyboards import LevelCb, ModeCb, PersonaCb, TtsCb, VoiceModeCb
from app.db.models import Message, Mistake, Usage, User, Vocab
from app.main import build_dialog, build_dispatcher
from app.services.dialog import BackgroundTasks
from tests.conftest import FakeTelegramSession

USER_ID = 555
_ids = itertools.count(1)


class Harness:
    def __init__(self, dp, bot: Bot, tg: FakeTelegramSession, dialog, session_factory) -> None:
        self.dp = dp
        self.bot = bot
        self.tg = tg
        self.dialog = dialog
        self.session_factory = session_factory
        self.tg_user = TgUser(id=USER_ID, is_bot=False, first_name="Alex", username="alex")
        self.chat = Chat(id=USER_ID, type="private")

    def _message(self, **fields: Any) -> TgMessage:
        return TgMessage(
            message_id=next(_ids),
            date=datetime.now(UTC),
            chat=self.chat,
            from_user=self.tg_user,
            **fields,
        )

    async def feed(self, **message_fields: Any) -> None:
        self.tg.requests.clear()
        update = Update(update_id=next(_ids), message=self._message(**message_fields))
        await self.dp.feed_update(self.bot, update)
        await self.dialog.tasks.drain()

    async def click(self, data: str) -> None:
        self.tg.requests.clear()
        callback = CallbackQuery(
            id=str(next(_ids)),
            from_user=self.tg_user,
            chat_instance="ci",
            data=data,
            message=self._message(text="menu"),
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(_ids), callback_query=callback))
        await self.dialog.tasks.drain()

    def sent_texts(self) -> list[str]:
        return [m.text for m in self.tg.of_type(SendMessage)]

    async def user(self) -> User:
        async with self.session_factory() as session:
            return await session.get(User, USER_ID)

    async def count(self, model, *where) -> int:
        async with self.session_factory() as session:
            return await session.scalar(select(func.count(model.id)).where(*where))


_dispatcher: Dispatcher | None = None


def get_dispatcher(settings, session_factory, dialog) -> Dispatcher:
    """Роутеры aiogram цепляются к родителю один раз за процесс — переиспользуем диспетчер."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = build_dispatcher(settings, session_factory, dialog)
    else:
        _dispatcher["settings"] = settings
        _dispatcher["dialog"] = dialog
        _dispatcher.fsm.storage = MemoryStorage()
    return _dispatcher


@pytest.fixture
async def harness(settings, session_factory, http) -> AsyncGenerator[Harness]:
    tasks = BackgroundTasks()
    dialog = build_dialog(settings, session_factory, http, tasks)
    dp = get_dispatcher(settings, session_factory, dialog)
    tg = FakeTelegramSession()
    bot = Bot("123456:TEST", session=tg, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    yield Harness(dp, bot, tg, dialog, session_factory)
    await tasks.drain()


async def onboard(h: Harness) -> None:
    await h.feed(text="/start")


async def test_quick_start(harness: Harness, fake_apis) -> None:
    """/start без анкеты: приветствие и сразу реплика собеседника, уровень — по желанию."""
    h = harness
    await h.feed(text="/start")
    welcome, opener = h.tg.of_type(SendMessage)
    assert welcome.text.startswith("👋 Привет, Alex! Я Jake 🇺🇸")
    assert "Уроки" not in welcome.text  # Mini App не настроен
    levels = [b.text for row in welcome.reply_markup.inline_keyboard for b in row]
    assert levels == list(texts.QUICK_LEVELS.values())
    assert opener.text == fake_apis.companion_reply
    assert opener.reply_markup.inline_keyboard[0][0].text == texts.TTS_BUTTON
    user = await h.user()
    assert (user.onboarded, user.level, user.persona, user.voice_mode) == (
        True,
        "B1",
        "us_casual",
        "auto",
    )
    assert user.name == "Alex"  # имя из Telegram, без вопроса
    assert "Alex just joined" in fake_apis.companion_requests()[-1]["messages"][-1]["content"]

    # Уровень — одной кнопкой; кнопки уровня после выбора убираются.
    await h.click(LevelCb(level="A2", src="w").pack())
    assert (await h.user()).level == "A2"
    assert h.tg.of_type(AnswerCallbackQuery)[0].text == texts.LEVEL_SET.format(level="A2")
    assert len(h.tg.of_type(EditMessageReplyMarkup)) == 1

    # Повторный /start — экран настроек, а не анкета.
    await h.feed(text="/start")
    assert h.sent_texts()[0].startswith(texts.SETTINGS_BACK)


async def test_text_without_start_gets_welcome_and_reply(harness: Harness, fake_apis) -> None:
    await harness.feed(text="Hello!")
    welcome, reply = harness.sent_texts()
    assert welcome.startswith("👋 Привет, Alex!")
    assert reply == fake_apis.companion_reply
    assert (await harness.user()).onboarded is True


async def test_text_dialog_with_analyst_and_mistakes(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    await h.feed(text="I go to cinema yesterday")

    reply = h.tg.of_type(SendMessage)[0]
    assert reply.text == fake_apis.companion_reply
    companion_request = fake_apis.companion_requests()[-1]
    assert companion_request["messages"][-1] == {
        "role": "user",
        "content": "I go to cinema yesterday",
    }
    # История: приветствие собеседника идёт в контекст перед репликой пользователя.
    assert companion_request["messages"][-2]["role"] == "assistant"

    # Аналитик отработал в фоне: ошибка, слово, оценка уровня, учёт расходов.
    assert await h.count(Mistake) == 1
    assert await h.count(Vocab) == 1
    assert (await h.user()).estimated_level == "A2"
    # тема реплики (для урока и прогресса) нормализована: «Кино.» → «кино»
    async with h.session_factory() as session:
        topics = list(await session.scalars(select(Message.topic).order_by(Message.id)))
    assert topics == [None, "кино", None]  # приветствие, реплика, ответ собеседника
    assert await h.count(Usage, Usage.kind == "llm_analyst") == 1
    assert await h.count(Usage, Usage.kind == "llm_companion") == 2

    await h.feed(text="/mistakes")
    report = h.sent_texts()[0]
    assert "I went to the cinema yesterday" in report
    assert "Прошедшее время" in report


async def test_tts_button_caches_file_id_and_offers_auto_voice(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    await h.feed(text="Hi there")
    async with h.session_factory() as session:
        reply_id = await session.scalar(select(func.max(Message.id)))

    await h.click(TtsCb(message_id=reply_id).pack())
    first = h.tg.of_type(SendVoice)
    assert len(first) == 1 and first[0].caption is None  # текст уже в чате
    assert len(fake_apis.tts_requests) == 1
    assert fake_apis.tts_requests[0]["path"].endswith("/bIHbv24MWmeRgasZH58o")  # голос Джейка
    assert h.tg.of_type(SendMessage) == []

    await h.click(TtsCb(message_id=reply_id).pack())
    second = h.tg.of_type(SendVoice)
    assert second[0].voice.startswith("voice-")  # отправлен file_id из кэша
    assert len(fake_apis.tts_requests) == 1
    assert await h.count(Usage, Usage.kind == "tts") == 1
    # Второе нажатие за день — предложение озвучивать без кнопки.
    offer = h.tg.of_type(SendMessage)[0]
    assert offer.text == texts.VOICE_OFFER
    await h.click(VoiceModeCb(mode="always", src="o").pack())
    assert (await h.user()).voice_mode == "always"
    done = h.tg.of_type(EditMessageText)[0]
    assert done.text == texts.VOICE_OFFER_DONE.format(mode=texts.VOICE_MODES["always"])

    # Больше не предлагаем.
    await h.click(TtsCb(message_id=reply_id).pack())
    assert h.tg.of_type(SendMessage) == []


async def test_tts_limit(harness: Harness, settings, fake_apis) -> None:
    h = harness
    settings.daily_tts_limit = 0
    await onboard(h)
    await h.feed(text="Hi there")
    async with h.session_factory() as session:
        reply_id = await session.scalar(select(func.max(Message.id)))
    await h.click(TtsCb(message_id=reply_id).pack())
    answer = h.tg.of_type(AnswerCallbackQuery)[0]
    assert (answer.text, answer.show_alert) == (texts.TTS_LIMIT, True)
    assert h.tg.of_type(SendVoice) == []

    # Голосовое при исчерпанном лимите: ответ текстом с цитатой, плюс разовое уведомление.
    await h.feed(voice=Voice(file_id="v", file_unique_id="u", duration=3))
    notice, reply = h.sent_texts()
    assert notice.startswith("🔇")
    assert reply.startswith("<blockquote>🗣 I go to cinema yesterday</blockquote>")
    assert h.tg.of_type(SendVoice) == []


async def test_voice_message_gets_single_voice_reply(harness: Harness, fake_apis) -> None:
    """Режим по умолчанию: на голосовое — одно голосовое с текстом и транскриптом в подписи."""
    h = harness
    await onboard(h)
    await h.feed(voice=Voice(file_id="tg-voice", file_unique_id="u1", duration=4))

    assert len(h.tg.of_type(GetFile)) == 1
    assert len(fake_apis.stt_requests) == 1
    assert h.tg.of_type(SendMessage) == []  # ни транскрипта, ни текста отдельными сообщениями
    (voice,) = h.tg.of_type(SendVoice)
    assert voice.caption == (
        f"<blockquote>🗣 {fake_apis.stt_text}</blockquote>\n{fake_apis.companion_reply}"
    )
    async with h.session_factory() as session:
        user_msg = await session.scalar(
            select(Message).where(Message.role == "user").order_by(Message.id.desc())
        )
        reply = await session.scalar(select(Message).order_by(Message.id.desc()))
        stt = await session.scalar(select(Usage).where(Usage.kind == "stt"))
    assert (user_msg.kind, user_msg.text) == ("voice", fake_apis.stt_text)
    assert reply.tts_file_id is not None and reply.tg_message_id is not None
    assert stt.audio_seconds == 4
    assert await h.count(Mistake) == 1  # аналитик разобрал транскрипт

    # На текст в том же режиме — текст с кнопкой 🔊.
    await h.feed(text="Nice")
    assert h.sent_texts() == [fake_apis.companion_reply]
    assert h.tg.of_type(SendVoice) == []


async def test_voice_modes_from_settings(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    await h.feed(text="/settings")
    menu = h.tg.of_type(SendMessage)[0]
    assert "голосом на голосовые" in menu.text
    labels = [b.text for row in menu.reply_markup.inline_keyboard for b in row]
    assert "✓ 🎙 На голосовые" in labels and "✓ 🇺🇸 Jake" in labels and "✓ B1" in labels

    await h.click(VoiceModeCb(mode="always", src="s").pack())
    assert (await h.user()).voice_mode == "always"
    assert "все ответы голосом" in h.tg.of_type(EditMessageText)[0].text
    await h.feed(text="Hello")
    assert h.tg.of_type(SendMessage) == []
    assert h.tg.of_type(SendVoice)[0].caption == fake_apis.companion_reply

    await h.click(VoiceModeCb(mode="off", src="s").pack())
    await h.feed(voice=Voice(file_id="v2", file_unique_id="u2", duration=2))
    assert h.tg.of_type(SendVoice) == []
    reply = h.tg.of_type(SendMessage)[0]
    assert reply.text.startswith("<blockquote>🗣 ")
    assert reply.reply_markup.inline_keyboard[0][0].text == texts.TTS_BUTTON


async def test_settings_change_persona_level_and_mode(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    await h.click(PersonaCb(key="uk_casual").pack())
    await h.click(LevelCb(level="C1", src="s").pack())
    await h.click(ModeCb(mode="correct").pack())
    user = await h.user()
    assert (user.persona, user.level, user.mode) == ("uk_casual", "C1", "correct")
    await h.feed(text="Hello")
    system = fake_apis.companion_requests()[-1]["messages"][0]["content"]
    assert "You are Emma" in system and "level ~C1" in system and "✏️" in system


async def test_stale_buttons_answered(harness: Harness) -> None:
    await harness.click("ob:use_name")  # кнопка из старой анкеты
    answer = harness.tg.of_type(AnswerCallbackQuery)[0]
    assert (answer.text, answer.show_alert) == (texts.STALE_MENU, True)


async def test_voice_too_long(harness: Harness, fake_apis, settings) -> None:
    h = harness
    await onboard(h)
    await h.feed(
        voice=Voice(file_id="v", file_unique_id="u", duration=settings.max_voice_seconds + 1)
    )
    assert h.sent_texts() == [texts.VOICE_TOO_LONG.format(limit=settings.max_voice_seconds)]
    assert fake_apis.stt_requests == []


async def test_photo_message(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    await h.feed(
        photo=[
            PhotoSize(file_id="small", file_unique_id="s", width=90, height=90),
            PhotoSize(file_id="big", file_unique_id="b", width=1280, height=960),
        ],
        caption="My weekend",
    )
    assert h.tg.of_type(GetFile)[0].file_id == "big"
    assert h.sent_texts() == ["What a happy dog! Is it yours?"]
    last = fake_apis.companion_requests()[-1]["messages"][-1]
    assert last["content"][0] == {"type": "text", "text": "My weekend"}
    async with h.session_factory() as session:
        photo_msg = await session.scalar(select(Message).where(Message.kind == "photo"))
    assert photo_msg.photo_caption == "a dog running on a beach"

    # В следующем запросе фото уже только текстовым описанием.
    await h.feed(text="Yes, it's mine")
    history = fake_apis.companion_requests()[-1]["messages"]
    assert {"role": "user", "content": "[photo: a dog running on a beach]\nMy weekend"} in history


async def test_daily_limit(harness: Harness, fake_apis, settings) -> None:
    h = harness
    settings.daily_msg_limit = 1
    await onboard(h)
    await h.feed(text="first")
    await h.feed(text="second")
    assert h.sent_texts() == [texts.LIMIT_REACHED.format(limit=1)]


async def test_review_mode_auto_review(harness: Harness, fake_apis, settings) -> None:
    h = harness
    settings.review_every = 2
    await onboard(h)
    await h.click(ModeCb(mode="review").pack())
    assert (await h.user()).mode == "review"
    h.tg.requests.clear()
    companion_prompt_check = None

    await h.feed(text="I go to cinema yesterday")
    assert h.sent_texts() == [fake_apis.companion_reply]
    companion_prompt_check = fake_apis.companion_requests()[-1]["messages"][0]["content"]
    assert "never mention or correct" in companion_prompt_check

    await h.feed(text="It was great")
    sent = h.sent_texts()
    assert sent[0] == fake_apis.companion_reply
    assert sent[1].startswith("📝 <b>Разбор сессии</b>")
    assert "I went to the cinema yesterday" in sent[1]

    await h.feed(text="/review")
    assert h.sent_texts() == [texts.REVIEW_EMPTY]


async def test_reset_starts_new_topic(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    await h.feed(text="Hi")
    await h.feed(text="/reset")
    assert h.sent_texts() == [texts.RESET_DONE, fake_apis.companion_reply]
    request = fake_apis.companion_requests()[-1]["messages"]
    # Старая история отброшена: только system + служебная заметка про новую тему.
    assert [m["role"] for m in request] == ["system", "user"]
    assert "wants to talk about something new" in request[-1]["content"]


async def test_stats_admin_only(harness: Harness, settings) -> None:
    h = harness
    await h.feed(text="/stats")
    assert h.sent_texts() == [texts.ADMIN_ONLY]
    settings.admin_ids = [USER_ID]
    await h.feed(text="/stats")
    assert h.sent_texts()[0].startswith("📊 <b>Статистика</b>")


async def test_unknown_command_and_unsupported_content(harness: Harness) -> None:
    h = harness
    await h.feed(text="/whatever")
    assert h.sent_texts() == [texts.UNKNOWN_COMMAND]
    await onboard(h)
    await h.feed(sticker=None, location={"latitude": 1.0, "longitude": 2.0})
    assert h.sent_texts() == [texts.UNSUPPORTED]


async def test_app_and_admin_commands(harness: Harness, settings) -> None:
    h = harness
    await h.feed(text="/app")
    assert h.sent_texts() == [texts.APP_NOT_CONFIGURED]

    settings.webapp_url = "https://buddy.example"
    await h.feed(text="/app")
    button = h.tg.of_type(SendMessage)[0].reply_markup.inline_keyboard[0][0]
    assert button.web_app.url == "https://buddy.example/"

    await h.feed(text="/admin")
    assert h.sent_texts() == [texts.ADMIN_ONLY]
    settings.admin_ids = [USER_ID]
    await h.feed(text="/admin")
    button = h.tg.of_type(SendMessage)[0].reply_markup.inline_keyboard[0][0]
    assert button.web_app.url == "https://buddy.example/?page=admin"


async def test_welcome_mentions_mini_app(harness: Harness, settings) -> None:
    settings.webapp_url = "https://buddy.example"
    await onboard(harness)
    assert texts.WELCOME_APP.strip() in harness.tg.of_type(SendMessage)[0].text


async def test_blocked_user_is_ignored(harness: Harness) -> None:
    h = harness
    await onboard(h)
    async with h.session_factory() as session:
        user = await session.get(User, USER_ID)
        user.blocked = True
        await session.commit()
    await h.feed(text="Hello?")
    await h.feed(text="/help")
    assert h.tg.requests == []


async def test_premium_raises_daily_limit(harness: Harness, settings, fake_apis) -> None:
    h = harness
    settings.daily_msg_limit = 1
    settings.premium_daily_msg_limit = 5
    await onboard(h)
    async with h.session_factory() as session:
        user = await session.get(User, USER_ID)
        user.premium_until = datetime.now(UTC) + timedelta(days=3)
        await session.commit()
    await h.feed(text="first")
    await h.feed(text="second")
    assert h.sent_texts() == [fake_apis.companion_reply]
    await h.feed(text="/help")
    assert "⭐ Премиум активен до" in h.sent_texts()[0]
    assert "5 реплик" in h.sent_texts()[0]
