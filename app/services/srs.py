"""Интервальное повторение слов: упрощённый SM-2 с двумя оценками — «не помню» и «помню»."""

from datetime import datetime, timedelta

from app.db.models import Vocab

GRADES = ("again", "good")
MIN_EASE = 1.3
KNOWN_AFTER_DAYS = 21


def review_card(card: Vocab, grade: str, now: datetime) -> None:
    if grade not in GRADES:
        raise ValueError(f"unknown grade {grade!r}")
    if grade == "again":
        card.reps = 0
        card.ease = max(MIN_EASE, round(card.ease - 0.2, 2))
        card.interval_days = 0
        card.next_review_at = now  # снова в очереди на повторение
        card.status = "learning"
        return
    card.reps += 1
    if card.reps == 1:
        interval = 1.0
    elif card.reps == 2:
        interval = 3.0
    else:
        interval = round(max(card.interval_days, 1.0) * card.ease, 1)
    card.interval_days = interval
    card.next_review_at = now + timedelta(days=interval)
    card.status = "known" if interval >= KNOWN_AFTER_DAYS else "learning"
