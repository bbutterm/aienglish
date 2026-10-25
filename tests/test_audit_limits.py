"""Регрессии аудита 2026-10-25: лимиты и деньги при параллельных апдейтах и повторах."""

import asyncio
import itertools
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from aiogram.methods import GetFile, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    MessageOriginUser,
    PhotoSize,
    SuccessfulPayment,
    Update,
    Voice,
)
from aiogram.types import Message as TgMessage
from aiogram.types import User as TgUser
from sqlalchemy import func, select, update

from app.bot import texts
from app.bot.handlers.text import MAX_TEXT_CHARS
from app.bot.keyboards import RewriteCb, TtsCb
from app.db import repo
from app.db.models import Message, Payment, Usage, User
from app.services import journey, payments
from app.web import app as web_app
from app.web import features_api
from tests.conftest import FakeAPIs
from tests.test_bot_flow import USER_ID, Harness, harness, onboard
from tests.test_web_api import Web, seed_user, web

_ids = itertools.count(50_000_000)


class Delays:
    """Задержка внешних API, чтобы параллельные обработчики перемежались, как в проде."""

    llm = 0.0
    stt = 0.0
    tts = 0.0


@pytest.fixture
def delays() -> Delays:
    return Delays()


@pytest.fixture
async def http(fake_apis: FakeAPIs, delays: Delays) -> AsyncGenerator[httpx.AsyncClient]:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            await asyncio.sleep(delays.llm)
        elif request.url.path == "/v1/speech-to-text":
            await asyncio.sleep(delays.stt)
        elif request.url.path.startswith("/v1/text-to-speech/"):
            await asyncio.sleep(delays.tts)
        return fake_apis.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yield client


def tg_user() -> TgUser:
    return TgUser(id=USER_ID, is_bot=False, first_name="Alex", username="alex")


def message_update(**fields: Any) -> Update:
    message = TgMessage(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=USER_ID, type="private"),
        from_user=tg_user(),
        **fields,
    )
    return Update(update_id=next(_ids), message=message)


def callback_update(data: str) -> Update:
    callback = CallbackQuery(
        id=str(next(_ids)),
        from_user=tg_user(),
        chat_instance="ci",
        data=data,
        message=message_update(text="menu").message,
    )
    return Update(update_id=next(_ids), callback_query=callback)


async def feed_parallel(h: Harness, updates: list[Update]) -> None:
    """Как polling aiogram: каждый апдейт — своя задача."""
    h.tg.requests.clear()
    await asyncio.gather(*(h.dp.feed_update(h.bot, u) for u in updates))
    await h.dialog.tasks.drain()


async def count(h_or_factory: Any, model: Any, *where: Any) -> int:
    factory = getattr(h_or_factory, "session_factory", h_or_factory)
    async with factory() as session:
        return await session.scalar(select(func.count(model.id)).where(*where))


async def last_reply_id(h: Harness) -> int:
    async with h.session_factory() as session:
        return await session.scalar(
            select(Message.id).where(Message.role == "assistant").order_by(Message.id.desc())
        )


# --- бот: параллельные апдейты не обходят дневные лимиты ---


async def test_parallel_voice_notes_respect_daily_limit(harness: Harness, settings, delays) -> None:
    h = harness
    await onboard(h)
    settings.daily_msg_limit = 1
    delays.stt = 0.2
    voice = Voice(file_id="v", file_unique_id="u", duration=30)
    await feed_parallel(h, [message_update(voice=voice) for _ in range(5)])
    assert await count(h, Message, Message.role == "user") == 1
    assert await count(h, Usage, Usage.kind == "stt") == 1


async def test_photo_album_respects_daily_limit(harness: Harness, settings, delays) -> None:
    h = harness
    await onboard(h)
    settings.daily_msg_limit = 1
    delays.llm = 0.1
    photo = [PhotoSize(file_id="p", file_unique_id="u", width=800, height=600)]
    updates = [message_update(photo=photo, media_group_id="album") for _ in range(6)]
    await feed_parallel(h, updates)
    assert await count(h, Message, Message.role == "user") == 1


async def test_parallel_forwards_respect_cap(harness: Harness, settings, delays) -> None:
    h = harness
    settings.daily_msg_limit = 5  # разборов в день — пятая часть: 1
    await onboard(h)
    delays.llm = 0.2
    origin = MessageOriginUser(
        date=datetime.now(UTC), sender_user=TgUser(id=9, is_bot=False, first_name="Bob")
    )
    post = "Apple has just announced a new laptop with a stunning display and all-day battery."
    await feed_parallel(h, [message_update(text=post, forward_origin=origin) for _ in range(6)])
    assert await count(h, Usage, Usage.kind == "llm_forward") == 1


async def test_tts_button_tapped_three_times_pays_once(harness: Harness, settings, delays) -> None:
    h = harness
    settings.daily_tts_limit = 1
    await onboard(h)
    await h.feed(text="Hello there")
    data = TtsCb(message_id=await last_reply_id(h)).pack()
    delays.tts = 0.2
    await feed_parallel(h, [callback_update(data) for _ in range(3)])
    assert await count(h, Usage, Usage.kind == "tts") == 1


async def test_rewrite_taps_respect_limit(harness: Harness, settings, delays) -> None:
    h = harness
    await onboard(h)
    settings.daily_msg_limit = 1
    await h.feed(text="I am working here since three years")
    data = RewriteCb(message_id=await last_reply_id(h)).pack()
    delays.llm = 0.2
    await feed_parallel(h, [callback_update(data) for _ in range(4)])
    assert await count(h, Usage, Usage.kind == "llm_rewrite") == 1


async def test_reset_spends_daily_limit(harness: Harness, settings, fake_apis) -> None:
    """/reset — вызов модели: расходует реплику из дневного лимита, а не бесплатен."""
    h = harness
    settings.daily_msg_limit = 2
    await onboard(h)
    before = len(fake_apis.companion_requests())
    for _ in range(5):
        await h.feed(text="/reset")
    assert len(fake_apis.companion_requests()) - before == 2
    assert texts.LIMIT_REACHED.format(limit=2) in h.sent_texts()


async def test_voice_size_is_checked_not_only_declared_duration(
    harness: Harness, fake_apis
) -> None:
    h = harness
    await onboard(h)
    await h.feed(voice=Voice(file_id="v", file_unique_id="u", duration=5, file_size=20_000_000))
    assert not fake_apis.stt_requests
    assert h.sent_texts() == [texts.VOICE_TOO_LONG.format(limit=60)]


async def test_long_transcript_is_capped_like_text(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    fake_apis.stt_text = ("I went to the shop and bought some bread. " * 200).strip()
    await h.feed(voice=Voice(file_id="v", file_unique_id="u", duration=55, file_size=200_000))
    assert len(fake_apis.companion_requests()[-1]["messages"][-1]["content"]) <= MAX_TEXT_CHARS


# --- Mini App: «одноразовые» действия при параллельных запросах ---


def content_calls(fake_apis: FakeAPIs) -> int:
    return sum(
        1
        for r in fake_apis.llm_requests
        if r["messages"][0]["content"].startswith("You prepare a short")
    )


async def test_parallel_idea_open_generates_once(
    web: Web, session_factory, fake_apis, delays
) -> None:
    await seed_user(session_factory)
    idea_id = (await web.get("/api/ideas")).json()["ideas"][0]["id"]
    delays.llm = 0.2
    responses = await asyncio.gather(*(web.post(f"/api/ideas/{idea_id}/open") for _ in range(8)))
    assert all(r.status_code == 200 for r in responses)
    assert all(r.json()["content"] for r in responses)
    assert content_calls(fake_apis) == 1


async def test_parallel_idea_start_writes_once(
    web: Web, session_factory, fake_apis, monkeypatch
) -> None:
    await seed_user(session_factory)
    talk = next(i for i in (await web.get("/api/ideas")).json()["ideas"] if i["kind"] == "talk")
    await web.post(f"/api/ideas/{talk['id']}/open")
    original = features_api.own_idea
    barrier = asyncio.Barrier(2)

    async def own_idea_then_wait(session, user, idea_id):
        found = await original(session, user, idea_id)
        await barrier.wait()  # оба запроса прочитали тему до того, как другой её отметил
        return found

    monkeypatch.setattr(features_api, "own_idea", own_idea_then_wait)
    url = f"/api/ideas/{talk['id']}/start"
    responses = await asyncio.gather(web.post(url), web.post(url))
    assert all(r.status_code == 200 for r in responses)
    await web.state.tasks.drain()
    openers = [r for r in fake_apis.companion_requests() if "picked a topic" in str(r)]
    assert len(openers) == 1


async def test_lesson_completed_twice_follows_up_once(
    web: Web, session_factory, monkeypatch
) -> None:
    await seed_user(session_factory)
    lesson = (await web.post("/api/lessons")).json()
    web.tg.requests.clear()
    original = web_app.own_lesson
    barrier = asyncio.Barrier(2)

    async def own_lesson_then_wait(session, user, lesson_id):
        found = await original(session, user, lesson_id)
        await barrier.wait()
        return found

    monkeypatch.setattr(web_app, "own_lesson", own_lesson_then_wait)
    url = f"/api/lessons/{lesson['id']}/complete"
    responses = await asyncio.gather(web.post(url), web.post(url))
    assert all(r.status_code == 200 and r.json()["status"] == "done" for r in responses)
    await web.state.tasks.drain()
    assert len([m for m in web.tg.of_type(SendMessage) if m.chat_id == USER_ID]) == 1


# --- оплата ---


def month_payment(settings, charge: str, **fields: Any) -> SuccessfulPayment:
    return SuccessfulPayment(
        currency="XTR",
        total_amount=settings.stars_month,
        invoice_payload=payments.make_payload(USER_ID, payments.PLANS["month"]),
        telegram_payment_charge_id=charge,
        provider_payment_charge_id="",
        **fields,
    )


async def test_renewal_of_blocked_subscriber_is_recorded(harness: Harness, settings) -> None:
    """Продление подписки приходит без pre-checkout: звёзды списаны — платёж записываем."""
    h = harness
    await onboard(h)
    await h.feed(successful_payment=month_payment(settings, "c-1", is_first_recurring=True))
    async with h.session_factory() as session:
        await session.execute(update(User).where(User.id == USER_ID).values(blocked=True))
        await session.commit()
    await h.feed(successful_payment=month_payment(settings, "c-2", is_recurring=True))
    assert await count(h, Payment) == 2
    await h.feed(text="Hello")  # обычные сообщения заблокированного по-прежнему игнорируем
    assert h.sent_texts() == []


async def test_payment_keeps_concurrent_referral_bonus(session_factory, settings) -> None:
    async with session_factory() as session:
        await repo.upsert_user(session, USER_ID, "a", "Alex")
        friend = await repo.upsert_user(session, 777, "k", "Kate")
        friend.referred_by = USER_ID
        await session.commit()
    async with session_factory() as pay_session, session_factory() as other:
        payer = await repo.upsert_user(pay_session, USER_ID, "a", "Alex")  # как в middleware
        friend = await other.get(User, 777)
        assert await journey.reward_referral(other, friend, 5) is not None  # +7 дней Alex'у
        await payments.apply_payment(pay_session, payer, month_payment(settings, "c-q"))
    async with session_factory() as session:
        until = (await session.get(User, USER_ID)).premium_until
    assert (
        round((until - datetime.now(UTC)) / timedelta(days=1)) == 7 + payments.PLANS["month"].days
    )


async def test_redelivered_payment_counts_once_in_parallel(session_factory, settings) -> None:
    async with session_factory() as session:
        await repo.upsert_user(session, USER_ID, "a", "Alex")
    paid = month_payment(settings, "c-dup")
    async with session_factory() as s1, session_factory() as s2:
        u1, u2 = await s1.get(User, USER_ID), await s2.get(User, USER_ID)
        results = await asyncio.gather(
            payments.apply_payment(s1, u1, paid), payments.apply_payment(s2, u2, paid)
        )
    assert sum(r is not None for r in results) == 1
    async with session_factory() as session:
        until = (await session.get(User, USER_ID)).premium_until
    assert round((until - datetime.now(UTC)) / timedelta(days=1)) == payments.PLANS["month"].days
