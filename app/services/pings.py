"""«Друг пишет первым»: если пользователь пропал примерно на сутки, собеседник пишет сам.

Не дежурное «пора заниматься!», а продолжение вашего разговора («Ну что, купил MacBook?»).
Правила: не раньше чем через ~23 ч после последней активности (как у Duolingo — примерно в то же
время, когда человек обычно занимается), не ночью по его времени, не чаще раза в сутки, после трёх
сообщений без ответа — тишина; тех, кто пропал больше двух недель, не трогаем.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Message, User
from app.services.dialog import DialogService

logger = logging.getLogger(__name__)

AWAY_AFTER = timedelta(hours=23)
FORGET_AFTER = timedelta(days=14)
PING_EVERY = timedelta(hours=22)
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


async def due_users(session: AsyncSession, now: datetime) -> list[User]:
    talked = (
        select(func.count(Message.id))
        .where(Message.user_id == User.id, Message.role == "user")
        .scalar_subquery()
    )
    rows = await session.scalars(
        select(User)
        .where(
            User.onboarded.is_(True),
            User.blocked.is_(False),
            User.ping_enabled.is_(True),
            User.pings_unanswered < MAX_UNANSWERED,
            User.last_active_at <= now - AWAY_AFTER,
            User.last_active_at >= now - FORGET_AFTER,
            (User.last_ping_at.is_(None)) | (User.last_ping_at <= now - PING_EVERY),
            talked >= MIN_MESSAGES,
        )
        .order_by(User.last_active_at)
        .limit(CANDIDATES)
    )
    return [user for user in rows if awake(user, now)][:BATCH]


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
            user = await session.get(User, user_id)
            if user is None:
                return False
            # Отмечаем заранее: даже если LLM упадёт, не будем долбить пользователя каждые 5 минут.
            user.last_ping_at = now
            user.pings_unanswered += 1
            await session.commit()
            try:
                await send_opener(self._bot, user.id, session, user, self._dialog, "comeback")
            except TelegramForbiddenError:
                user.ping_enabled = False  # бот заблокирован — больше не пишем
                await session.commit()
                return False
            logger.info("Pinged user %s", user.id)
            return True
