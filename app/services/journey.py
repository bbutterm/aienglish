"""Путь нового пользователя: откуда пришёл, приглашения и подсказки первых дней.

Первые минуты — только разговор (никаких анкет). Дальше по одной подсказке за раз, когда уже есть
что показать: после 3-й реплики — «вот что я заметил» (ошибки и простые слова), после 6-й —
проверка уровня по речи, после 8-й — приглашение в урок, после 15-й — настройки.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, Mistake, User, Vocab
from app.services.plans import extend_premium

LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
REFERRAL_DAYS = 7
REFERRAL_ACTIVATION = 5  # столько реплик должен сделать приглашённый
_PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

HINT_INSIGHT = "first_insight"
HINT_LEVEL = "level_check"
HINT_APP = "app_invite"
HINT_SETTINGS = "settings_tip"
ALL_HINTS = (HINT_INSIGHT, HINT_LEVEL, HINT_APP, HINT_SETTINGS)
INSIGHT_AFTER, INSIGHT_GIVE_UP = 3, 10
LEVEL_AFTER = 6
APP_AFTER = 8
SETTINGS_AFTER = 15


@dataclass(frozen=True)
class StartPayload:
    source: str | None
    referrer: int | None


def parse_start(args: str | None) -> StartPayload:
    """Параметр /start: ref_<telegram id> — приглашение, остальное — метка источника."""
    if not args or not _PAYLOAD_RE.match(args):
        return StartPayload(None, None)
    if args.startswith("ref_") and args[4:].isdigit():
        return StartPayload("ref", int(args[4:]))
    return StartPayload(args.lower(), None)


async def apply_start(session: AsyncSession, user: User, payload: StartPayload) -> bool:
    """Запоминает первый источник нового пользователя. True — если засчитано приглашение."""
    if user.onboarded or user.source is not None or payload.source is None:
        return False
    user.source = payload.source
    invited = False
    if payload.referrer and payload.referrer != user.id:
        referrer = await session.get(User, payload.referrer)
        if referrer is not None and not referrer.blocked:
            user.referred_by = referrer.id
            invited = True
    await session.commit()
    return invited


async def user_messages(session: AsyncSession, user_id: int) -> int:
    return (
        await session.scalar(
            select(func.count(Message.id)).where(Message.user_id == user_id, Message.role == "user")
        )
    ) or 0


async def reward_referral(session: AsyncSession, user: User, messages: int) -> User | None:
    """Приглашённый освоился — обоим неделя премиума. Возвращает пригласившего, если наградили."""
    if not user.referred_by or user.referral_rewarded or messages < REFERRAL_ACTIVATION:
        return None
    referrer = await session.get(User, user.referred_by)
    user.referral_rewarded = True
    if referrer is None or referrer.blocked:
        await session.commit()
        return None
    extend_premium(user, REFERRAL_DAYS)
    extend_premium(referrer, REFERRAL_DAYS)
    await session.commit()
    return referrer


def level_gap(level: str, estimated: str | None) -> bool:
    """Оценка по речи отличается от выбранного уровня хотя бы на ступень."""
    return estimated in LEVELS and level in LEVELS and estimated != level


async def mark_hint(session: AsyncSession, user: User, key: str) -> None:
    if key not in user.hints:
        user.hints = [*user.hints, key]
        await session.commit()


async def insight_items(
    session: AsyncSession, user: User, limit: int = 3
) -> tuple[list[Mistake], list[Vocab]]:
    """Свежие ошибки и прокачанные слова для подсказки «вот что я заметил»."""
    since = datetime.now(UTC) - timedelta(days=2)
    mistakes = await session.scalars(
        select(Mistake)
        .where(Mistake.user_id == user.id, Mistake.created_at >= since)
        .order_by(Mistake.severity.desc(), Mistake.id.desc())
        .limit(limit)
    )
    upgrades = await session.scalars(
        select(Vocab)
        .where(Vocab.user_id == user.id, Vocab.source == "upgrade", Vocab.created_at >= since)
        .order_by(Vocab.id.desc())
        .limit(1)
    )
    return list(mistakes), list(upgrades)


def next_hint(user: User, messages: int, has_insight: bool, app: bool) -> str | None:
    """Какую подсказку показать после этой реплики (не больше одной за раз)."""
    shown = set(user.hints)
    if (
        HINT_INSIGHT not in shown
        and messages >= INSIGHT_AFTER
        and (has_insight or messages >= INSIGHT_GIVE_UP)
    ):
        return HINT_INSIGHT
    if HINT_LEVEL not in shown and messages >= LEVEL_AFTER:
        return HINT_LEVEL
    if app and HINT_APP not in shown and messages >= APP_AFTER:
        return HINT_APP
    if HINT_SETTINGS not in shown and messages >= SETTINGS_AFTER:
        return HINT_SETTINGS
    return None
