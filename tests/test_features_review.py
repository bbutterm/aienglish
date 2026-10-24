"""Регрессии по ревью ночных фич: гонки, лимиты, устойчивость к ответам моделей."""

import asyncio
import random
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.methods import AnswerPreCheckoutQuery
from aiogram.types import MessageOriginUser, PreCheckoutQuery, SuccessfulPayment, Update
from aiogram.types import User as TgUser
from sqlalchemy import update

from app.bot import texts
from app.db import repo
from app.db.models import Mistake, User
from app.services import journey, payments
from app.services.extract import parse_words
from app.services.lessons import Material, build_payload, clean_grammar
from app.services.podcast import clean_script
from tests.test_bot_flow import USER_ID, Harness, _ids, harness, onboard
from tests.test_web_api import Web, seed_user, web


async def test_referral_reward_paid_once_under_race(session_factory) -> None:
    async with session_factory() as session:
        friend = await repo.upsert_user(session, 777, "f", "Kate")
        user = await repo.upsert_user(session, USER_ID, "a", "Alex")
        user.referred_by = 777
        await session.commit()
    # Две реплики подряд: у обоих обработчиков свой, ещё «ненаграждённый» пользователь.
    async with session_factory() as s1, session_factory() as s2:
        u1, u2 = await s1.get(User, USER_ID), await s2.get(User, USER_ID)
        first = await journey.reward_referral(s1, u1, 5)
        second = await journey.reward_referral(s2, u2, 6)
    assert first is not None and second is None
    async with session_factory() as session:
        friend = await session.get(User, 777)
    assert friend.premium_until < datetime.now(UTC) + timedelta(days=8)


async def test_hint_shown_once_under_race(session_factory) -> None:
    async with session_factory() as session:
        await repo.upsert_user(session, USER_ID, "a", "Alex")
    async with session_factory() as s1, session_factory() as s2:
        u1, u2 = await s1.get(User, USER_ID), await s2.get(User, USER_ID)
        assert await journey.mark_hint(s1, u1, journey.HINT_INSIGHT) is True
        assert await journey.mark_hint(s2, u2, journey.HINT_INSIGHT) is False
        assert u2.hints == [journey.HINT_INSIGHT]


def test_start_payload_with_huge_id() -> None:
    assert journey.parse_start("ref_" + "9" * 20).referrer is None
    assert journey.parse_start("ref_123456789").referrer == 123456789


async def test_subscription_adds_days_on_top_of_pass(session_factory, settings) -> None:
    async with session_factory() as session:
        user = await repo.upsert_user(session, USER_ID, "a", "Alex")
        user.premium_until = datetime.now(UTC) + timedelta(days=90)
        paid = SuccessfulPayment(
            currency="XTR",
            total_amount=settings.stars_month,
            invoice_payload=payments.make_payload(USER_ID, payments.PLANS["month"]),
            telegram_payment_charge_id="c-stack",
            provider_payment_charge_id="",
            subscription_expiration_date=int((datetime.now(UTC) + timedelta(days=30)).timestamp()),
            is_recurring=True,
        )
        await payments.apply_payment(session, user, paid)
    assert user.premium_until > datetime.now(UTC) + timedelta(days=119)


async def test_blocked_user_cannot_pay(harness: Harness, settings) -> None:
    h = harness
    await onboard(h)
    async with h.session_factory() as session:
        await session.execute(update(User).where(User.id == USER_ID).values(blocked=True))
        await session.commit()
    query = PreCheckoutQuery(
        id="q1",
        from_user=h.tg_user,
        currency="XTR",
        total_amount=settings.stars_month,
        invoice_payload=payments.make_payload(USER_ID, payments.PLANS["month"]),
    )
    h.tg.requests.clear()
    await h.dp.feed_update(h.bot, Update(update_id=next(_ids), pre_checkout_query=query))
    (answer,) = h.tg.of_type(AnswerPreCheckoutQuery)
    assert answer.ok is False and answer.error_message == texts.PAYMENT_UNAVAILABLE


async def test_forwards_have_a_daily_cap(harness: Harness, settings) -> None:
    h = harness
    settings.daily_msg_limit = 5  # разборов — пятая часть: 1 в день
    await onboard(h)
    origin = MessageOriginUser(
        date=datetime.now(UTC), sender_user=TgUser(id=9, is_bot=False, first_name="Bob")
    )
    post = "Apple has just announced a new laptop with a stunning display and all-day battery."
    await h.feed(text=post, forward_origin=origin)
    assert h.sent_texts()[0].startswith("📨")
    await h.feed(text=post, forward_origin=origin)
    assert h.sent_texts() == [texts.FORWARD_LIMIT]


def test_parsers_survive_odd_json() -> None:
    assert clean_grammar([{"title_ru": "A", "rule_ru": "B", "mistake_ids": 5}], {1}) == [
        {"title_ru": "A", "rule_ru": "B", "examples": [], "mistake_ids": []}
    ]
    odd = [{"title_ru": "A", "rule_ru": "B", "mistake_ids": [[1], 1], "examples": {"en": "x"}}]
    assert clean_grammar(odd, {1})[0]["mistake_ids"] == [1]
    with pytest.raises(ValueError):
        clean_script({"lines": 5, "words": 3})
    assert parse_words('{"words": 3}', "photo", 5) == ([], None)
    material = Material(mistakes=[Mistake(id=1, original="a", corrected="b", type="x")], vocab=[])
    with pytest.raises(ValueError):
        build_payload({"exercises": {"type": "fix"}, "cards": 7}, material, random.Random(1))


async def test_first_visit_generates_ideas_once(web: Web, session_factory, fake_apis) -> None:
    await seed_user(session_factory)
    first, second = await asyncio.gather(web.get("/api/ideas"), web.get("/api/ideas"))
    assert len(first.json()["ideas"]) == len(second.json()["ideas"]) == 3
    calls = [
        r for r in fake_apis.llm_requests if "conversation topics" in r["messages"][0]["content"]
    ]
    assert len(calls) == 1
    assert (await web.get("/api/ideas")).json()["refreshes_left"] == 1


async def test_idea_start_is_not_repeated(web: Web, session_factory, fake_apis) -> None:
    await seed_user(session_factory)
    talk = next(i for i in (await web.get("/api/ideas")).json()["ideas"] if i["kind"] == "talk")
    for _ in range(3):
        assert (await web.post(f"/api/ideas/{talk['id']}/start")).status_code == 200
    await web.state.tasks.drain()
    openers = [r for r in fake_apis.companion_requests() if "picked a topic" in str(r)]
    assert len(openers) == 1
