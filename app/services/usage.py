from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Message, Usage, User
from app.llm.openrouter import LLMResult


def _to_decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, 6)))


def stt_cost(seconds: float, settings: Settings) -> float:
    return seconds / 3600 * settings.stt_usd_per_hour


def tts_cost(chars: int, settings: Settings) -> float:
    return chars / 1000 * settings.tts_usd_per_1k_chars


async def record_usage(
    session: AsyncSession,
    *,
    user_id: int | None,
    kind: str,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    audio_seconds: float | None = None,
    chars: int | None = None,
    cost_usd: float | None = None,
) -> None:
    session.add(
        Usage(
            user_id=user_id,
            kind=kind,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            audio_seconds=audio_seconds,
            chars=chars,
            cost_usd=_to_decimal(cost_usd),
        )
    )


async def record_llm(
    session: AsyncSession, user_id: int | None, kind: str, result: LLMResult
) -> None:
    await record_usage(
        session,
        user_id=user_id,
        kind=kind,
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
    )


@dataclass
class PeriodStats:
    user_messages: int = 0
    cost_by_kind: dict[str, tuple[int, Decimal]] = field(default_factory=dict)

    @property
    def total_cost(self) -> Decimal:
        return sum((cost for _, cost in self.cost_by_kind.values()), Decimal(0))


@dataclass
class Stats:
    users_total: int
    users_onboarded: int
    users_active_day: int
    users_new_day: int
    day: PeriodStats
    month: PeriodStats


async def _period(session: AsyncSession, since: datetime) -> PeriodStats:
    stats = PeriodStats()
    stats.user_messages = (
        await session.scalar(
            select(func.count(Message.id)).where(
                Message.role == "user", Message.created_at >= since
            )
        )
    ) or 0
    rows = await session.execute(
        select(Usage.kind, func.count(Usage.id), func.coalesce(func.sum(Usage.cost_usd), 0))
        .where(Usage.created_at >= since)
        .group_by(Usage.kind)
        .order_by(Usage.kind)
    )
    for kind, count, cost in rows:
        stats.cost_by_kind[kind] = (count, Decimal(cost))
    return stats


async def collect_stats(session: AsyncSession, now: datetime | None = None) -> Stats:
    now = now or datetime.now(UTC)
    day_ago = now - timedelta(days=1)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async def count(*conditions: object) -> int:
        return (await session.scalar(select(func.count(User.id)).where(*conditions))) or 0

    return Stats(
        users_total=await count(),
        users_onboarded=await count(User.onboarded.is_(True)),
        users_active_day=await count(User.last_active_at >= day_ago),
        users_new_day=await count(User.created_at >= day_ago),
        day=await _period(session, day_ago),
        month=await _period(session, month_start),
    )


def format_stats(stats: Stats) -> str:
    def period(title: str, data: PeriodStats) -> list[str]:
        lines = [f"<b>{title}</b>", f"Реплик пользователей: {data.user_messages}"]
        for kind, (count, cost) in data.cost_by_kind.items():
            lines.append(f"• {kind}: {count} шт · ${cost:.4f}")
        lines.append(f"Итого: <b>${data.total_cost:.4f}</b>")
        return lines

    lines = [
        "📊 <b>Статистика</b>",
        f"Пользователей: {stats.users_total} (прошли онбординг: {stats.users_onboarded})",
        f"Активных за сутки: {stats.users_active_day}, новых: {stats.users_new_day}",
        "",
        *period("За 24 часа", stats.day),
        "",
        *period("С начала месяца (UTC)", stats.month),
    ]
    return "\n".join(lines)
