"""Урок в Mini App по последним разговорам: тема разговора + разбор ошибок и грамматика.

Собирается LLM из реплик, ошибок и слов пользователя; ответы проверяются на сервере.
"""

import asyncio
import hashlib
import json
import logging
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import repo
from app.db.models import AudioCache, Lesson, Message, Mistake, User, Vocab
from app.llm.jsonutil import extract_json_object
from app.llm.openrouter import LLMError, OpenRouterClient
from app.personas import get_persona
from app.prompts import render
from app.services.plans import limits_for
from app.services.review import TYPE_LABELS_RU
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
SECTIONS = ("topic", "grammar")
MAX_PHRASES = 6
MAX_GRAMMAR_POINTS = 2
MISTAKES_IN_REVIEW = 5
# Разговор для урока: реплики с прошлого урока; если их почти нет — за последнюю неделю.
CONVERSATION_MESSAGES = 40
CONVERSATION_FALLBACK = timedelta(days=7)
MIN_FRESH_USER_MESSAGES = 4
TRANSCRIPT_CHARS = 6000
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
    messages: list[Message] = field(default_factory=list)  # недавний разговор, по порядку
    topics: list[str] = field(default_factory=list)  # темы разговора, самые частые первыми

    @property
    def user_messages(self) -> int:
        return sum(1 for m in self.messages if m.role == "user")


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
        valid = isinstance(mistake_id, int) and not isinstance(mistake_id, bool)
        exercise["mistake_id"] = mistake_id if valid and mistake_id in mistake_ids else None
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
            # у «прокачки» пример — фраза самого ученика с новым словом
            "example": v.example if v.source == "upgrade" and v.example else examples.get(v.id),
            "source": v.source,
            "note": v.note,
        }
        for v in material.vocab[:MAX_CARDS]
    ]


def clean_topic_block(raw: object) -> dict[str, Any] | None:
    """Блок «Тема»: о чём говорили и полезные фразы по теме."""
    if not isinstance(raw, dict):
        return None
    phrases = []
    raw_phrases = raw.get("phrases")
    for item in raw_phrases if isinstance(raw_phrases, list) else []:
        if not isinstance(item, dict) or not (en := _text(item.get("en"), 80)):
            continue
        phrases.append(
            {"en": en, "ru": _text(item.get("ru"), 80), "example": _text(item.get("example"), 160)}
        )
        if len(phrases) >= MAX_PHRASES:
            break
    summary = _text(raw.get("summary_ru"), 400)
    if not phrases and not summary:
        return None
    return {"summary_ru": summary, "phrases": phrases}


def clean_grammar(raw: object, mistake_ids: set[int]) -> list[dict[str, Any]]:
    """Короткие правила, которые объясняют ошибки пользователя."""
    points = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        title, rule = _text(item.get("title_ru"), 60), _text(item.get("rule_ru"), 600)
        if not title or not rule:
            continue
        raw_examples = item.get("examples")
        examples = [
            {"en": en, "ru": _text(example.get("ru"), 160)}
            for example in (raw_examples if isinstance(raw_examples, list) else [])[:3]
            if isinstance(example, dict) and (en := _text(example.get("en"), 160))
        ]
        raw_ids = item.get("mistake_ids")
        ids = [
            i
            for i in (raw_ids if isinstance(raw_ids, list) else [])
            if isinstance(i, int) and i in mistake_ids
        ]
        points.append(
            {"title_ru": title, "rule_ru": rule, "examples": examples, "mistake_ids": ids}
        )
        if len(points) >= MAX_GRAMMAR_POINTS:
            break
    return points


def mistakes_review(material: Material) -> list[dict[str, Any]]:
    """Разбор ошибок из разговоров: было → правильно, почему."""
    return [
        {
            "id": m.id,
            "original": m.original,
            "corrected": m.corrected,
            "explanation_ru": m.explanation_ru,
            "type_ru": TYPE_LABELS_RU.get(m.type, m.type),
        }
        for m in material.mistakes[:MISTAKES_IN_REVIEW]
    ]


def build_payload(raw: dict[str, Any], material: Material, rng: random.Random) -> dict[str, Any]:
    """Ответ модели → урок. ValueError, если годных упражнений слишком мало."""
    mistake_ids = {m.id for m in material.mistakes}
    exercises = []
    raw_exercises = raw.get("exercises")
    for item in raw_exercises if isinstance(raw_exercises, list) else []:
        exercise = clean_exercise(item, mistake_ids, rng)
        if exercise:
            section = item.get("section")
            if section not in SECTIONS:
                section = "grammar" if exercise["type"] == "fix" else "topic"
            exercise["section"] = section
            exercises.append(exercise)
        if len(exercises) >= MAX_EXERCISES:
            break
    if len(exercises) < MIN_EXERCISES:
        raise ValueError(f"only {len(exercises)} valid exercises")
    # Сначала тема разговора, потом разбор ошибок и грамматика — порядок урока.
    exercises.sort(key=lambda e: SECTIONS.index(e["section"]))

    examples: dict[int, str] = {}
    raw_cards = raw.get("cards")
    for card in raw_cards if isinstance(raw_cards, list) else []:
        vocab_id = card.get("vocab_id") if isinstance(card, dict) else None
        if isinstance(vocab_id, int) and (example := _text(card.get("example"), 200)):
            examples.setdefault(vocab_id, example)
    topic_ru = _text(raw.get("topic_ru"), 40) or (material.topics[0] if material.topics else None)
    return {
        "version": 2,
        "title": _text(raw.get("title"), 80) or "Урок по твоим разговорам",
        "topic": _text(raw.get("topic"), 100),
        "topic_ru": topic_ru,
        "intro_ru": _text(raw.get("intro_ru"), 600),
        "topic_block": clean_topic_block(raw.get("topic_block")),
        "mistakes_review": mistakes_review(material),
        "grammar": clean_grammar(raw.get("grammar"), mistake_ids),
        "exercises": exercises,
        "cards": _cards(material, examples),
        "insights": clean_insights(raw.get("insights")),
        "based_on": {
            "mistakes": len(material.mistakes),
            "words": len(material.vocab),
            "messages": material.user_messages,
            "topics": material.topics,
        },
    }


def fallback_payload(material: Material, rng: random.Random) -> dict[str, Any] | None:
    """Урок без LLM: разбор и исправление своих ошибок, карточки слов."""
    exercises = []
    for mistake in material.mistakes[:MAX_EXERCISES]:
        options, answer = _shuffle([mistake.corrected, mistake.original], 0, rng)
        exercises.append(
            {
                "type": "fix",
                "section": "grammar",
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
        "version": 2,
        "title": "Разбор ошибок",
        "topic": None,
        "topic_ru": material.topics[0] if material.topics else None,
        "intro_ru": "Сегодня короткий урок: разберём твои ошибки из разговоров и повторим слова.",
        "topic_block": None,
        "mistakes_review": mistakes_review(material),
        "grammar": [],
        "exercises": exercises,
        "cards": cards,
        "insights": None,
        "based_on": {
            "mistakes": len(material.mistakes),
            "words": len(material.vocab),
            "messages": material.user_messages,
            "topics": material.topics,
        },
    }


def conversation_transcript(messages: list[Message], user_name: str) -> str:
    """Последний разговор для урока; если длинный — оставляем конец."""
    lines = []
    for message in messages:
        text = message.text or ""
        if message.kind == "photo" and message.role == "user":
            text = f"[photo: {message.photo_caption or 'a photo'}] {text}".strip()
        if text:
            lines.append(f"{'Learner' if message.role == 'user' else 'Friend'}: {text}")
    transcript = "\n".join(lines)
    return transcript[-TRANSCRIPT_CHARS:]


def public_view(lesson: Lesson) -> dict[str, Any]:
    """Урок для клиента: правильные ответы — только у уже решённых упражнений."""
    payload = lesson.payload
    exercises = []
    for index, exercise in enumerate(payload["exercises"]):
        item: dict[str, Any] = {
            "index": index,
            "type": exercise["type"],
            "section": exercise.get("section", "grammar"),
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
        "topic_ru": payload.get("topic_ru"),
        "intro_ru": payload.get("intro_ru"),
        "topic_block": payload.get("topic_block"),
        "mistakes_review": payload.get("mistakes_review") or [],
        "grammar": payload.get("grammar") or [],
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
        messages = await self._recent_conversation(session, user, now)
        recent_ids = {m.id for m in messages if m.role == "user"}
        topics = Counter(m.topic for m in messages if m.role == "user" and m.topic)

        due = await session.scalars(
            select(Mistake)
            .where(
                Mistake.user_id == user.id,
                or_(
                    Mistake.next_review_at <= now,
                    Mistake.next_review_at.is_(None) & Mistake.resolved.is_(False),
                ),
            )
            .order_by(Mistake.id.desc())
            .limit(MISTAKES_FOR_LESSON * 3)
        )
        # Ошибки из этого разговора — первыми, дальше самые серьёзные и частые.
        mistakes = sorted(
            due,
            key=lambda m: (m.message_id not in recent_ids, -m.severity, -m.times_seen, -m.id),
        )[:MISTAKES_FOR_LESSON]
        vocab = await session.scalars(
            select(Vocab)
            .where(
                Vocab.user_id == user.id,
                or_(Vocab.next_review_at.is_(None), Vocab.next_review_at <= now),
            )
            .order_by(Vocab.next_review_at.asc().nulls_last(), Vocab.id.desc())
            .limit(WORDS_FOR_LESSON)
        )
        return Material(
            mistakes=mistakes,
            vocab=list(vocab),
            messages=messages,
            topics=[topic for topic, _ in topics.most_common(3)],
        )

    async def _recent_conversation(
        self, session: AsyncSession, user: User, now: datetime
    ) -> list[Message]:
        last_lesson = await session.scalar(
            select(func.max(Lesson.created_at)).where(Lesson.user_id == user.id)
        )
        since = last_lesson or now - CONVERSATION_FALLBACK

        async def load(after: datetime) -> list[Message]:
            rows = await session.scalars(
                select(Message)
                .where(Message.user_id == user.id, Message.created_at > after)
                .order_by(Message.id.desc())
                .limit(CONVERSATION_MESSAGES)
            )
            return list(reversed(list(rows)))

        messages = await load(since)
        if sum(1 for m in messages if m.role == "user") < MIN_FRESH_USER_MESSAGES:
            messages = await load(min(since, now - CONVERSATION_FALLBACK))
        return messages

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
        content = await self._material_json(session, user, material)
        await session.commit()  # запрос к модели — без открытой транзакции
        result = await self._llm.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            models=[self._settings.lesson_model, self._settings.companion_fallback_model],
            temperature=0.7,
            max_tokens=8000,
            json_mode=True,
            reasoning_effort=self._settings.lesson_reasoning_effort or None,
        )
        await record_llm(session, user.id, "llm_lesson", result)
        try:
            return build_payload(extract_json_object(result.text), material, rng)
        except ValueError:
            raise
        except Exception as exc:  # неожиданная форма JSON — простой урок лучше, чем ошибка 500
            logger.exception("Unexpected lesson JSON for user %s", user.id)
            raise ValueError("unexpected lesson JSON") from exc

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
            "conversation_topics": material.topics,
            "conversation": conversation_transcript(material.messages, user.name),
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
        # Атомарно: из двух параллельных запросов урок завершает (и шлёт follow-up) только один.
        claimed = await session.scalar(
            update(Lesson)
            .where(Lesson.id == lesson.id, Lesson.status != "done")
            .values(
                status="done",
                score=sum(1 for a in lesson.answers.values() if a.get("correct")),
                total=len(lesson.payload["exercises"]),
                completed_at=datetime.now(UTC),
            )
            .returning(Lesson.id)
        )
        await session.commit()
        await session.refresh(lesson)
        return claimed is not None

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
        await session.commit()  # TTS — без открытой транзакции

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
