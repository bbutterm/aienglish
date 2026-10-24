import random
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import Lesson, Mistake, Vocab
from app.services.lessons import Material, build_payload, fallback_payload, public_view
from app.services.srs import review_card


def material() -> Material:
    return Material(
        mistakes=[
            Mistake(
                id=7,
                original="I go to cinema yesterday",
                corrected="I went to the cinema yesterday",
                type="tense",
                explanation_ru="Прошедшее время",
            )
        ],
        vocab=[Vocab(id=3, term="blockbuster", translation_ru="блокбастер")],
    )


RAW = {
    "title": "Вчерашний день",
    "topic": "Weekend movies",
    "intro_ru": "Повторим прошедшее время.",
    "exercises": [
        {
            "type": "fix",
            "mistake_id": 7,
            "options": ["I went to the cinema yesterday", "I go to cinema yesterday", "I goes"],
            "answer": 0,
            "explanation_ru": "go → went",
        },
        {"type": "gap", "sentence": "I ____ it.", "options": ["see", "saw", "seen"], "answer": 1},
        {
            "type": "translate",
            "ru": "Я смотрел фильм",
            "options": ["I watched a film", "I watch film"],
            "answer": "I watched a film",
        },
        {
            "type": "listen",
            "text": "I saw a great film",
            "options": ["I saw a great film.", "I sew a grey film", "Eye saw"],
            "answer": 0,
        },
        {"type": "fix", "mistake_id": 999, "options": ["a", "b"], "answer": 1},
        {"type": "gap", "sentence": "no blank here", "options": ["a", "b"], "answer": 0},
        {"type": "unknown", "options": ["a", "b"], "answer": 0},
        {"type": "translate", "ru": "x", "options": ["a", "A"], "answer": 0},
        {"type": "translate", "ru": "x", "options": ["a", "b"], "answer": 5},
    ],
    "cards": [
        {"vocab_id": 3, "example": "It was a real blockbuster."},
        {"vocab_id": 42, "example": "?"},
    ],
    "insights": {
        "strengths": ["Смелые фразы", ""],
        "weaknesses": ["Времена"],
        "tip_ru": "Пиши про вчера",
    },
}


def test_build_payload_validates_and_shuffles() -> None:
    payload = build_payload(RAW, material(), random.Random(1))
    exercises = payload["exercises"]
    assert [e["type"] for e in exercises] == ["fix", "gap", "translate", "listen", "fix"]
    # после перемешивания ответ указывает на тот же текст
    assert exercises[0]["options"][exercises[0]["answer"]] == "I went to the cinema yesterday"
    assert exercises[0]["mistake_id"] == 7
    assert exercises[4]["mistake_id"] is None  # чужой id не привязываем
    assert exercises[1]["sentence"] == "I ___ it."
    assert exercises[2]["options"][exercises[2]["answer"]] == "I watched a film"
    listen = exercises[3]
    assert listen["options"][listen["answer"]] == "I saw a great film"
    assert payload["cards"] == [
        {
            "vocab_id": 3,
            "term": "blockbuster",
            "translation_ru": "блокбастер",
            "example": "It was a real blockbuster.",
        }
    ]
    assert payload["insights"]["strengths"] == ["Смелые фразы"]
    assert payload["based_on"] == {"mistakes": 1, "words": 1}


def test_build_payload_rejects_too_few_exercises() -> None:
    with pytest.raises(ValueError):
        build_payload({"exercises": RAW["exercises"][4:]}, material(), random.Random(1))


def test_fallback_payload() -> None:
    payload = fallback_payload(material(), random.Random(2))
    fix = payload["exercises"][0]
    assert set(fix["options"]) == {"I go to cinema yesterday", "I went to the cinema yesterday"}
    assert fix["options"][fix["answer"]] == "I went to the cinema yesterday"
    assert fallback_payload(Material([], []), random.Random()) is None


def test_public_view_hides_answers_until_answered() -> None:
    payload = build_payload(RAW, material(), random.Random(1))
    listen_index = 3
    lesson = Lesson(
        id=1, payload=payload, answers={"0": {"choice": 0, "correct": True}}, status="new"
    )
    view = public_view(lesson)
    assert "answer" not in view["exercises"][1] and "result" not in view["exercises"][1]
    assert "text" not in view["exercises"][listen_index]
    result = view["exercises"][0]["result"]
    assert result["answer"] == payload["exercises"][0]["answer"]
    assert result["explanation_ru"] == "go → went"


def test_srs_review() -> None:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    card = Vocab(term="x", ease=2.5, reps=0, interval_days=0, status="new")
    review_card(card, "good", now)
    assert (card.reps, card.interval_days, card.status) == (1, 1.0, "learning")
    review_card(card, "good", now)
    assert card.interval_days == 3.0
    review_card(card, "good", now)
    assert card.interval_days == 7.5
    assert card.next_review_at == now + timedelta(days=7.5)
    review_card(card, "again", now)
    assert (card.reps, card.interval_days, card.ease, card.next_review_at) == (0, 0, 2.3, now)
    card.interval_days, card.reps = 10, 5
    review_card(card, "good", now)
    assert card.status == "known"  # 10 * 2.3 = 23 дня
    with pytest.raises(ValueError):
        review_card(card, "meh", now)
