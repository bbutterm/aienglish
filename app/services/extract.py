"""Слова в карточки из фото и пересланных английских текстов."""

import base64
import logging
from dataclasses import dataclass

from app.config import Settings
from app.db.models import User
from app.db.repo import VocabItem
from app.llm.jsonutil import extract_json_object
from app.llm.openrouter import LLMParseError, LLMResult, OpenRouterClient
from app.prompts import render

logger = logging.getLogger(__name__)

PHOTO_WORDS = 5
FORWARD_WORDS = 6
MIN_LATIN_LETTERS = 40


@dataclass
class Extracted:
    words: list[VocabItem]
    summary_ru: str | None
    llm: LLMResult


def looks_english(text: str | None) -> bool:
    """Пересланный текст стоит разбирать, если он в основном английский и не совсем короткий."""
    if not text:
        return False
    latin = sum(1 for c in text if "a" <= c.lower() <= "z")
    cyrillic = sum(1 for c in text if "а" <= c.lower() <= "я" or c.lower() == "ё")
    return latin >= MIN_LATIN_LETTERS and latin > 3 * cyrillic


def parse_words(raw: str, source: str, limit: int) -> tuple[list[VocabItem], str | None]:
    data = extract_json_object(raw)
    words: list[VocabItem] = []
    seen: set[str] = set()
    raw_words = data.get("words")
    for item in raw_words if isinstance(raw_words, list) else []:
        if not isinstance(item, dict):
            continue
        term = item.get("term")
        if not isinstance(term, str) or not term.strip() or term.strip().lower() in seen:
            continue
        seen.add(term.strip().lower())
        translation, example = item.get("translation_ru"), item.get("example")
        words.append(
            VocabItem(
                term=term.strip()[:100],
                translation_ru=translation.strip()[:200] if isinstance(translation, str) else None,
                source=source,
                example=example.strip()[:300] if isinstance(example, str) else None,
            )
        )
        if len(words) >= limit:
            break
    summary = data.get("summary_ru")
    return words, summary.strip()[:500] if isinstance(summary, str) and summary.strip() else None


class WordExtractor:
    def __init__(self, llm: OpenRouterClient, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings

    async def from_photo(self, user: User, image: bytes) -> Extracted:
        system = render(
            "extract_words_system.md.j2",
            user_name=user.name,
            level=user.level,
            source="photo",
            count=PHOTO_WORDS,
        )
        encoded = base64.b64encode(image).decode()
        content = [
            {"type": "text", "text": "The photo:"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
        ]
        result = await self._chat(system, content)
        try:
            words, _ = parse_words(result.text, "photo", PHOTO_WORDS)
        except ValueError:
            logger.warning("Photo words: unparseable JSON: %.200s", result.text)
            words = []
        return Extracted(words=words, summary_ru=None, llm=result)

    async def from_text(self, user: User, text: str) -> Extracted:
        system = render(
            "extract_words_system.md.j2",
            user_name=user.name,
            level=user.level,
            source="forward",
            count=FORWARD_WORDS,
        )
        result = await self._chat(system, text[:4000])
        try:
            words, summary = parse_words(result.text, "forward", FORWARD_WORDS)
        except ValueError as exc:
            raise LLMParseError(str(exc), result) from exc
        return Extracted(words=words, summary_ru=summary, llm=result)

    async def _chat(self, system: str, content: object) -> LLMResult:
        return await self._llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": content}],
            models=[self._settings.companion_model, self._settings.companion_fallback_model],
            temperature=0.3,
            max_tokens=1000,
            json_mode=True,
            reasoning_effort=self._settings.analyst_reasoning_effort or None,
        )
