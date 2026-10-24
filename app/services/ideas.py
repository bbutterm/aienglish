"""«Темы для тебя»: личные темы и миссии. Заголовки — сразу, содержание — по нажатию."""

import json
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import repo
from app.db.models import Message, Mistake, TopicIdea, User, Vocab
from app.llm.jsonutil import extract_json_object
from app.llm.openrouter import LLMError, OpenRouterClient
from app.personas import get_persona
from app.prompts import render
from app.services.missions import GOALS
from app.services.plans import is_premium
from app.services.usage import record_llm

logger = logging.getLogger(__name__)

KINDS = ("talk", "mission")
TALKS_PER_BATCH = 4
MISSIONS_PER_BATCH = 2
VISIBLE_DAYS = 14
VISIBLE_LIMIT = 8
REFRESHES_PER_DAY = 2
PREMIUM_REFRESHES_PER_DAY = 6


class IdeaError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] if value else None


def clean_ideas(raw: object, missions: bool) -> list[dict[str, Any]]:
    ideas = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind") if item.get("kind") in KINDS else "talk"
        title = (_text(item.get("title_ru"), 90) or "").strip(" «»\"'.")
        if not title or title.lower() in seen or (kind == "mission" and not missions):
            continue
        seen.add(title.lower())
        ideas.append(
            {
                "kind": kind,
                "title_ru": title,
                "title_en": _text(item.get("title_en"), 120),
                "focus_ru": _text(item.get("focus_ru"), 40),
            }
        )
    return ideas[: TALKS_PER_BATCH + MISSIONS_PER_BATCH]


def _pairs(raw: object, limit: int) -> list[dict[str, str | None]]:
    pairs = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and (en := _text(item.get("en"), 160)):
            pairs.append({"en": en, "ru": _text(item.get("ru"), 160)})
        if len(pairs) >= limit:
            break
    return pairs


def clean_content(raw: dict[str, Any], kind: str) -> dict[str, Any]:
    """Содержание карточки темы; ValueError — если миссию по нему не сыграть."""
    content: dict[str, Any] = {
        "intro_ru": _text(raw.get("intro_ru"), 400),
        "phrases": _pairs(raw.get("phrases"), 6),
    }
    if kind == "mission":
        goals = [g for g in _pairs(raw.get("goals"), GOALS) if g["ru"]]
        role, setting = _text(raw.get("role_en"), 120), _text(raw.get("setting_en"), 400)
        if len(goals) < GOALS or not role or not setting:
            raise ValueError("mission without role, setting or 3 goals")
        content |= {
            "role_en": role,
            "setting_en": setting,
            "setting_ru": _text(raw.get("setting_ru"), 400),
            "goals": goals,
        }
    else:
        questions = raw.get("questions")
        content |= {
            "questions": [q for q in (_text(v, 200) for v in (questions or [])[:3]) if q]
            if isinstance(questions, list)
            else [],
            "tip_ru": _text(raw.get("tip_ru"), 300),
        }
    return content


def idea_view(idea: TopicIdea) -> dict[str, Any]:
    return {
        "id": idea.id,
        "kind": idea.kind,
        "title_ru": idea.title_ru,
        "title_en": idea.title_en,
        "focus_ru": idea.focus_ru,
        "status": idea.status,
        "content": idea.content,
    }


class IdeaService:
    def __init__(self, llm: OpenRouterClient, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings

    async def visible(self, session: AsyncSession, user: User, missions: bool) -> list[TopicIdea]:
        since = datetime.now(UTC) - timedelta(days=VISIBLE_DAYS)
        stmt = (
            select(TopicIdea)
            .where(
                TopicIdea.user_id == user.id,
                TopicIdea.created_at >= since,
                TopicIdea.status != "done",
            )
            # Свежая порция сверху, внутри неё — порядок модели (у порции одно now()).
            .order_by(TopicIdea.created_at.desc(), TopicIdea.id)
            .limit(VISIBLE_LIMIT)
        )
        if not missions:
            stmt = stmt.where(TopicIdea.kind == "talk")
        return list(await session.scalars(stmt))

    async def refreshes_left(self, session: AsyncSession, user: User) -> int:
        limit = PREMIUM_REFRESHES_PER_DAY if is_premium(user) else REFRESHES_PER_DAY
        since = repo.day_start_utc(user.timezone)
        used = await repo.count_usage_since(session, user.id, "llm_ideas", since)
        return max(0, limit - used)

    async def generate(self, session: AsyncSession, user: User, missions: bool) -> list[TopicIdea]:
        """Новая порция заголовков; содержимое — позже, по нажатию."""
        if not user.onboarded:
            raise IdeaError("not_onboarded")
        if await self.refreshes_left(session, user) <= 0:
            raise IdeaError("limit_reached")
        persona = get_persona(user.persona)
        system = render(
            "ideas_system.md.j2",
            user_name=user.name,
            level=user.level,
            persona_name=persona.name,
            talks=TALKS_PER_BATCH,
            missions=MISSIONS_PER_BATCH if missions else 0,
        )
        try:
            result = await self._llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": await self._profile(session, user)},
                ],
                models=[self._settings.lesson_model, self._settings.companion_fallback_model],
                temperature=0.9,
                max_tokens=2000,
                json_mode=True,
                reasoning_effort=self._settings.analyst_reasoning_effort or None,
            )
        except LLMError as exc:
            raise IdeaError("generation_failed") from exc
        await record_llm(session, user.id, "llm_ideas", result)
        try:
            ideas = clean_ideas(extract_json_object(result.text).get("ideas"), missions)
        except ValueError:
            ideas = []
        if not ideas:
            await session.commit()
            raise IdeaError("generation_failed")
        rows = [TopicIdea(user_id=user.id, **idea) for idea in ideas]
        session.add_all(rows)
        await session.commit()
        return rows

    async def open(self, session: AsyncSession, user: User, idea: TopicIdea) -> TopicIdea:
        """Содержимое темы генерируется один раз — при первом открытии."""
        if idea.content is not None:
            return idea
        persona = get_persona(user.persona)
        system = render(
            "idea_content_system.md.j2",
            user_name=user.name,
            level=user.level,
            persona_name=persona.name,
            kind=idea.kind,
            title_en=idea.title_en or idea.title_ru,
            title_ru=idea.title_ru,
            focus_ru=idea.focus_ru,
        )
        try:
            result = await self._llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": await self._profile(session, user)},
                ],
                models=[self._settings.lesson_model, self._settings.companion_fallback_model],
                temperature=0.7,
                max_tokens=2000,
                json_mode=True,
                reasoning_effort=self._settings.analyst_reasoning_effort or None,
            )
        except LLMError as exc:
            raise IdeaError("generation_failed") from exc
        await record_llm(session, user.id, "llm_idea_content", result)
        try:
            idea.content = clean_content(extract_json_object(result.text), idea.kind)
        except ValueError as exc:
            await session.commit()
            raise IdeaError("generation_failed") from exc
        idea.status = "opened"
        await session.commit()
        return idea

    async def _profile(self, session: AsyncSession, user: User) -> str:
        since = datetime.now(UTC) - timedelta(days=30)
        topics = Counter(
            await session.scalars(
                select(Message.topic).where(
                    Message.user_id == user.id,
                    Message.role == "user",
                    Message.topic.is_not(None),
                    Message.created_at >= since,
                )
            )
        )
        types = await session.execute(
            select(Mistake.type, func.count(Mistake.id))
            .where(Mistake.user_id == user.id, Mistake.created_at >= since)
            .group_by(Mistake.type)
        )
        mistakes = await session.scalars(
            select(Mistake)
            .where(Mistake.user_id == user.id)
            .order_by(Mistake.times_seen.desc(), Mistake.id.desc())
            .limit(6)
        )
        plain = await session.scalars(
            select(Vocab.note)
            .where(Vocab.user_id == user.id, Vocab.source == "upgrade")
            .order_by(Vocab.id.desc())
            .limit(6)
        )
        suggested = await session.scalars(
            select(TopicIdea.title_ru)
            .where(TopicIdea.user_id == user.id)
            .order_by(TopicIdea.id.desc())
            .limit(30)
        )
        return json.dumps(
            {
                "learner": {
                    "name": user.name,
                    "level_self": user.level,
                    "level_estimated": user.estimated_level,
                    "interests_summary": user.conversation_summary,
                },
                "recent_topics": [t for t, _ in topics.most_common(6)],
                "mistake_types_30d": dict(types.all()),
                "typical_mistakes": [f"{m.original} → {m.corrected}" for m in mistakes],
                "overused_plain_words": [p for p in plain if p],
                "already_suggested": list(suggested),
            },
            ensure_ascii=False,
        )
