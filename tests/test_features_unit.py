"""Чистые функции включаемых фич: разбор ответов моделей, миссии, путь новичка."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import User
from app.services import journey
from app.services.analyst import parse_analysis
from app.services.ideas import clean_content, clean_ideas
from app.services.missions import apply_goals, is_complete, is_expired
from app.services.podcast import clean_script
from app.services.rewrite import parse_rewrite


def test_parse_upgrades_and_goals() -> None:
    raw = json.dumps(
        {
            "upgrades": [
                {"basic": "Very Good", "better": "Great", "translation_ru": "отличный"},
                {"basic": "nice", "better": "nice"},  # то же самое — отбрасываем
                {"basic": "bad", "better": "awful", "example": "It was awful."},
                {"basic": "big", "better": "huge"},  # больше двух не берём
            ],
            "goals_done": [0, 2, 2, 5, True, "1"],
        }
    )
    analysis = parse_analysis(raw, goals=3)
    assert [(u.basic, u.better) for u in analysis.upgrades] == [
        ("very good", "great"),
        ("bad", "awful"),
    ]
    assert analysis.upgrades[0].as_vocab().source == "upgrade"
    assert analysis.goals_done == [0, 2]
    assert parse_analysis(raw).goals_done == []  # нет миссии — цели игнорируем


def test_clean_ideas_and_content() -> None:
    ideas = clean_ideas(
        [
            {"kind": "talk", "title_ru": "«Твой лучший отпуск».", "focus_ru": "Past Simple"},
            {"kind": "talk", "title_ru": "твой лучший отпуск"},  # дубль
            {"kind": "mission", "title_ru": "Собеседование"},
            {"kind": "weird", "title_ru": "Странный тип → разговор"},
        ],
        missions=False,
    )
    assert [(i["kind"], i["title_ru"]) for i in ideas] == [
        ("talk", "Твой лучший отпуск"),
        ("talk", "Странный тип → разговор"),
    ]
    with pytest.raises(ValueError):
        clean_content({"role_en": "a barista", "setting_en": "cafe", "goals": []}, "mission")
    talk = clean_content({"questions": ["Why?", "", "How?", "4th", "5th"]}, "talk")
    assert talk["questions"] == ["Why?", "How?"]


def test_missions() -> None:
    mission = {"goals": [{"en": "a", "ru": "а"}] * 3, "done": [1]}
    mission, new = apply_goals(mission, [1, 2, 7])
    assert (mission["done"], new) == ([1, 2], [2])
    assert not is_complete(mission)
    mission, _ = apply_goals(mission, [0])
    assert is_complete(mission)
    old = datetime.now(UTC) - timedelta(hours=25)
    assert is_expired({"started_at": old.isoformat()})
    assert not is_expired({"started_at": datetime.now(UTC).isoformat()})


def test_clean_script() -> None:
    script = clean_script(
        {
            "title": "  ",
            "lines": [{"speaker": "Jake", "text": "Hi " * 10}] * 3
            + [{"speaker": "emma", "text": "x" * 1000}] * 3,
            "words": [{"en": "huge", "ru": "огромный"}, {"ru": "без en"}],
        }
    )
    assert script["title"] == "Твоя неделя"
    assert len(script["lines"]) == 6 and sum(len(line["text"]) for line in script["lines"]) <= 1800
    assert script["words"] == [{"en": "huge", "ru": "огромный"}]
    with pytest.raises(ValueError):
        clean_script({"lines": [{"speaker": "jake", "text": "only one"}]})


def test_parse_rewrite() -> None:
    rewrite = parse_rewrite(
        json.dumps({"native": "I agree.", "changes": [{"from": "am agree", "to": "agree"}]}),
        "I am agree",
    )
    assert rewrite.native == "I agree." and not rewrite.is_same("I am agree")
    same = parse_rewrite(json.dumps({"native": "Sounds good!", "changes": []}), "Sounds good")
    assert same.is_same("Sounds good")


def test_journey_hints_order() -> None:
    user = User(hints=[], level="B1")
    assert journey.next_hint(user, 2, has_insight=True, app=True) is None
    assert journey.next_hint(user, 3, has_insight=True, app=True) == journey.HINT_INSIGHT
    assert journey.next_hint(user, 5, has_insight=False, app=True) is None  # ждём первую ошибку
    assert journey.next_hint(user, 6, has_insight=False, app=True) == journey.HINT_LEVEL
    user.hints = [journey.HINT_LEVEL]
    assert journey.next_hint(user, 8, has_insight=False, app=True) == journey.HINT_APP
    assert journey.next_hint(user, 8, has_insight=False, app=False) is None
    assert journey.next_hint(user, 10, has_insight=False, app=False) == journey.HINT_INSIGHT
    assert journey.level_gap("B1", "A2") and not journey.level_gap("B1", "B1")
    assert not journey.level_gap("B1", None)
