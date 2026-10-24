"""Премиум за звёзды и пробная неделя."""

from datetime import UTC, datetime, timedelta

from aiogram.methods import AnswerPreCheckoutQuery, CreateInvoiceLink, SendMessage
from aiogram.types import PreCheckoutQuery, SuccessfulPayment, Update
from aiogram.types import User as TgUser
from sqlalchemy import func, select

from app.bot import texts
from app.db.models import Payment
from app.services import payments
from tests.test_bot_flow import USER_ID, Harness, _ids, harness, onboard
from tests.test_web_api import Web, seed_user, web


def test_payload_and_checkout(settings) -> None:
    plan = payments.PLANS["year"]
    payload = payments.make_payload(42, plan)
    assert payments.parse_payload(payload) == (plan, 42)
    assert payments.parse_payload("premium:forever:42") is None
    assert payments.check_checkout(payload, 42, settings.stars_year, settings) is None
    assert payments.check_checkout(payload, 43, settings.stars_year, settings) is not None
    assert payments.check_checkout(payload, 42, 1, settings) is not None


async def test_premium_command(harness: Harness, settings) -> None:
    h = harness
    await onboard(h)
    await h.feed(text="/premium")
    assert h.sent_texts() == [texts.PREMIUM_OFF]  # по умолчанию оплата выключена

    await h.dialog.features.set("payments_stars", True)
    await h.feed(text="/premium")
    (pitch,) = h.tg.of_type(SendMessage)
    links = [row[0].url for row in pitch.reply_markup.inline_keyboard]
    assert len(links) == 3 and all(link.startswith("https://t.me/$invoice-") for link in links)
    invoices = h.tg.of_type(CreateInvoiceLink)
    month = next(i for i in invoices if "month" in i.payload)
    assert (month.currency, month.subscription_period) == ("XTR", 30 * 24 * 3600)
    assert month.prices[0].amount == settings.stars_month
    assert next(i for i in invoices if "year" in i.payload).subscription_period is None


async def test_pre_checkout_and_payment(harness: Harness, settings) -> None:
    h = harness
    await onboard(h)
    payload = payments.make_payload(USER_ID, payments.PLANS["month"])

    async def checkout(amount: int, payload: str = payload) -> AnswerPreCheckoutQuery:
        h.tg.requests.clear()
        query = PreCheckoutQuery(
            id=str(next(_ids)),
            from_user=h.tg_user,
            currency="XTR",
            total_amount=amount,
            invoice_payload=payload,
        )
        await h.dp.feed_update(h.bot, Update(update_id=next(_ids), pre_checkout_query=query))
        return h.tg.of_type(AnswerPreCheckoutQuery)[0]

    assert (await checkout(settings.stars_month)).ok is True
    assert (await checkout(1)).ok is False  # цена не та
    other = payments.make_payload(999, payments.PLANS["month"])
    assert (await checkout(settings.stars_month, other)).ok is False  # чужой счёт

    expires = datetime.now(UTC) + timedelta(days=30)
    paid = SuccessfulPayment(
        currency="XTR",
        total_amount=settings.stars_month,
        invoice_payload=payload,
        telegram_payment_charge_id="charge-1",
        provider_payment_charge_id="",
        subscription_expiration_date=int(expires.timestamp()),
        is_recurring=True,
        is_first_recurring=True,
    )
    await h.feed(successful_payment=paid)
    user = await h.user()
    assert abs((user.premium_until - expires).total_seconds()) < 2
    assert h.sent_texts()[0].startswith("🎉 Спасибо! Премиум активен до")
    await h.feed(successful_payment=paid)  # Telegram прислал тот же платёж ещё раз
    assert h.sent_texts() == []
    async with h.session_factory() as session:
        assert await session.scalar(select(func.count(Payment.id))) == 1


async def test_trial_week_and_limit_upsell(harness: Harness, settings) -> None:
    h = harness
    await h.dialog.features.set("trial_week", True)
    await h.feed(text="/start")
    assert h.sent_texts()[0].startswith(texts.TRIAL_WELCOME)
    user = await h.user()
    assert user.premium_until > datetime.now(UTC) + timedelta(days=6, hours=23)

    await h.dialog.features.set("payments_stars", True)
    settings.premium_daily_msg_limit = 1
    await h.feed(text="hi")
    await h.feed(text="again")
    assert h.sent_texts()[0].endswith(texts.LIMIT_UPSELL)


async def test_premium_api(web: Web, session_factory) -> None:
    await seed_user(session_factory)
    assert (await web.get("/api/premium")).status_code == 404
    await web.state.features.set("payments_stars", True)
    data = (await web.get("/api/premium")).json()
    assert [p["key"] for p in data["plans"]] == ["month", "quarter", "year"]
    assert data["plans"][0]["subscription"] is True and data["premium"] is False
    link = (await web.post("/api/premium/year/invoice")).json()["link"]
    assert link == "https://t.me/$invoice-premium-year-555"
    assert (await web.post("/api/premium/forever/invoice")).status_code == 404
