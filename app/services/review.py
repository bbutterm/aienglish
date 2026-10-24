from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from html import escape

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repo
from app.db.models import Mistake, User

TYPE_LABELS_RU = {
    "tense": "времена",
    "article": "артикли",
    "preposition": "предлоги",
    "word_choice": "выбор слова",
    "word_order": "порядок слов",
    "spelling": "орфография",
    "grammar": "грамматика",
    "other": "другое",
}

REVIEW_LIMIT = 10
MAX_MESSAGE_LEN = 4000


def format_mistake(mistake: Mistake) -> str:
    line = f"❌ {escape(mistake.original)}\n✅ <b>{escape(mistake.corrected)}</b>"
    if mistake.times_seen > 1:
        line += f"  <i>(×{mistake.times_seen})</i>"
    if mistake.explanation_ru:
        line += f"\n💡 <i>{escape(mistake.explanation_ru)}</i>"
    return line


def format_mistakes(title: str, mistakes: Sequence[Mistake]) -> str:
    blocks = [title]
    length = len(title)
    for mistake in mistakes:
        block = format_mistake(mistake)
        if length + len(block) + 2 > MAX_MESSAGE_LEN:
            break
        blocks.append(block)
        length += len(block) + 2
    return "\n\n".join(blocks)


def format_review(mistakes: Sequence[Mistake]) -> str:
    counts = Counter(m.type for m in mistakes)
    top = ", ".join(
        f"{TYPE_LABELS_RU.get(kind, kind)} — {count}" for kind, count in counts.most_common(3)
    )
    title = f"📝 <b>Разбор сессии</b>\nЧаще всего: {top}"
    return format_mistakes(title, mistakes)


async def build_review(session: AsyncSession, user: User) -> str | None:
    """Разбор ошибок с прошлого разбора. None — новых ошибок нет."""
    mistakes = await repo.mistakes_since(session, user.id, user.last_review_at, REVIEW_LIMIT)
    user.last_review_at = datetime.now(UTC)
    await session.commit()
    if not mistakes:
        return None
    return format_review(mistakes)
