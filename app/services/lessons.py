"""Урок дня в Mini App: собирается LLM из ошибок и слов пользователя, проверяется на сервере."""

import asyncio
import hashlib
import json
import logging
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import repo
from app.db.models import AudioCache, Lesson, Mistake, User, Vocab
from app.llm.jsonutil import extract_json_object
from app.llm.openrouter import LLMError, OpenRouterClient
from app.personas import get_persona
from app.prompts import render
from app.services.plans import limits_for
from app.services.tts import TextToSpeech, TTSError
from app.services.usage import record_llm, record_usage, tts_cost

logger = logging.getLogger(__name__)

EXERCISE_TYPES = ("fix", "gap", "translate", "listen")
DEFAULT_QUESTIONS = {
    "fix": "Как сказать правильно?",
    "gap": "Вставь пропущенное",
    "translate": "Выбери перевод",
    "listen": "Что ты услышал?",
}
MIN_EXERCISES = 3
MAX_EXERCISES = 10
MAX_CARDS = 6
MISTAKES_FOR_LESSON = 8
WORDS_FOR_LESSON = 6
# Ошибку, исправленную в уроке, повторим через неделю; неисправленную — завтра.
FIX_RESOLVED_REVIEW = timedelta(days=7)
FIX_RETRY = timedelta(days=1)

_GAP_RE = re.compile(r"_{2,}")


class LessonError(Exception):
    """Урок нельзя выдать; code уходит клиенту."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Material:
    mistakes: list[Mistake]
    vocab: list[Vocab]


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] if value else None


def _shuffle(options: list[str], answer: int, rng: random.Random) -> tuple[list[str], int]:
    order = list(range(len(options)))
    rng.shuffle(order)
    return [options[i] for i in order], order.index(answer)


def clean_exercise(
    item: object, mistake_ids: set[int], rng: random.Random
) -> dict[str, Any] | None:
    """Одно упражнение от модели → нормализованный вид; None, если оно негодное."""
    if not isinstance(item, dict) or item.get("type") not in EXERCISE_TYPES:
        return None
    kind = item["type"]
    raw_options = item.get("options")
    if not isinstance(raw_options, list):
        return None
    answer = item.get("answer")
    if isinstance(answer, str):  # модель иногда пишет сам правильный вариант
        answer = raw_options.index(answer) if answer in raw_options else None
    if not isinstance(answer, int) or isinstance(answer, bool):
        return None
    if not 0 <= answer < len(raw_options):
        return None
    correct = _text(raw_options[answer], 200)

    options: list[str] = []
    for option in raw_options:
        option = _text(option, 200)
        if option and option.lower() not in {o.lower() for o in options}:
            options.append(option)
    if not correct or correct not in options or not 2 <= len(options) <= 4:
        return None

    exercise: dict[str, Any] = {
        "type": kind,
        "question_ru": _text(item.get("question_ru"), 120) or DEFAULT_QUESTIONS[kind],
        "explanation_ru": _text(item.get("explanation_ru"), 400),
    }
    if kind == "fix":
        mistake_id = item.get("mistake_id")
        exercise["mistake_id"] = mistake_id if mistake_id in mistake_ids else None
    elif kind == "gap":
        sentence = _text(item.get("sentence"), 300)
        if not sentence or not _GAP_RE.search(sentence):
            return None
        exercise["sentence"] = _GAP_RE.sub("___", sentence)
    elif kind == "translate":
        exercise["ru"] = _text(item.get("ru"), 300)
        if not exercise["ru"]:
            return None
    elif kind == "listen":
        text = _text(item.get("text"), 200)
        if not text:
            return None
        exercise["text"] = text
        options[options.index(correct)] = text  # услышанное и правильный ответ совпадают
        correct = text

    exercise["options"], exercise["answer"] = _shuffle(options, options.index(correct), rng)
    return exercise


def clean_insights(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    def points(value: object) -> list[str]:
        items = value if isinstance(value, list) else []
        return [p for p in (_text(v, 200) for v in items) if p][:3]

    insights = {
        "strengths": points(raw.get("strengths")),
        "weaknesses": points(raw.get("weaknesses")),
        "tip_ru": _text(raw.get("tip_ru"), 300),
    }
    if not (insights["strengths"] or insights["weaknesses"] or insights["tip_ru"]):
        return None
    return insights


def _cards(material: Material, examples: dict[int, str]) -> list[dict[str, Any]]:
    return [
        {
            "vocab_id": v.id,
            "term": v.term,
            "translation_ru": v.translation_ru,
            "example": examples.get(v.id),
        }
        for v in material.vocab[:MAX_CARDS]
    ]


def build_payload(raw: dict[str, Any], material: Material, rng: random.Random) -> dict[str, Any]:
    """Ответ модели → урок. ValueError, если годных упражнений слишком мало."""
    mistake_ids = {m.id for m in material.mistakes}
    exercises = []
    for item in raw.get("exercises") or []:
        exercise = clean_exercise(item, mistake_ids, rng)
        if exercise:
            exercises.append(exercise)
        if len(exercises) >= MAX_EXERCISES:
            break
    if len(exercises) < MIN_EXERCISES:
        raise ValueError(f"only {len(exercises)} valid exercises")

    examples: dict[int, str] = {}
    for card in raw.get("cards") or []:
        if isinstance(card, dict) and (example := _text(card.get("example"), 200)):
            examples.setdefault(card.get("vocab_id"), example)
    return {
        "version": 1,
        "title": _text(raw.get("title"), 80) or "Урок дня",
        "topic": _text(raw.get("topic"), 100),
        "intro_ru": _text(raw.get("intro_ru"), 600),
        "exercises": exercises,
        "cards": _cards(material, examples),
        "insights": clean_insights(raw.get("insights")),
        "based_on": {"mistakes": len(material.mistakes), "words": len(material.vocab)},
    }


def fallback_payload(material: Material, rng: random.Random) -> dict[str, Any] | None:
    """Урок без LLM: исправление своих ошибок и карточки слов."""
    exercises = []
    for mistake in material.mistakes[:MAX_EXERCISES]:
        options, answer = _shuffle([mistake.corrected, mistake.original], 0, rng)
        exercises.append(
            {
                "type": "fix",
                "question_ru": DEFAULT_QUESTIONS["fix"],
                "explanation_ru": mistake.explanation_ru,
                "mistake_id": mistake.id,
                "options": options,
                "answer": answer,
            }
        )
    cards = _cards(material, {})
    if not exercises and not cards:
        return None
    return {
        "version": 1,
        "title": "Повторение",
        "topic": None,
        "intro_ru": "Сегодня короткий урок: повторим твои ошибки из разговоров и новые слова.",
        "exercises": exercises,
        "cards": cards,
        "insights": None,
        "based_on": {"mistakes": len(material.mistakes), "words": len(material.vocab)},
    }


def public_view(lesson: Lesson) -> dict[str, Any]:
    """Урок для клиента: правильные ответы — только у уже решённых упражнений."""
    payload = lesson.payload
    exercises = []
    for index, exercise in enumerate(payload["exercises"]):
        item: dict[str, Any] = {
            "index": index,
            "type": exercise["type"],
            "question_ru": exercise["question_ru"],
            "options": exercise["options"],
        }
        for key in ("sentence", "ru"):
            if key in exercise:
                item[key] = exercise[key]
        result = lesson.answers.get(str(index))
        if result is not None:
            item["result"] = grade_view(exercise, result["choice"])
        exercises.append(item)
    return {
        "id": lesson.id,
        "status": lesson.status,
        "created_at": lesson.created_at.isoformat() if lesson.created_at else None,
        "title": payload["title"],
        "topic": payload.get("topic"),
        "intro_ru": payload.get("intro_ru"),
        "exercises": exercises,
        "cards": payload.get("cards") or [],
        "insights": payload.get("insights"),
        "score": lesson.score,
        "total": lesson.total,
    }


def grade_view(exercise: dict[str, Any], choice: int) -> dict[str, Any]:
    view = {
        "choice": choice,
        "correct": choice == exercise["answer"],
        "answer": exercise["answer"],
        "explanation_ru": exercise.get("explanation_ru"),
    }
    if exercise["type"] == "listen":
        view["text"] = exercise["text"]
    return view


class LessonService:
    def __init__(self, llm: OpenRouterClient, tts: TextToSpeech, settings: Settings) -> None:
        self._llm = llm
        self._tts = tts
        self._settings = settings
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # --- материал и лимиты ---

    async def material(self, session: AsyncSession, user: User) -> Material:
        now = datetime.now(UTC)
        mistakes = await session.scalars(
            select(Mistake)
            .where(
                Mistake.user_id == user.id,
                or_(
                    Mistake.next_review_at <= now,
                    Mistake.next_review_at.is_(None) & Mistake.resolved.is_(False),
                ),
            )
            .order_by(Mistake.severity.desc(), Mistake.times_seen.desc(), Mistake.id.desc())
            .limit(MISTAKES_FOR_LESSON)
        )
        vocab = await session.scalars(
            select(Vocab)
            .where(
                Vocab.user_id == user.id,
                or_(Vocab.next_review_at.is_(None), Vocab.next_review_at <= now),
            )
            .order_by(Vocab.next_review_at.asc().nulls_last(), Vocab.id.desc())
            .limit(WORDS_FOR_LESSON)
        )
        return Material(mistakes=list(mistakes), vocab=list(vocab))

    async def today(self, session: AsyncSession, user: User) -> list[Lesson]:
        since = repo.day_start_utc(user.timezone)
        result = await session.scalars(
            select(Lesson)
            .where(Lesson.user_id == user.id, Lesson.created_at >= since)
            .order_by(Lesson.id)
        )
        return list(result)

    async def remaining_today(self, session: AsyncSession, user: User) -> int:
        since = repo.day_start_utc(user.timezone)
        created = await session.scalar(
            select(func.count(Lesson.id)).where(
                Lesson.user_id == user.id, Lesson.created_at >= since
            )
        )
        return max(0, limits_for(user, self._settings).lessons - (created or 0))

    # --- генерация ---

    async def generate(self, session: AsyncSession, user: User) -> Lesson:
        if not user.onboarded:
            raise LessonError("not_onboarded")
        async with self._locks[user.id]:
            if await self.remaining_today(session, user) <= 0:
                raise LessonError("limit_reached")
            material = await self.material(session, user)
            rng = random.Random()
            try:
                payload = await self._generate_with_llm(session, user, material, rng)
            except (LLMError, ValueError) as exc:
                logger.warning("Lesson generation via LLM failed for %s: %s", user.id, exc)
                payload = fallback_payload(material, rng)
                if payload is None:
                    await session.commit()  # расходы на неудачный запрос всё равно учитываем
                    raise LessonError("generation_failed") from exc
            lesson = Lesson(
                user_id=user.id, payload=payload, answers={}, total=len(payload["exercises"])
            )
            session.add(lesson)
            await session.commit()
            return lesson

    async def _generate_with_llm(
        self, session: AsyncSession, user: User, material: Material, rng: random.Random
    ) -> dict[str, Any]:
        persona = get_persona(user.persona)
        system = render(
            "lesson_system.md.j2", user_name=user.name, level=user.level, persona_name=persona.name
        )
        result = await self._llm.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": await self._material_json(session, user, material)},
            ],
            models=[self._settings.lesson_model, self._settings.companion_fallback_model],
            temperature=0.7,
            max_tokens=8000,
            json_mode=True,
            reasoning_effort=self._settings.lesson_reasoning_effort or None,
        )
        await record_llm(session, user.id, "llm_lesson", result)
        return build_payload(extract_json_object(result.text), material, rng)

    async def _material_json(self, session: AsyncSession, user: User, material: Material) -> str:
        since = datetime.now(UTC) - timedelta(days=30)
        stats = await session.execute(
            select(Mistake.type, func.count(Mistake.id))
            .where(Mistake.user_id == user.id, Mistake.created_at >= since)
            .group_by(Mistake.type)
        )
        data = {
            "learner": {
                "name": user.name,
                "level_self": user.level,
                "level_estimated": user.estimated_level,
                "interests_summary": user.conversation_summary,
            },
            "mistakes": [
                {
                    "id": m.id,
                    "original": m.original,
                    "corrected": m.corrected,
                    "type": m.type,
                    "times_seen": m.times_seen,
                }
                for m in material.mistakes
            ],
            "words": [
                {"id": v.id, "term": v.term, "translation_ru": v.translation_ru}
                for v in material.vocab
            ],
            "mistake_stats_30d": dict(stats.all()),
        }
        return json.dumps(data, ensure_ascii=False)

    # --- прохождение ---

    async def answer(
        self, session: AsyncSession, lesson: Lesson, index: int, choice: int
    ) -> dict[str, Any]:
        exercises = lesson.payload["exercises"]
        if not 0 <= index < len(exercises):
            raise LessonError("bad_exercise")
        exercise = exercises[index]
        previous = lesson.answers.get(str(index))
        if previous is not None:  # повторный ответ не переигрывает первый
            return grade_view(exercise, previous["choice"])
        if not 0 <= choice < len(exercise["options"]):
            raise LessonError("bad_choice")

        correct = choice == exercise["answer"]
        lesson.answers = {**lesson.answers, str(index): {"choice": choice, "correct": correct}}
        if exercise["type"] == "fix" and exercise.get("mistake_id"):
            mistake = await session.get(Mistake, exercise["mistake_id"])
            if mistake is not None and mistake.user_id == lesson.user_id:
                mistake.resolved = correct
                mistake.next_review_at = datetime.now(UTC) + (
                    FIX_RESOLVED_REVIEW if correct else FIX_RETRY
                )
        await session.commit()
        return grade_view(exercise, choice)

    async def complete(self, session: AsyncSession, lesson: Lesson) -> bool:
        """Завершает урок. True — если он завершён именно сейчас (а не раньше)."""
        if lesson.status == "done":
            return False
        lesson.status = "done"
        lesson.score = sum(1 for a in lesson.answers.values() if a.get("correct"))
        lesson.total = len(lesson.payload["exercises"])
        lesson.completed_at = datetime.now(UTC)
        await session.commit()
        return True

    async def audio(
        self, session: AsyncSession, user: User, lesson: Lesson, index: int
    ) -> tuple[bytes, str]:
        exercises = lesson.payload["exercises"]
        if not 0 <= index < len(exercises) or exercises[index]["type"] != "listen":
            raise LessonError("no_audio")
        text = exercises[index]["text"]
        voice_id = get_persona(user.persona).voice_id(self._settings)
        audio_format = self._settings.lesson_audio_format
        key = hashlib.sha256(
            f"{voice_id}|{self._settings.tts_model}|{audio_format}|{text}".encode()
        ).hexdigest()
        cached = await session.get(AudioCache, key)
        if cached is not None:
            return cached.content, cached.mime

        try:
            content = await self._tts.synthesize_raw(text, voice_id, audio_format)
        except TTSError as exc:
            logger.warning("Lesson audio failed: %s", exc)
            raise LessonError("audio_failed") from exc
        mime = _mime_for(audio_format)
        await record_usage(
            session,
            user_id=user.id,
            kind="tts_lesson",
            model=self._settings.tts_model,
            chars=len(text),
            cost_usd=tts_cost(len(text), self._settings),
        )
        session.add(AudioCache(key=key, content=content, mime=mime))
        try:
            await session.commit()
        except IntegrityError:  # параллельный запрос успел положить то же аудио
            await session.rollback()
        return content, mime


def _mime_for(audio_format: str) -> str:
    if audio_format.startswith("mp3"):
        return "audio/mpeg"
    if audio_format.startswith("opus"):
        return "audio/ogg"
    if audio_format.startswith("wav"):
        return "audio/wav"
    return "application/octet-stream"
