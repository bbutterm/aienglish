import json

import pytest

from app.services.analyst import build_analyst_input, parse_analysis


def test_parse_valid_analysis() -> None:
    raw = json.dumps(
        {
            "mistakes": [
                {
                    "original": "I go to cinema yesterday",
                    "corrected": "I went to the cinema yesterday",
                    "type": "tense",
                    "explanation_ru": "Прошедшее время",
                    "severity": 2,
                }
            ],
            "vocab": [{"term": "Blockbuster", "translation_ru": "блокбастер", "source": "bot"}],
            "estimated_level": "b1",
        }
    )
    analysis = parse_analysis(raw)
    assert len(analysis.mistakes) == 1
    assert analysis.mistakes[0].corrected == "I went to the cinema yesterday"
    assert analysis.mistakes[0].severity == 2
    assert analysis.vocab[0].term == "blockbuster"
    assert analysis.estimated_level == "B1"


def test_parse_tolerates_fences_and_garbage_items() -> None:
    raw = """```json
    {"mistakes": [
        {"original": "", "corrected": "x"},
        {"original": "same", "corrected": "Same"},
        "nonsense",
        {"original": "he go", "corrected": "he goes", "type": "weird", "severity": 9},
        {"original": "a", "corrected": "b", "severity": "high"}
    ],
     "vocab": [{"term": "cosy"}, {"term": "Cosy"}, {"term": "x", "source": "alien"}],
     "estimated_level": "expert"}
    ```"""
    analysis = parse_analysis(raw)
    assert [(m.original, m.type, m.severity) for m in analysis.mistakes] == [
        ("he go", "other", 3),
        ("a", "other", 1),
    ]
    assert [(v.term, v.source) for v in analysis.vocab] == [("cosy", "bot"), ("x", "bot")]
    assert analysis.estimated_level is None


def test_parse_empty_arrays() -> None:
    analysis = parse_analysis('{"mistakes": [], "vocab": [], "estimated_level": null}')
    assert analysis.mistakes == [] and analysis.vocab == []


def test_parse_rejects_non_json() -> None:
    with pytest.raises(ValueError):
        parse_analysis("Sorry, I can't help with that.")


def test_build_analyst_input() -> None:
    text = build_analyst_input(
        user_text='I said "hi"', prev_bot_text="How are you?", bot_reply=None, is_voice=True
    )
    assert text.splitlines() == [
        'Friend\'s previous message: "How are you?"',
        'Learner\'s message (voice transcript): "I said \\"hi\\""',
    ]
