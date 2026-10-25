"""Включаемые фичи в боте: прокачка слов, «как сказать лучше», подсказки первых дней,
приглашения, миссии, «друг пишет первым», разбор пересланного."""

from datetime import UTC, datetime, timedelta

from aiogram.methods import SendMessage
from aiogram.types import MessageOriginUser
from aiogram.types import User as TgUser
from sqlalchemy import select, update

from app.bot import texts
from app.bot.after_turn import start_mission
from app.bot.keyboards import LevelCb, PingCb, RewriteCb
from app.db import repo
from app.db.models import Message, TopicIdea, User, Vocab
from app.services.pings import PingScheduler
from tests.conftest import FAKE_MISSION
from tests.test_bot_flow import USER_ID, Harness, harness, onboard


def buttons(message: SendMessage) -> list[str]:
    markup = message.reply_markup
    return [b.text for row in markup.inline_keyboard for b in row] if markup else []


async def chat(h: Harness, *lines: str) -> None:
    for line in lines:
        await h.feed(text=line)


# --- прокачка слов ---


async def test_word_upgrades_make_cards(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    await h.feed(text="My monitor is very big")
    analyst = [
        r for r in fake_apis.llm_requests if "You analyse ONE" in r["messages"][0]["content"]
    ]
    assert '"upgrades"' in analyst[-1]["messages"][0]["content"]
    async with h.session_factory() as session:
        card = await session.scalar(select(Vocab).where(Vocab.source == "upgrade"))
    assert (card.term, card.note, card.example) == ("huge", "very big", "My new monitor is huge.")


async def test_word_upgrades_off(harness: Harness, fake_apis) -> None:
    h = harness
    await h.dialog.features.set("word_upgrades", False)
    await onboard(h)
    await h.feed(text="My monitor is very big")
    analyst = [
        r for r in fake_apis.llm_requests if "You analyse ONE" in r["messages"][0]["content"]
    ]
    assert '"upgrades"' not in analyst[-1]["messages"][0]["content"]
    assert await h.count(Vocab, Vocab.source == "upgrade") == 0


# --- скажи как носитель ---


async def test_native_rewrite(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    await h.feed(text="I am working here since three years")
    reply = h.tg.of_type(SendMessage)[0]
    assert buttons(reply) == [texts.TTS_BUTTON, texts.REWRITE_BUTTON]
    async with h.session_factory() as session:
        reply_id = await session.scalar(
            select(Message.id).where(Message.role == "assistant").order_by(Message.id.desc())
        )
    await h.click(RewriteCb(message_id=reply_id).pack())
    (result,) = h.sent_texts()
    assert "<i>I am working here since three years</i>" in result
    assert "<b>I&#x27;ve been working here for three years.</b>" in result
    assert "<s>since</s> → <b>for</b> — с длительностью — for" in result
    sent = h.tg.of_type(SendMessage)[0]
    assert sent.reply_to_message_id is not None  # ответом на реплику пользователя
    assert await h.count(Message) == 3  # приветствие собеседника, реплика, ответ — без новых


async def test_native_rewrite_off_hides_button(harness: Harness) -> None:
    h = harness
    await h.dialog.features.set("native_rewrite", False)
    await onboard(h)
    await h.feed(text="Hello there")
    assert buttons(h.tg.of_type(SendMessage)[0]) == [texts.TTS_BUTTON]


# --- подсказки первых дней ---


async def test_first_steps_hints(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    await chat(h, "Hi", "I like films")
    assert len(h.sent_texts()) == 1  # пока только ответ собеседника
    await h.feed(text="I go to cinema yesterday")
    reply, insight = h.sent_texts()
    assert insight.startswith("Кстати, вот что я заметил")
    assert "<s>I go to cinema yesterday</s> → <b>I went to the cinema yesterday</b>" in insight

    await chat(h, "ok", "nice")
    await h.feed(text="cool")  # 6-я реплика: аналитик считает A2, а выбран B1
    _, level_hint = h.tg.of_type(SendMessage)
    assert "ближе к <b>A2</b>" in level_hint.text
    assert buttons(level_hint) == [
        texts.HINT_LEVEL_YES.format(level="A2"),
        texts.HINT_LEVEL_NO.format(level="B1"),
    ]
    await h.click(LevelCb(level="A2", src="j").pack())
    assert (await h.user()).level == "A2"

    # Mini App не настроен — приглашения в урок нет; на 15-й реплике — настройки.
    for i in range(7, 15):
        await h.feed(text=f"message {i}")
        assert len(h.sent_texts()) == 1
    await h.feed(text="message 15")
    _, tip = h.tg.of_type(SendMessage)
    assert tip.text == texts.HINT_SETTINGS and tip.reply_markup is not None
    assert set((await h.user()).hints) == {"first_insight", "level_check", "settings_tip"}


# --- источники и приглашения ---


async def test_start_source_and_referral_reward(harness: Harness, session_factory) -> None:
    h = harness
    async with session_factory() as session:
        friend = await repo.upsert_user(session, 777, "friend", "Kate")
        friend.onboarded = True
        await session.commit()
    await h.feed(text="/start ref_777")
    welcome = h.sent_texts()[0]
    assert welcome.startswith(texts.REFERRAL_WELCOME)
    user = await h.user()
    assert (user.source, user.referred_by, user.referral_rewarded) == ("ref", 777, False)

    await chat(h, "one", "two", "three", "four")
    assert (await h.user()).premium_until is None
    await h.feed(text="five")
    user = await h.user()
    assert user.referral_rewarded and user.premium_until > datetime.now(UTC) + timedelta(days=6)
    async with session_factory() as session:
        friend = await session.get(User, 777)
    assert friend.premium_until > datetime.now(UTC) + timedelta(days=6)
    rewards = [m for m in h.tg.of_type(SendMessage) if "🎁" in m.text]
    assert {m.chat_id for m in rewards} == {USER_ID, 777}


async def test_start_campaign_source(harness: Harness) -> None:
    h = harness
    await h.feed(text="/start tgads_nov")
    assert (await h.user()).source == "tgads_nov"
    await h.feed(text="/start other")  # повторный /start источник не перезаписывает
    assert (await h.user()).source == "tgads_nov"


# --- миссии ---


async def test_mission_flow(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    async with h.session_factory() as session:
        user = await session.get(User, USER_ID)
        idea = TopicIdea(
            user_id=USER_ID, kind="mission", title_ru="Верни ноутбук", content=FAKE_MISSION
        )
        session.add(idea)
        await session.commit()
        h.tg.requests.clear()
        await start_mission(h.bot, session, user, h.dialog, idea)
    intro, opener = h.sent_texts()
    assert intro.startswith("🎭 <b>Миссия: Верни ноутбук</b>")
    assert "1. Объясни, что не так" in intro
    note = fake_apis.companion_requests()[-1]["messages"]
    assert "role-play scene starts now" in note[-1]["content"]
    assert "ROLE-PLAY SCENE" in note[0]["content"] and "shop assistant" in note[0]["content"]

    await h.feed(text="Hi, my laptop is broken and I want a refund")
    reply, goal1, goal2 = h.sent_texts()
    assert goal1 == "🎯 1/3: Объясни, что не так"
    assert goal2 == "🎯 2/3: Попроси вернуть деньги"
    analyst = [
        r for r in fake_apis.llm_requests if "You analyse ONE" in r["messages"][0]["content"]
    ]
    assert "goals_done" in analyst[-1]["messages"][0]["content"]

    await h.feed(text="No thanks, I don't need a store credit")
    reply, done = h.sent_texts()
    assert done.startswith("🏁 <b>Миссия выполнена: Верни ноутбук</b>")
    assert "Реплик: 2" in done
    assert (await h.user()).active_mission is None
    async with h.session_factory() as session:
        assert (await session.get(TopicIdea, idea.id)).status == "done"


async def test_mission_stop(harness: Harness) -> None:
    h = harness
    await onboard(h)
    await h.feed(text="/stop")
    assert h.sent_texts() == [texts.NO_MISSION]
    async with h.session_factory() as session:
        await session.execute(
            update(User)
            .where(User.id == USER_ID)
            .values(
                active_mission={
                    "title_ru": "Сцена",
                    "goals": [{"en": "a", "ru": "а"}] * 3,
                    "done": [0],
                    "started_at": datetime.now(UTC).isoformat(),
                }
            )
        )
        await session.commit()
    await h.feed(text="/stop")
    assert h.sent_texts() == [texts.MISSION_STOPPED.format(title="Сцена", done=1, total=3)]
    assert (await h.user()).active_mission is None


# --- друг пишет первым ---


def zone_where_noon(moment: datetime) -> str:
    """Часовой пояс, в котором `moment` — полдень (Etc/GMT-N — это UTC+N)."""
    offset = (12 - moment.hour + 12) % 24 - 12
    return "UTC" if offset == 0 else f"Etc/GMT{-offset:+d}"


async def make_away(h: Harness, hours: float, tz: str = "UTC", **fields) -> None:
    async with h.session_factory() as session:
        await session.execute(
            update(User)
            .where(User.id == USER_ID)
            .values(
                last_active_at=datetime.now(UTC) - timedelta(hours=hours),
                timezone=tz,
                **fields,
            )
        )
        await session.commit()


async def test_friend_ping(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    await chat(h, "I want to buy a MacBook", "It's expensive", "Maybe next week")
    scheduler = PingScheduler(h.bot, h.dialog, h.session_factory)
    noon = datetime.now(UTC) + timedelta(days=1)  # в поясе пользователя это полдень
    tz = zone_where_noon(noon)

    await make_away(h, hours=5, tz=tz)
    assert await scheduler.tick(noon - timedelta(days=1)) == 0  # ещё не пропал

    await make_away(h, hours=0, tz=tz)
    h.tg.requests.clear()
    assert await scheduler.tick(noon) == 1  # через сутки — пишет сам
    assert h.sent_texts() == [fake_apis.companion_reply]
    note = fake_apis.companion_requests()[-1]["messages"][-1]["content"]
    assert "hasn't written for about a day" in note
    user = await h.user()
    assert user.pings_unanswered == 1 and user.last_ping_at is not None

    assert await scheduler.tick(noon + timedelta(minutes=5)) == 0  # не чаще раза в сутки
    night = noon + timedelta(days=1, hours=11)  # 23:00 у пользователя
    assert await scheduler.tick(night) == 0  # ночью не пишем

    await h.feed(text="Yes, I bought it!")  # ответил — счётчик сбрасывается
    assert (await h.user()).pings_unanswered == 0


async def test_friend_ping_respects_settings(harness: Harness) -> None:
    h = harness
    await onboard(h)
    await chat(h, "one", "two", "three")
    scheduler = PingScheduler(h.bot, h.dialog, h.session_factory)
    later = datetime.now(UTC) + timedelta(days=1)
    tz = zone_where_noon(later)
    await make_away(h, hours=0, tz=tz)

    await h.feed(text="/settings")
    assert texts.PING_BUTTONS[True] in str(h.tg.of_type(SendMessage)[0].reply_markup)
    await h.click(PingCb(on=False).pack())
    assert (await h.user()).ping_enabled is False
    assert await scheduler.tick(later) == 0

    await make_away(h, hours=0, tz=tz, ping_enabled=True, pings_unanswered=3)
    assert await scheduler.tick(later) == 0  # три сообщения без ответа — тишина

    await make_away(h, hours=0, tz=tz, pings_unanswered=0)
    await h.dialog.features.set("friend_ping", False)
    assert await scheduler.tick(later) == 0
    await h.dialog.features.set("friend_ping", True)
    assert await scheduler.tick(later) == 1  # контроль: при тех же условиях — пишет


# --- разбор пересланного ---


async def test_forwarded_english_post(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    origin = MessageOriginUser(
        date=datetime.now(UTC), sender_user=TgUser(id=9, is_bot=False, first_name="Bob")
    )
    post = "Apple has just announced a new laptop with a stunning display and all-day battery."
    await h.feed(text=post, forward_origin=origin)
    (answer,) = h.sent_texts()
    assert answer.startswith("📨 <b>О чём это:</b> Пост о запуске нового ноутбука.")
    assert "<b>mug</b> — кружка" in answer
    assert await h.count(Vocab, Vocab.source == "forward") == 2
    assert fake_apis.companion_requests()[-1]["messages"][-1]["content"] != post

    # Русский пересланный текст — обычная реплика в разговор.
    await h.feed(text="Привет, как дела? Это просто пересланное сообщение", forward_origin=origin)
    assert h.sent_texts() == [fake_apis.companion_reply]
