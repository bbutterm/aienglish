"""Регрессии аудита 2026-10-25: «друг пишет первым», секреты в логах, initData, схема API."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram.methods import SendMessage
from sqlalchemy import update

from app.db.models import User
from app.main import redact_secrets
from app.services import pings
from app.services.pings import PingScheduler
from tests.conftest import sign_init_data
from tests.test_bot_flow import USER_ID, Harness, harness, onboard
from tests.test_web_api import Web, seed_user, web


async def chat(h: Harness, *lines: str) -> None:
    for line in lines:
        await h.feed(text=line)


async def set_user(h: Harness, **values) -> None:
    async with h.session_factory() as session:
        await session.execute(update(User).where(User.id == USER_ID).values(**values))
        await session.commit()


async def first_ping(h: Harness, since: datetime) -> datetime | None:
    """Гоняем планировщик каждые 5 минут, как в проде; когда он напишет?"""
    scheduler = PingScheduler(h.bot, h.dialog, h.session_factory)
    moment = since + timedelta(hours=20)
    while moment < since + timedelta(hours=72):
        if await scheduler.tick(moment):
            return moment
        moment += timedelta(seconds=pings.TICK_SECONDS)
    return None


def is_night(moment: datetime, zone: str) -> bool:
    start, end = pings.QUIET_HOURS
    return not end <= moment.astimezone(ZoneInfo(zone)).hour < start


async def test_ping_never_at_night_for_user_outside_moscow(harness: Harness, fake_apis) -> None:
    """Пояс неизвестен (по умолчанию Москва), человек из Нью-Йорка болтал в 20:00 по своему
    времени. Раньше пинг приходил в 9:00 МСК — в 2 часа ночи по Нью-Йорку."""
    h = harness
    await onboard(h)
    await chat(h, "I want to buy a MacBook", "It's expensive", "Maybe next week")
    seen = datetime(2026, 10, 24, 20, 0, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
    await set_user(h, last_active_at=seen)
    assert (await h.user()).timezone == "Europe/Moscow"
    moment = await first_ping(h, seen)
    assert moment is None or not is_night(moment, "America/New_York")


async def test_ping_comes_at_the_users_usual_time(harness: Harness, fake_apis) -> None:
    h = harness
    await onboard(h)
    await chat(h, "one", "two", "three")
    seen = datetime(2026, 10, 24, 15, 0, tzinfo=ZoneInfo("Europe/Moscow")).astimezone(UTC)
    await set_user(h, last_active_at=seen)
    moment = await first_ping(h, seen)
    assert moment is not None
    assert abs((moment - seen) - timedelta(days=1)) <= pings.SAME_TIME_WINDOW


async def test_known_timezone_keeps_night_owls_quiet(harness: Harness, fake_apis) -> None:
    """Пояс известен из Mini App: болтал в 00:30 по Новосибирску — ночью не пишем."""
    h = harness
    await onboard(h)
    await chat(h, "one", "two", "three")
    seen = datetime(2026, 10, 25, 0, 30, tzinfo=ZoneInfo("Asia/Novosibirsk")).astimezone(UTC)
    await set_user(h, last_active_at=seen, timezone="Asia/Novosibirsk")
    moment = await first_ping(h, seen)
    assert moment is None or not is_night(moment, "Asia/Novosibirsk")


async def test_two_schedulers_ping_once(harness: Harness, fake_apis, monkeypatch) -> None:
    """Два процесса бота (деплой без остановки старого) выбрали одного кандидата."""
    h = harness
    await onboard(h)
    await chat(h, "one", "two", "three")
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)  # 15:00 МСК
    await set_user(h, last_active_at=now - timedelta(hours=24))
    original = pings.due_users
    barrier = asyncio.Barrier(2)

    async def both_selected(session, moment):
        rows = await original(session, moment)
        await barrier.wait()
        return rows

    monkeypatch.setattr(pings, "due_users", both_selected)
    h.tg.requests.clear()
    first = PingScheduler(h.bot, h.dialog, h.session_factory)
    second = PingScheduler(h.bot, h.dialog, h.session_factory)
    assert sum(await asyncio.gather(first.tick(now), second.tick(now))) == 1
    assert len([m for m in h.tg.of_type(SendMessage) if m.chat_id == USER_ID]) == 1


async def test_no_ping_right_after_user_came_back(harness: Harness, fake_apis, monkeypatch) -> None:
    """Пачка пингов идёт минутами: пока очередь дошла, пользователь уже вернулся сам."""
    h = harness
    await onboard(h)
    await chat(h, "one", "two", "three")
    now = datetime.now(UTC)
    await set_user(h, last_active_at=now - timedelta(hours=24), timezone="UTC")
    if not pings.awake(await h.user(), now):
        now = now.replace(hour=12)
        await set_user(h, last_active_at=now - timedelta(hours=24))
    original = pings.due_users

    async def due_then_user_writes(session, moment):
        rows = await original(session, moment)
        assert rows
        await h.feed(text="Hi! I'm back")
        return rows

    monkeypatch.setattr(pings, "due_users", due_then_user_writes)
    assert await PingScheduler(h.bot, h.dialog, h.session_factory).tick(now) == 0
    notes = [r["messages"][-1]["content"] for r in fake_apis.companion_requests()]
    assert not [n for n in notes if "hasn't written for about a day" in str(n)]


async def test_mini_app_reports_timezone(web: Web, session_factory) -> None:
    await seed_user(session_factory)

    async def me(zone: str):
        return await web.client.get("/api/me", headers={**web.headers(USER_ID), "X-Timezone": zone})

    assert (await me("Asia/Novosibirsk")).status_code == 200
    async with session_factory() as session:
        assert (await session.get(User, USER_ID)).timezone == "Asia/Novosibirsk"
    for bad in ("../../etc/passwd", "Mars/Olympus", "x" * 100):
        assert (await me(bad)).status_code == 200
    async with session_factory() as session:
        assert (await session.get(User, USER_ID)).timezone == "Asia/Novosibirsk"


# --- секреты и Mini App ---


def test_bot_token_is_redacted_from_logs() -> None:
    """URL файлов Telegram содержит токен: /file/bot<ТОКЕН>/... — в лог он не попадает."""
    token = "123456:SECRET-TOKEN-abcdef"
    original = logging.getLogRecordFactory()
    lines: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(logging.Formatter().format(record))

    logger = logging.getLogger("audit.redact")
    handler = Collect()
    logger.addHandler(handler)
    try:
        redact_secrets([token, "", "short"])
        try:
            raise RuntimeError(f"502, url='https://api.telegram.org/file/bot{token}/voice/a.oga'")
        except RuntimeError:
            logger.error("Update handling failed for bot%s", token, exc_info=True)
        logger.error("plain %s", "message")
    finally:
        logging.setLogRecordFactory(original)
        logger.removeHandler(handler)
    assert lines and all(token not in line for line in lines)
    assert "file/bot***/voice" in lines[0] and lines[1] == "plain message"


async def test_non_ascii_init_data_hash_is_401(web: Web) -> None:
    good = sign_init_data(web.token, USER_ID)
    forged = good.rsplit("hash=", 1)[0] + "hash=%C3%A9"
    response = await web.client.get("/api/me", headers={"Authorization": f"tma {forged}"})
    assert response.status_code == 401


async def test_api_schema_is_not_public(web: Web) -> None:
    assert (await web.client.get("/openapi.json")).status_code == 404
