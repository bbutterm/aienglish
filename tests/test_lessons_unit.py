import random
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import Lesson, Message, Mistake, Vocab
from app.services.analyst import clean_topic
from app.services.lessons import (
    Material,
    build_payload,
    conversation_transcript,
    fallback_payload,
    public_view,
)
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
        messages=[
            Message(role="user", kind="voice", text="I go to cinema yesterday", topic="кино"),
            Message(role="assistant", kind="text", text="Nice! What did you watch?"),
            Message(role="user", kind="photo", text=None, photo_caption="a cinema ticket"),
        ],
        topics=["кино"],
    )


RAW = {
    "title": "Вчерашний день",
    "topic": "Weekend movies",
    "topic_ru": "  Кино ",
    "intro_ru": "Повторим прошедшее время.",
    "topic_block": {
        "summary_ru": "Ты рассказывал про поход в кино.",
        "phrases": [
            {
                "en": "to go to the cinema",
                "ru": "сходить в кино",
                "example": "We went to the cinema.",
            },
            {"en": "  ", "ru": "пусто"},
            "not a phrase",
        ],
    },
    "grammar": [
        {
            "title_ru": "Past Simple",
            "rule_ru": "Законченное действие в прошлом — Past Simple.",
            "examples": [{"en": "I went home.", "ru": "Я пошёл домой."}, {"ru": "без en"}],
            "mistake_ids": [7, 999],
        },
        {"title_ru": "Без текста правила"},
        {"title_ru": "Артикли", "rule_ru": "the cinema — с артиклем."},
        {"title_ru": "Третье лишнее", "rule_ru": "Больше двух правил не берём."},
    ],
    "exercises": [
        {
            "type": "fix",
            "mistake_id": 7,
            "options": ["I went to the cinema yesterday", "I go to cinema yesterday", "I goes"],
            "answer": 0,
            "explanation_ru": "go → went",
        },
        {
            "type": "gap",
            "section": "grammar",
            "sentence": "I ____ it.",
            "options": ["see", "saw", "seen"],
            "answer": 1,
        },
        {
            "type": "translate",
            "ru": "Я смотрел фильм",
            "options": ["I watched a film", "I watch film"],
            "answer": "I watched a film",
        },
        {
            "type": "listen",
            "section": "topic",
            "text": "I saw a great film",
            "options": ["I saw a great film.", "I sew a grey film", "Eye saw"],
            "answer": 0,
        },
        {"type": "fix", "section": "bogus", "mistake_id": 999, "options": ["a", "b"], "answer": 1},
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
    # сначала тема разговора, потом ошибки и грамматика; без раздела fix — грамматика, прочее — тема
    assert [(e["section"], e["type"]) for e in exercises] == [
        ("topic", "translate"),
        ("topic", "listen"),
        ("grammar", "fix"),
        ("grammar", "gap"),
        ("grammar", "fix"),
    ]
    translate, listen, fix, gap, foreign_fix = exercises
    # после перемешивания ответ указывает на тот же текст
    assert fix["options"][fix["answer"]] == "I went to the cinema yesterday"
    assert fix["mistake_id"] == 7
    assert foreign_fix["mistake_id"] is None  # чужой id не привязываем
    assert gap["sentence"] == "I ___ it."
    assert translate["options"][translate["answer"]] == "I watched a film"
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
    assert payload["based_on"] == {"mistakes": 1, "words": 1, "messages": 2, "topics": ["кино"]}


def test_build_payload_topic_and_grammar_parts() -> None:
    payload = build_payload(RAW, material(), random.Random(1))
    assert payload["version"] == 2
    assert payload["topic_ru"] == "Кино"
    assert payload["topic_block"] == {
        "summary_ru": "Ты рассказывал про поход в кино.",
        "phrases": [
            {
                "en": "to go to the cinema",
                "ru": "сходить в кино",
                "example": "We went to the cinema.",
            }
        ],
    }
    # правило без текста отброшено, больше двух не берём, чужие id ошибок не привязываем
    assert payload["grammar"] == [
        {
            "title_ru": "Past Simple",
            "rule_ru": "Законченное действие в прошлом — Past Simple.",
            "examples": [{"en": "I went home.", "ru": "Я пошёл домой."}],
            "mistake_ids": [7],
        },
        {
            "title_ru": "Артикли",
            "rule_ru": "the cinema — с артиклем.",
            "examples": [],
            "mistake_ids": [],
        },
    ]
    assert payload["mistakes_review"] == [
        {
            "id": 7,
            "original": "I go to cinema yesterday",
            "corrected": "I went to the cinema yesterday",
            "explanation_ru": "Прошедшее время",
            "type_ru": "времена",
        }
    ]


def test_build_payload_topic_defaults_to_conversation() -> None:
    raw = {k: v for k, v in RAW.items() if k not in {"topic_ru", "topic_block", "grammar"}}
    payload = build_payload(raw, material(), random.Random(1))
    assert payload["topic_ru"] == "кино"
    assert payload["topic_block"] is None and payload["grammar"] == []


def test_conversation_transcript() -> None:
    transcript = conversation_transcript(material().messages, "Alex")
    assert transcript == (
        "Learner: I go to cinema yesterday\n"
        "Friend: Nice! What did you watch?\n"
        "Learner: [photo: a cinema ticket]"
    )


def test_clean_topic() -> None:
    assert clean_topic(" Компьютеры. ") == "компьютеры"
    assert clean_topic("«Работа и карьера в IT компании»") == "работа и карьера"
    assert clean_topic("null") is None and clean_topic("") is None and clean_topic(3) is None


def test_build_payload_rejects_too_few_exercises() -> None:
    with pytest.raises(ValueError):
        build_payload({"exercises": RAW["exercises"][4:]}, material(), random.Random(1))


def test_fallback_payload() -> None:
    payload = fallback_payload(material(), random.Random(2))
    fix = payload["exercises"][0]
    assert fix["section"] == "grammar" and payload["topic_ru"] == "кино"
    assert [m["id"] for m in payload["mistakes_review"]] == [7]
    assert set(fix["options"]) == {"I go to cinema yesterday", "I went to the cinema yesterday"}
    assert fix["options"][fix["answer"]] == "I went to the cinema yesterday"
    assert fallback_payload(Material([], []), random.Random()) is None


def test_public_view_hides_answers_until_answered() -> None:
    payload = build_payload(RAW, material(), random.Random(1))
    listen_index, fix_index = 1, 2
    lesson = Lesson(
        id=1,
        payload=payload,
        answers={str(fix_index): {"choice": 0, "correct": True}},
        status="new",
    )
    view = public_view(lesson)
    assert "answer" not in view["exercises"][0] and "result" not in view["exercises"][0]
    assert "text" not in view["exercises"][listen_index]
    assert [e["section"] for e in view["exercises"]] == ["topic"] * 2 + ["grammar"] * 3
    result = view["exercises"][fix_index]["result"]
    assert result["answer"] == payload["exercises"][fix_index]["answer"]
    assert result["explanation_ru"] == "go → went"
    assert view["topic_block"] == payload["topic_block"] and view["grammar"] == payload["grammar"]
    assert view["mistakes_review"] == payload["mistakes_review"]


def test_public_view_of_old_lesson() -> None:
    """Уроки, собранные до разделов (version 1), открываются как раньше."""
    payload = build_payload(RAW, material(), random.Random(1))
    old = {
        "title": payload["title"],
        "exercises": [{k: v for k, v in e.items() if k != "section"} for e in payload["exercises"]],
    }
    view = public_view(Lesson(id=2, payload=old, answers={}, status="new"))
    assert view["topic_block"] is None and view["mistakes_review"] == [] and view["grammar"] == []
    assert {e["section"] for e in view["exercises"]} == {"grammar"}


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
