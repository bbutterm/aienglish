import json
import logging
from dataclasses import dataclass, field

from app.config import Settings
from app.db.repo import MistakeItem, VocabItem
from app.llm.jsonutil import extract_json_object
from app.llm.openrouter import LLMResult, OpenRouterClient
from app.prompts import render

logger = logging.getLogger(__name__)

MISTAKE_TYPES = {
    "tense",
    "article",
    "preposition",
    "word_choice",
    "word_order",
    "spelling",
    "grammar",
    "other",
}
LEVELS = {"A1", "A2", "B1", "B2", "C1", "C2"}
MAX_MISTAKES = 5
MAX_VOCAB = 3


@dataclass
class Analysis:
    mistakes: list[MistakeItem] = field(default_factory=list)
    vocab: list[VocabItem] = field(default_factory=list)
    estimated_level: str | None = None


@dataclass(frozen=True)
class AnalystResult:
    analysis: Analysis
    llm: LLMResult


def _clean_str(value: object, limit: int = 300) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def parse_analysis(raw: str) -> Analysis:
    """Терпимый к мусору разбор JSON аналитика: невалидные элементы отбрасываются."""
    data = extract_json_object(raw)

    analysis = Analysis()
    for item in data.get("mistakes") or []:
        if not isinstance(item, dict):
            continue
        original = _clean_str(item.get("original"))
        corrected = _clean_str(item.get("corrected"))
        if not original or not corrected or original.lower() == corrected.lower():
            continue
        mistake_type = str(item.get("type") or "other").strip().lower()
        try:
            severity = int(item.get("severity") or 1)
        except (TypeError, ValueError):
            severity = 1
        analysis.mistakes.append(
            MistakeItem(
                original=original,
                corrected=corrected,
                type=mistake_type if mistake_type in MISTAKE_TYPES else "other",
                explanation_ru=_clean_str(item.get("explanation_ru"), 500),
                severity=min(max(severity, 1), 3),
            )
        )
        if len(analysis.mistakes) >= MAX_MISTAKES:
            break

    seen: set[str] = set()
    for item in data.get("vocab") or []:
        if not isinstance(item, dict):
            continue
        term = _clean_str(item.get("term"), 100)
        if not term or term.lower() in seen:
            continue
        seen.add(term.lower())
        source = str(item.get("source") or "bot").strip().lower()
        analysis.vocab.append(
            VocabItem(
                term=term.lower(),
                translation_ru=_clean_str(item.get("translation_ru"), 200),
                source=source if source in {"bot", "user", "photo"} else "bot",
            )
        )
        if len(analysis.vocab) >= MAX_VOCAB:
            break

    level = data.get("estimated_level")
    if isinstance(level, str) and level.strip().upper() in LEVELS:
        analysis.estimated_level = level.strip().upper()
    return analysis


def build_analyst_input(
    *, user_text: str, prev_bot_text: str | None, bot_reply: str | None, is_voice: bool
) -> str:
    parts = []
    if prev_bot_text:
        parts.append(f"Friend's previous message: {json.dumps(prev_bot_text, ensure_ascii=False)}")
    kind = "voice transcript" if is_voice else "text"
    parts.append(f"Learner's message ({kind}): {json.dumps(user_text, ensure_ascii=False)}")
    if bot_reply:
        parts.append(f"Friend's reply: {json.dumps(bot_reply, ensure_ascii=False)}")
    return "\n".join(parts)


class Analyst:
    def __init__(self, llm: OpenRouterClient, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings

    async def analyze(
        self,
        *,
        level: str,
        user_text: str,
        prev_bot_text: str | None,
        bot_reply: str | None,
        is_voice: bool,
    ) -> AnalystResult:
        messages = [
            {
                "role": "system",
                "content": render("analyst_system.md.j2", level=level, is_voice=is_voice),
            },
            {
                "role": "user",
                "content": build_analyst_input(
                    user_text=user_text,
                    prev_bot_text=prev_bot_text,
                    bot_reply=bot_reply,
                    is_voice=is_voice,
                ),
            },
        ]
        result = await self._llm.chat(
            messages,
            models=[self._settings.analyst_model],
            temperature=0.2,
            max_tokens=1500,
            json_mode=True,
            reasoning_effort=self._settings.analyst_reasoning_effort or None,
        )
        return AnalystResult(analysis=parse_analysis(result.text), llm=result)
