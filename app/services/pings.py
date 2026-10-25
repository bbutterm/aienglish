"""«Друг пишет первым»: если пользователь пропал примерно на сутки, собеседник пишет сам.

Не дежурное «пора заниматься!», а продолжение вашего разговора («Ну что, купил MacBook?»).
Правила: не раньше чем через ~23 ч после последней активности и примерно в то же время суток
(±1 ч, как у Duolingo — когда человек обычно занимается), не ночью по его времени, не чаще раза
в сутки, после трёх сообщений без ответа — тишина; тех, кто пропал больше двух недель, не трогаем.

Часовой пояс сообщает Mini App; кто её не открывал, тот по умолчанию «московский». Окно «в то же
время суток» защищает и их: пинг приходит тогда, когда человек сам писал боту накануне.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Message, User
from app.services.dialog import DialogService

logger = logging.getLogger(__name__)

AWAY_AFTER = timedelta(hours=23)
FORGET_AFTER = timedelta(days=14)
PING_EVERY = timedelta(hours=22)
SAME_TIME_WINDOW = timedelta(hours=1)  # ± от времени суток последней активности
DAY = timedelta(days=1)
MAX_UNANSWERED = 3
MIN_MESSAGES = 3
QUIET_HOURS = (22, 9)  # с 22:00 до 9:00 по времени пользователя не пишем
BATCH = 50
CANDIDATES = 1000  # из них отбираем тех, у кого сейчас не ночь
TICK_SECONDS = 300


def local_hour(user: User, now: datetime) -> int:
    try:
        zone = ZoneInfo(user.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return now.astimezone(zone).hour


def awake(user: User, now: datetime) -> bool:
    start, end = QUIET_HOURS
    hour = local_hour(user, now)
    return end <= hour < start


def same_time_of_day(user: User, now: datetime) -> bool:
    """Сейчас ±1 ч от того времени суток, когда пользователь писал в последний раз."""
    if user.last_active_at is None:
        return False
    phase = (now - user.last_active_at) % DAY
    return phase <= SAME_TIME_WINDOW or phase >= DAY - SAME_TIME_WINDOW


def due_conditions(now: datetime) -> list[Any]:
    talked = (
        select(func.count(Message.id))
        .where(Message.user_id == User.id, Message.role == "user")
        .scalar_subquery()
    )
    return [
        User.onboarded.is_(True),
        User.blocked.is_(False),
        User.ping_enabled.is_(True),
        User.pings_unanswered < MAX_UNANSWERED,
        User.last_active_at <= now - AWAY_AFTER,
        User.last_active_at >= now - FORGET_AFTER,
        (User.last_ping_at.is_(None)) | (User.last_ping_at <= now - PING_EVERY),
        talked >= MIN_MESSAGES,
    ]


async def due_users(session: AsyncSession, now: datetime) -> list[User]:
    rows = await session.scalars(
        select(User).where(*due_conditions(now)).order_by(User.last_active_at).limit(CANDIDATES)
    )
    return [u for u in rows if awake(u, now) and same_time_of_day(u, now)][:BATCH]


class PingScheduler:
    def __init__(
        self,
        bot: Bot,
        dialog: DialogService,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._bot = bot
        self._dialog = dialog
        self._session_factory = session_factory

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Ping tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self, now: datetime | None = None) -> int:
        if not await self._dialog.features.enabled("friend_ping"):
            return 0
        now = now or datetime.now(UTC)
        async with self._session_factory() as session:
            users = await due_users(session, now)
        sent = 0
        for user in users:
            if await self._ping(user.id, now):
                sent += 1
        return sent

    async def _ping(self, user_id: int, now: datetime) -> bool:
        # Импорт здесь: модуль бота сам зависит от сервисов.
        from app.bot.handlers.common import send_opener

        async with self._session_factory() as session:
            # Атомарно «забираем» пользователя, перепроверив условия: пока шла пачка пингов, он мог
            # вернуться сам или выключить пинги, а второй процесс бота — выбрать его же.
            # Отмечаем заранее: даже если LLM упадёт, не будем долбить пользователя каждые 5 минут.
            claimed = await session.scalar(
                update(User)
                .where(User.id == user_id, *due_conditions(now))
                .values(last_ping_at=now, pings_unanswered=User.pings_unanswered + 1)
                .returning(User.id)
            )
            await session.commit()
            user = await session.get(User, user_id) if claimed else None
            if user is None:
                return False
            try:
                await send_opener(self._bot, user.id, session, user, self._dialog, "comeback")
            except TelegramForbiddenError:
                user.ping_enabled = False  # бот заблокирован — больше не пишем
                await session.commit()
                return False
            logger.info("Pinged user %s", user.id)
            return True
