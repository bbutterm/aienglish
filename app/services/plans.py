from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.db.models import User


@dataclass(frozen=True)
class Limits:
    messages: int
    tts: int
    lessons: int


def is_premium(user: User, now: datetime | None = None) -> bool:
    return user.premium_until is not None and user.premium_until > (now or datetime.now(UTC))


def limits_for(user: User, settings: Settings, now: datetime | None = None) -> Limits:
    if is_premium(user, now):
        return Limits(
            messages=settings.premium_daily_msg_limit,
            tts=settings.premium_daily_tts_limit,
            lessons=settings.premium_daily_lesson_limit,
        )
    return Limits(
        messages=settings.daily_msg_limit,
        tts=settings.daily_tts_limit,
        lessons=settings.daily_lesson_limit,
    )


def extend_premium(user: User, days: int, now: datetime | None = None) -> None:
    """Продлевает премиум на `days` дней от текущего конца (или от сейчас); 0 — отменяет."""
    now = now or datetime.now(UTC)
    if days <= 0:
        user.premium_until = None
        return
    start = user.premium_until if is_premium(user, now) else now
    user.premium_until = start + timedelta(days=days)
