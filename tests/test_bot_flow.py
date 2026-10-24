"""Сквозной сценарий: апдейты Telegram → диспетчер → сервисы → Postgres, с фейковыми API."""

import itertools
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
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
from app.bot.keyboards import LevelCb, ModeCb, PersonaCb, TtsCb, VoiceCb
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
    await h.feed(text="Sasha")
    await h.click(LevelCb(level="A2").pack())
    await h.click(PersonaCb(key="uk_casual", onboarding=True).pack())


async def test_onboarding_flow(harness: Harness, fake_apis) -> None:
    h = harness
    await h.feed(text="/start")
    assert h.sent_texts() == [texts.WELCOME]
    welcome = h.tg.of_type(SendMessage)[0]
    assert welcome.reply_markup.inline_keyboard[0][0].text == "Зови меня Alex"

    await h.feed(text="Sasha")
    assert h.sent_texts() == [texts.ASK_LEVEL.format(name="Sasha")]

    await h.click(LevelCb(level="A2").pack())
    assert h.tg.of_type(EditMessageText)[0].text == texts.LEVEL_CHOSEN.format(
        level=texts.LEVELS["A2"]
    )
    assert h.sent_texts() == [texts.ASK_PERSONA]

    await h.click(PersonaCb(key="uk_casual", onboarding=True).pack())
    sent = h.tg.of_type(SendMessage)
    assert sent[0].text == texts.ONBOARDING_DONE.format(name="Emma")
    # Собеседник сам начинает разговор, под ответом — кнопка озвучки.
    assert sent[1].text == fake_apis.companion_reply
    assert sent[1].reply_markup.inline_keyboard[0][0].text == texts.TTS_BUTTON

    user = await h.user()
    assert (user.display_name, user.level, user.persona, user.onboarded) == (
        "Sasha",
        "A2",
        "uk_casual",
        True,
    )
    opener_request = fake_apis.companion_requests()[-1]
    assert "You are Emma" in opener_request["messages"][0]["content"]
    assert "Sasha just joined" in opener_request["messages"][-1]["content"]


async def test_text_before_onboarding_starts_onboarding(harness: Harness, fake_apis) -> None:
    await harness.feed(text="Hello!")
    assert harness.sent_texts() == [texts.NEED_ONBOARDING, texts.WELCOME]
    assert fake_apis.llm_requests == []


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
    assert await h.count(Usage, Usage.kind == "llm_analyst") == 1
    assert await h.count(Usage, Usage.kind == "llm_companion") == 2

    await h.feed(text="/mistakes")
    report = h.sent_texts()[0]
    assert "I went to the cinema yesterday" in report
    assert "Прошедшее время" in report


async def test_tts_button_caches_file_id(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    await h.feed(text="Hi there")
    async with h.session_factory() as session:
        reply_id = await session.scalar(select(func.max(Message.id)))

    await h.click(TtsCb(message_id=reply_id).pack())
    first = h.tg.of_type(SendVoice)
    assert len(first) == 1 and len(fake_apis.tts_requests) == 1
    assert fake_apis.tts_requests[0]["path"].endswith("/Xb7hH8MSUJpSbSDYk0k2")  # голос Эммы

    await h.click(TtsCb(message_id=reply_id).pack())
    second = h.tg.of_type(SendVoice)
    assert second[0].voice.startswith("voice-")  # отправлен file_id из кэша
    assert len(fake_apis.tts_requests) == 1
    assert await h.count(Usage, Usage.kind == "tts") == 1


async def test_tts_limit(harness: Harness, settings) -> None:
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


async def test_voice_always_mode(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    await h.click(VoiceCb(action="toggle").pack())
    assert (await h.user()).voice_always is True

    await h.feed(text="Hello")
    text_reply = h.tg.of_type(SendMessage)[0]
    assert text_reply.reply_markup is None
    assert len(h.tg.of_type(SendVoice)) == 1


async def test_voice_message(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    await h.feed(voice=Voice(file_id="tg-voice", file_unique_id="u1", duration=4))

    assert len(h.tg.of_type(GetFile)) == 1
    assert len(fake_apis.stt_requests) == 1
    sent = h.sent_texts()
    assert sent[0] == texts.TRANSCRIPT.format(text=fake_apis.stt_text)
    assert sent[1] == fake_apis.companion_reply
    async with h.session_factory() as session:
        user_msg = await session.scalar(
            select(Message).where(Message.role == "user").order_by(Message.id.desc())
        )
        stt = await session.scalar(select(Usage).where(Usage.kind == "stt"))
    assert (user_msg.kind, user_msg.text) == ("voice", fake_apis.stt_text)
    assert stt.audio_seconds == 4
    assert await h.count(Mistake) == 1  # аналитик разобрал транскрипт


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
