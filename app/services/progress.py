"""Прогресс ученика для Mini App: активность, темы, ошибки, слова, сильные и слабые стороны."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Lesson, Message, Mistake, Usage, User, Vocab
from app.services.review import TYPE_LABELS_RU

ACTIVITY_DAYS = 60
TOPICS_LIMIT = 6


def _zone(name: str | None) -> str:
    try:
        ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return name or "UTC"


def streak(active_days: set[date], today: date) -> int:
    """Дни подряд с активностью; сегодняшний день не рвёт серию, пока не закончился."""
    day = today if today in active_days else today - timedelta(days=1)
    count = 0
    while day in active_days:
        count += 1
        day -= timedelta(days=1)
    return count


def per_ten(mistakes: int, messages: int) -> float | None:
    return round(mistakes * 10 / messages, 1) if messages else None


async def _active_days(session: AsyncSession, user: User, tz: str, since: datetime) -> set[date]:
    message_day = func.date(func.timezone(tz, Message.created_at))
    lesson_day = func.date(func.timezone(tz, Lesson.completed_at))
    messages = await session.scalars(
        select(message_day)
        .where(Message.user_id == user.id, Message.role == "user", Message.created_at >= since)
        .distinct()
    )
    lessons = await session.scalars(
        select(lesson_day).where(Lesson.user_id == user.id, Lesson.completed_at >= since).distinct()
    )
    return set(messages) | set(lessons)


async def _count(session: AsyncSession, column: Any, *where: Any) -> int:
    return (await session.scalar(select(func.count(column)).where(*where))) or 0


async def build_progress(
    session: AsyncSession, user: User, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    tz = _zone(user.timezone)
    today = now.astimezone(ZoneInfo(tz)).date()
    week_ago, two_weeks_ago = now - timedelta(days=7), now - timedelta(days=14)
    month_ago, two_months_ago = now - timedelta(days=30), now - timedelta(days=60)

    active = await _active_days(session, user, tz, now - timedelta(days=ACTIVITY_DAYS))
    is_user_msg = (Message.user_id == user.id, Message.role == "user")
    messages_week = await _count(session, Message.id, *is_user_msg, Message.created_at >= week_ago)
    messages_prev = await _count(
        session,
        Message.id,
        *is_user_msg,
        Message.created_at >= two_weeks_ago,
        Message.created_at < week_ago,
    )
    mistakes_week = await _count(
        session, Mistake.id, Mistake.user_id == user.id, Mistake.created_at >= week_ago
    )
    mistakes_prev = await _count(
        session,
        Mistake.id,
        Mistake.user_id == user.id,
        Mistake.created_at >= two_weeks_ago,
        Mistake.created_at < week_ago,
    )

    rows = await session.execute(
        select(
            Mistake.type,
            func.count(Mistake.id).filter(Mistake.created_at >= month_ago),
            func.count(Mistake.id).filter(Mistake.created_at < month_ago),
        )
        .where(Mistake.user_id == user.id, Mistake.created_at >= two_months_ago)
        .group_by(Mistake.type)
    )
    by_type = [
        {"type": kind, "label_ru": TYPE_LABELS_RU.get(kind, kind), "count": recent, "prev": prev}
        for kind, recent, prev in rows
    ]

    recurring = await session.scalars(
        select(Mistake)
        .where(Mistake.user_id == user.id, Mistake.resolved.is_(False), Mistake.times_seen > 1)
        .order_by(Mistake.times_seen.desc(), Mistake.id.desc())
        .limit(5)
    )
    topic_rows = await session.execute(
        select(Message.topic, func.count(Message.id.distinct()), func.count(Mistake.id))
        .outerjoin(Mistake, Mistake.message_id == Message.id)
        .where(*is_user_msg, Message.topic.is_not(None), Message.created_at >= month_ago)
        .group_by(Message.topic)
        .order_by(func.count(Message.id.distinct()).desc(), Message.topic)
        .limit(TOPICS_LIMIT)
    )
    topics = [
        {"topic": topic, "messages": messages, "mistakes": mistakes}
        for topic, messages, mistakes in topic_rows
    ]
    vocab_rows = await session.execute(
        select(Vocab.status, func.count(Vocab.id))
        .where(Vocab.user_id == user.id)
        .group_by(Vocab.status)
    )
    vocab = dict(vocab_rows.all())
    words_due = await _count(
        session,
        Vocab.id,
        Vocab.user_id == user.id,
        (Vocab.next_review_at.is_(None)) | (Vocab.next_review_at <= now),
    )
    voice_seconds = await session.scalar(
        select(func.coalesce(func.sum(Usage.audio_seconds), 0)).where(
            Usage.user_id == user.id, Usage.kind == "stt"
        )
    )
    latest = await session.scalar(
        select(Lesson)
        .where(
            Lesson.user_id == user.id,
            func.jsonb_typeof(Lesson.payload["insights"]) == "object",
        )
        .order_by(Lesson.id.desc())
        .limit(1)
    )
    insights = None
    if latest is not None:
        insights = {**latest.payload["insights"], "date": latest.created_at.date().isoformat()}

    return {
        "level": {"self": user.level, "estimated": user.estimated_level},
        "activity": {
            "streak": streak(active, today),
            "active_days_7": sum(1 for d in active if (today - d).days < 7),
            "active_days_30": sum(1 for d in active if (today - d).days < 30),
            "days": sorted(d.isoformat() for d in active if (today - d).days < 28),
        },
        "totals": {
            "messages": await _count(session, Message.id, *is_user_msg),
            "voice_minutes": round(float(voice_seconds or 0) / 60, 1),
            "lessons_done": await _count(
                session, Lesson.id, Lesson.user_id == user.id, Lesson.status == "done"
            ),
            "words": sum(vocab.values()),
            "words_known": vocab.get("known", 0),
            "words_learning": vocab.get("learning", 0),
            "words_new": vocab.get("new", 0),
            "words_due": words_due,
        },
        "mistake_rate": {
            "week": per_ten(mistakes_week, messages_week),
            "prev_week": per_ten(mistakes_prev, messages_prev),
            "messages_week": messages_week,
        },
        "topics": topics,
        "mistakes_by_type": sorted(by_type, key=lambda e: (-e["count"], -e["prev"])),
        "recurring": [
            {"original": m.original, "corrected": m.corrected, "times_seen": m.times_seen}
            for m in recurring
        ],
        "insights": insights,
    }
