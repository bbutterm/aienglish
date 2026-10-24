"""Аналитика и управление пользователями для админки Mini App."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, cast, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Lesson, Message, Mistake, Usage, User
from app.services.plans import is_premium, limits_for
from app.services.progress import build_progress

USERS_PAGE = 30


def _utc_day(column: Any) -> Any:
    return cast(func.timezone("UTC", column), Date)


def _money(value: Decimal | float | None) -> float:
    return round(float(value or 0), 4)


async def _scalar(session: AsyncSession, stmt: Any) -> int:
    return (await session.scalar(stmt)) or 0


async def overview(
    session: AsyncSession, days: int = 14, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=30)
    first_day = now.date() - timedelta(days=days - 1)
    series_start = datetime.combine(first_day, datetime.min.time(), tzinfo=UTC)
    user_msg = Message.role == "user"

    async def active_since(delta: timedelta) -> int:
        return await _scalar(
            session,
            select(func.count(distinct(Message.user_id))).where(
                user_msg, Message.created_at >= now - delta
            ),
        )

    series: dict[date, dict[str, Any]] = {
        first_day + timedelta(days=i): {
            "date": (first_day + timedelta(days=i)).isoformat(),
            "active_users": 0,
            "new_users": 0,
            "messages": 0,
            "lessons": 0,
            "cost_usd": 0.0,
        }
        for i in range(days)
    }

    async def fill(key: str, stmt: Any) -> None:
        for day, value in await session.execute(stmt):
            if day in series:
                series[day][key] = _money(value) if key == "cost_usd" else int(value)

    day = _utc_day(Message.created_at)
    await fill(
        "active_users",
        select(day, func.count(distinct(Message.user_id)))
        .where(user_msg, Message.created_at >= series_start)
        .group_by(day),
    )
    await fill(
        "messages",
        select(day, func.count(Message.id))
        .where(user_msg, Message.created_at >= series_start)
        .group_by(day),
    )
    day = _utc_day(User.created_at)
    await fill(
        "new_users",
        select(day, func.count(User.id)).where(User.created_at >= series_start).group_by(day),
    )
    day = _utc_day(Lesson.created_at)
    await fill(
        "lessons",
        select(day, func.count(Lesson.id)).where(Lesson.created_at >= series_start).group_by(day),
    )
    day = _utc_day(Usage.created_at)
    await fill(
        "cost_usd",
        select(day, func.sum(Usage.cost_usd)).where(Usage.created_at >= series_start).group_by(day),
    )

    cost_rows = await session.execute(
        select(Usage.kind, func.count(Usage.id), func.sum(Usage.cost_usd))
        .where(Usage.created_at >= since)
        .group_by(Usage.kind)
        .order_by(func.sum(Usage.cost_usd).desc().nulls_last())
    )
    cost_by_kind = [
        {"kind": kind, "count": count, "cost_usd": _money(cost)} for kind, count, cost in cost_rows
    ]
    total_cost = sum(item["cost_usd"] for item in cost_by_kind)

    top_rows = await session.execute(
        select(User, func.sum(Usage.cost_usd).label("cost"))
        .join(Usage, Usage.user_id == User.id)
        .where(Usage.created_at >= since)
        .group_by(User.id)
        .order_by(func.sum(Usage.cost_usd).desc().nulls_last())
        .limit(5)
    )
    mau = await active_since(timedelta(days=30))
    lessons_done = await _scalar(
        session,
        select(func.count(Lesson.id)).where(Lesson.created_at >= since, Lesson.status == "done"),
    )
    return {
        "users": {
            "total": await _scalar(session, select(func.count(User.id))),
            "onboarded": await _scalar(
                session, select(func.count(User.id)).where(User.onboarded.is_(True))
            ),
            "premium": await _scalar(
                session, select(func.count(User.id)).where(User.premium_until > now)
            ),
            "blocked": await _scalar(
                session, select(func.count(User.id)).where(User.blocked.is_(True))
            ),
            "dau": await active_since(timedelta(days=1)),
            "wau": await active_since(timedelta(days=7)),
            "mau": mau,
        },
        "last_30d": {
            "messages": await _scalar(
                session, select(func.count(Message.id)).where(user_msg, Message.created_at >= since)
            ),
            "lessons": await _scalar(
                session, select(func.count(Lesson.id)).where(Lesson.created_at >= since)
            ),
            "lessons_done": lessons_done,
            "cost_usd": round(total_cost, 4),
            "cost_per_active_user": round(total_cost / mau, 4) if mau else None,
        },
        "series": list(series.values()),
        "cost_by_kind": cost_by_kind,
        "top_users": [
            {"id": user.id, "name": user.name, "username": user.username, "cost_usd": _money(cost)}
            for user, cost in top_rows
        ],
    }


def _user_row(user: User, now: datetime) -> dict[str, Any]:
    return {
        "id": user.id,
        "name": user.name,
        "username": user.username,
        "level": user.level,
        "onboarded": user.onboarded,
        "premium": is_premium(user, now),
        "premium_until": user.premium_until.isoformat() if user.premium_until else None,
        "blocked": user.blocked,
        "last_active_at": user.last_active_at.isoformat() if user.last_active_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


async def list_users(
    session: AsyncSession, query: str = "", offset: int = 0, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=30)
    conditions = []
    query = query.strip().lstrip("@")
    if query:
        pattern = f"%{query}%"
        match = [
            User.username.ilike(pattern),
            User.first_name.ilike(pattern),
            User.display_name.ilike(pattern),
        ]
        if query.isdigit():
            match.append(User.id == int(query))
        conditions.append(or_(*match))

    messages_30d = (
        select(func.count(Message.id))
        .where(Message.user_id == User.id, Message.role == "user", Message.created_at >= since)
        .scalar_subquery()
    )
    cost_30d = (
        select(func.coalesce(func.sum(Usage.cost_usd), 0))
        .where(Usage.user_id == User.id, Usage.created_at >= since)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(User, messages_30d, cost_30d)
        .where(*conditions)
        .order_by(User.last_active_at.desc().nulls_last(), User.id.desc())
        .offset(max(offset, 0))
        .limit(USERS_PAGE)
    )
    total = await _scalar(session, select(func.count(User.id)).where(*conditions))
    return {
        "total": total,
        "offset": offset,
        "items": [
            {**_user_row(user, now), "messages_30d": messages, "cost_30d": _money(cost)}
            for user, messages, cost in rows
        ],
    }


async def user_detail(
    session: AsyncSession, user: User, settings: Settings, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=30)
    limits = limits_for(user, settings, now)
    costs = await session.execute(
        select(
            Usage.kind,
            func.sum(Usage.cost_usd),
            func.sum(Usage.cost_usd).filter(Usage.created_at >= since),
        )
        .where(Usage.user_id == user.id)
        .group_by(Usage.kind)
        .order_by(Usage.kind)
    )
    mistakes = await session.scalars(
        select(Mistake).where(Mistake.user_id == user.id).order_by(Mistake.id.desc()).limit(10)
    )
    lessons = await session.scalars(
        select(Lesson).where(Lesson.user_id == user.id).order_by(Lesson.id.desc()).limit(10)
    )
    cost_by_kind = [
        {"kind": kind, "cost_usd": _money(total), "cost_30d": _money(recent)}
        for kind, total, recent in costs
    ]
    return {
        **_user_row(user, now),
        "first_name": user.first_name,
        "estimated_level": user.estimated_level,
        "mode": user.mode,
        "persona": user.persona,
        "voice_mode": user.voice_mode,
        "limits": {"messages": limits.messages, "tts": limits.tts, "lessons": limits.lessons},
        "progress": await build_progress(session, user, now),
        "cost_by_kind": cost_by_kind,
        "cost_total": round(sum(c["cost_usd"] for c in cost_by_kind), 4),
        "cost_30d": round(sum(c["cost_30d"] for c in cost_by_kind), 4),
        "mistakes": [
            {
                "original": m.original,
                "corrected": m.corrected,
                "type": m.type,
                "times_seen": m.times_seen,
                "created_at": m.created_at.isoformat(),
            }
            for m in mistakes
        ],
        "lessons": [
            {
                "id": lesson.id,
                "title": lesson.payload.get("title"),
                "status": lesson.status,
                "score": lesson.score,
                "total": lesson.total,
                "created_at": lesson.created_at.isoformat(),
            }
            for lesson in lessons
        ],
    }
