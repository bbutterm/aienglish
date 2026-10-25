from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, Mistake, Usage, User, Vocab


def day_start_utc(tz_name: str | None, now: datetime | None = None) -> datetime:
    """Начало текущих суток в часовом поясе пользователя, в UTC."""
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    local = (now or datetime.now(UTC)).astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


# --- users ---


async def upsert_user(
    session: AsyncSession, tg_id: int, username: str | None, first_name: str | None
) -> User:
    now = datetime.now(UTC)
    stmt = (
        pg_insert(User)
        .values(id=tg_id, username=username, first_name=first_name, last_active_at=now)
        .on_conflict_do_update(
            index_elements=[User.id],
            set_={"username": username, "first_name": first_name, "last_active_at": now},
        )
        .returning(User)
    )
    result = await session.scalars(stmt, execution_options={"populate_existing": True})
    user = result.one()
    await session.commit()
    return user


# --- messages ---


async def add_message(
    session: AsyncSession,
    *,
    user_id: int,
    role: str,
    kind: str,
    text: str | None = None,
    photo_caption: str | None = None,
    tg_message_id: int | None = None,
) -> Message:
    message = Message(
        user_id=user_id,
        role=role,
        kind=kind,
        text=text,
        photo_caption=photo_caption,
        tg_message_id=tg_message_id,
    )
    session.add(message)
    await session.flush()
    return message


async def get_history(session: AsyncSession, user: User, limit: int) -> list[Message]:
    """Последние `limit` сообщений живого контекста (после context_start_id), по порядку."""
    stmt = (
        select(Message)
        .where(Message.user_id == user.id, Message.id > user.context_start_id)
        .order_by(Message.id.desc())
        .limit(limit)
    )
    rows = list((await session.scalars(stmt)).all())
    rows.reverse()
    return rows


async def get_live_messages(session: AsyncSession, user: User) -> list[Message]:
    stmt = (
        select(Message)
        .where(Message.user_id == user.id, Message.id > user.context_start_id)
        .order_by(Message.id)
    )
    return list((await session.scalars(stmt)).all())


async def count_live_messages(session: AsyncSession, user: User) -> int:
    stmt = select(func.count(Message.id)).where(
        Message.user_id == user.id, Message.id > user.context_start_id
    )
    return (await session.scalar(stmt)) or 0


async def last_message_id(session: AsyncSession, user_id: int) -> int:
    stmt = select(func.max(Message.id)).where(Message.user_id == user_id)
    return (await session.scalar(stmt)) or 0


async def count_user_messages_since(session: AsyncSession, user_id: int, since: datetime) -> int:
    stmt = select(func.count(Message.id)).where(
        Message.user_id == user_id, Message.role == "user", Message.created_at >= since
    )
    return (await session.scalar(stmt)) or 0


# --- usage ---


async def count_usage_since(session: AsyncSession, user_id: int, kind: str, since: datetime) -> int:
    stmt = select(func.count(Usage.id)).where(
        Usage.user_id == user_id, Usage.kind == kind, Usage.created_at >= since
    )
    return (await session.scalar(stmt)) or 0


# --- mistakes / vocab ---


@dataclass(frozen=True)
class MistakeItem:
    original: str
    corrected: str
    type: str
    explanation_ru: str | None
    severity: int


@dataclass(frozen=True)
class VocabItem:
    term: str
    translation_ru: str | None
    source: str
    example: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class UpgradeItem:
    basic: str  # что сказал ученик: very big
    better: str  # как сказать сильнее: huge
    translation_ru: str | None
    example: str | None  # фраза ученика с новым словом

    def as_vocab(self) -> VocabItem:
        return VocabItem(
            term=self.better,
            translation_ru=self.translation_ru,
            source="upgrade",
            example=self.example,
            note=self.basic,
        )


async def save_mistakes(
    session: AsyncSession, user_id: int, message_id: int | None, items: Sequence[MistakeItem]
) -> None:
    for item in items:
        existing = await session.scalar(
            select(Mistake)
            .where(
                Mistake.user_id == user_id,
                func.lower(Mistake.original) == item.original.lower(),
                func.lower(Mistake.corrected) == item.corrected.lower(),
            )
            .limit(1)
        )
        if existing is not None:
            existing.times_seen += 1
            existing.resolved = False
            continue
        session.add(
            Mistake(
                user_id=user_id,
                message_id=message_id,
                original=item.original,
                corrected=item.corrected,
                type=item.type,
                explanation_ru=item.explanation_ru,
                severity=item.severity,
            )
        )
    await session.flush()


async def save_vocab(session: AsyncSession, user_id: int, items: Sequence[VocabItem]) -> None:
    if not items:
        return
    stmt = (
        pg_insert(Vocab)
        .values(
            [
                {
                    "user_id": user_id,
                    "term": item.term,
                    "translation_ru": item.translation_ru,
                    "source": item.source,
                    "example": item.example,
                    "note": item.note,
                }
                for item in items
            ]
        )
        .on_conflict_do_nothing(constraint="uq_vocab_user_term")
    )
    await session.execute(stmt)


async def recent_mistakes(session: AsyncSession, user_id: int, limit: int) -> list[Mistake]:
    stmt = (
        select(Mistake).where(Mistake.user_id == user_id).order_by(Mistake.id.desc()).limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def mistakes_since(
    session: AsyncSession, user_id: int, since: datetime | None, limit: int
) -> list[Mistake]:
    stmt = select(Mistake).where(Mistake.user_id == user_id)
    if since is not None:
        stmt = stmt.where(Mistake.created_at > since)
    stmt = stmt.order_by(Mistake.severity.desc(), Mistake.id.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())
