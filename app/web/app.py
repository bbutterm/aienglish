"""HTTP API Mini App и раздача фронтенда."""

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from aiogram import Bot
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.bot.handlers.common import send_opener
from app.config import Settings
from app.db.models import Lesson, User, Vocab
from app.features import FeatureFlags
from app.main import build_dialog, build_llm
from app.personas import get_persona
from app.services import admin as admin_service
from app.services.dialog import BackgroundTasks
from app.services.ideas import IdeaService
from app.services.lessons import LessonError, LessonService, public_view
from app.services.plans import extend_premium, is_premium, limits_for
from app.services.podcast import PodcastService
from app.services.progress import build_progress
from app.services.srs import review_card
from app.services.tts import TextToSpeech
from app.web import features_api
from app.web.deps import (
    AdminDep,
    SessionDep,
    StateDep,
    UserDep,
    WebState,
    api_error,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

LESSON_ERRORS = {
    "not_onboarded": (403, "Сначала познакомься с ботом в чате — нажми /start."),
    "limit_reached": (429, "Урок на сегодня уже есть. Новый — завтра."),
    "generation_failed": (503, "Не получилось собрать урок. Попробуй чуть позже."),
    "bad_exercise": (422, "Такого упражнения нет."),
    "bad_choice": (422, "Такого варианта нет."),
    "no_audio": (404, "У этого упражнения нет аудио."),
    "audio_failed": (503, "Не получилось озвучить. Попробуй ещё раз."),
}


def build_state(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    http: httpx.AsyncClient,
    bot: Bot,
) -> WebState:
    tasks = BackgroundTasks()
    llm = build_llm(settings, http)
    tts = TextToSpeech(http, settings)
    features = FeatureFlags(session_factory)
    return WebState(
        settings=settings,
        session_factory=session_factory,
        lessons=LessonService(llm, tts, settings),
        dialog=build_dialog(settings, session_factory, http, tasks, features),
        bot=bot,
        tasks=tasks,
        features=features,
        ideas=IdeaService(llm, settings),
        podcasts=PodcastService(llm, tts, settings),
    )


def lesson_error(exc: LessonError) -> HTTPException:
    status, message = LESSON_ERRORS.get(exc.code, (400, "Ошибка урока."))
    return api_error(status, exc.code, message)


async def own_lesson(session: AsyncSession, user: User, lesson_id: int) -> Lesson:
    lesson = await session.get(Lesson, lesson_id)
    if lesson is None or lesson.user_id != user.id:
        raise api_error(404, "not_found", "Урок не найден.")
    return lesson


# --- модели запросов ---


class AnswerIn(BaseModel):
    index: int
    choice: int


class ReviewIn(BaseModel):
    grade: Literal["again", "good"]


class PremiumIn(BaseModel):
    days: int = Field(ge=0, le=3650)
    notify: bool = True


class BlockIn(BaseModel):
    blocked: bool


# --- фоновые действия бота ---


async def lesson_followup(state: WebState, user_id: int, topic: str | None) -> None:
    """После урока собеседник сам заводит разговор на его тему (§3 дизайна)."""
    async with state.session_factory() as session:
        user = await session.get(User, user_id)
        if user is None or user.blocked or not user.onboarded:
            return
        await send_opener(state.bot, user.id, session, user, state.dialog, "lesson", topic)


async def notify_premium(state: WebState, user: User) -> None:
    limits = limits_for(user, state.settings)
    await state.bot.send_message(
        user.id,
        texts.PREMIUM_GRANTED.format(
            until=f"{user.premium_until:%d.%m.%Y}",
            messages=limits.messages,
            tts=limits.tts,
            lessons=limits.lessons,
        ),
    )


# --- приложение ---


def _static_version() -> str:
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file() and path.name != "index.html":
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def create_app(state: WebState) -> FastAPI:
    app = FastAPI(title="English Buddy Mini App", docs_url=None, redoc_url=None)
    app.state.web = state
    index_html = (STATIC_DIR / "index.html").read_text().replace("__VERSION__", _static_version())

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # --- пользователь ---

    @app.get("/api/me")
    async def me(user: UserDep, state: StateDep) -> dict[str, Any]:
        persona = get_persona(user.persona)
        limits = limits_for(user, state.settings)
        return {
            "id": user.id,
            "name": user.name,
            "level": user.level,
            "estimated_level": user.estimated_level,
            "onboarded": user.onboarded,
            "persona": {"key": persona.key, "name": persona.name, "flag": persona.flag},
            "is_admin": user.id in state.settings.admin_ids,
            "premium": is_premium(user),
            "premium_until": user.premium_until.isoformat() if user.premium_until else None,
            "limits": {"messages": limits.messages, "tts": limits.tts, "lessons": limits.lessons},
            "features": await state.features.snapshot(),
        }

    @app.get("/api/lessons/today")
    async def lessons_today(user: UserDep, state: StateDep, session: SessionDep) -> dict[str, Any]:
        lessons = await state.lessons.today(session, user)
        material = await state.lessons.material(session, user)
        return {
            "lessons": [public_view(lesson) for lesson in lessons],
            "remaining": await state.lessons.remaining_today(session, user),
            "limit": limits_for(user, state.settings).lessons,
            "material": {
                "mistakes": len(material.mistakes),
                "words": len(material.vocab),
                "messages": material.user_messages,
                "topics": material.topics,
            },
        }

    @app.post("/api/lessons")
    async def create_lesson(user: UserDep, state: StateDep, session: SessionDep) -> dict[str, Any]:
        try:
            lesson = await state.lessons.generate(session, user)
        except LessonError as exc:
            raise lesson_error(exc) from exc
        return public_view(lesson)

    @app.post("/api/lessons/{lesson_id}/answers")
    async def answer(
        lesson_id: int, body: AnswerIn, user: UserDep, state: StateDep, session: SessionDep
    ) -> dict[str, Any]:
        lesson = await own_lesson(session, user, lesson_id)
        try:
            return await state.lessons.answer(session, lesson, body.index, body.choice)
        except LessonError as exc:
            raise lesson_error(exc) from exc

    @app.get("/api/lessons/{lesson_id}/audio/{index}")
    async def lesson_audio(
        lesson_id: int, index: int, user: UserDep, state: StateDep, session: SessionDep
    ) -> Response:
        lesson = await own_lesson(session, user, lesson_id)
        try:
            content, mime = await state.lessons.audio(session, user, lesson, index)
        except LessonError as exc:
            raise lesson_error(exc) from exc
        return Response(
            content, media_type=mime, headers={"Cache-Control": "private, max-age=86400"}
        )

    @app.post("/api/lessons/{lesson_id}/complete")
    async def complete(
        lesson_id: int, user: UserDep, state: StateDep, session: SessionDep
    ) -> dict[str, Any]:
        lesson = await own_lesson(session, user, lesson_id)
        if await state.lessons.complete(session, lesson):
            state.tasks.spawn(
                lesson_followup(state, user.id, lesson.payload.get("topic")),
                name=f"lesson-followup:{user.id}",
            )
        return public_view(lesson)

    @app.get("/api/progress")
    async def progress(user: UserDep, session: SessionDep) -> dict[str, Any]:
        return await build_progress(session, user)

    @app.get("/api/vocab")
    async def vocab(user: UserDep, session: SessionDep) -> dict[str, Any]:
        now = datetime.now(UTC)
        due = (Vocab.next_review_at.is_(None)) | (Vocab.next_review_at <= now)
        rows = await session.scalars(
            select(Vocab)
            .where(Vocab.user_id == user.id)
            .order_by(case((due, 0), else_=1), Vocab.next_review_at.asc().nulls_last())
            .limit(300)
        )
        items = [vocab_view(v, now) for v in rows]
        return {"items": items, "due": sum(1 for i in items if i["due"])}

    @app.post("/api/vocab/{vocab_id}/review")
    async def review(
        vocab_id: int, body: ReviewIn, user: UserDep, session: SessionDep
    ) -> dict[str, Any]:
        card = await session.get(Vocab, vocab_id)
        if card is None or card.user_id != user.id:
            raise api_error(404, "not_found", "Слово не найдено.")
        now = datetime.now(UTC)
        review_card(card, body.grade, now)
        await session.commit()
        return vocab_view(card, now)

    # --- админка ---

    @app.get("/api/admin/overview")
    async def admin_overview(
        _: AdminDep, session: SessionDep, days: Annotated[int, Query(ge=7, le=60)] = 14
    ) -> dict[str, Any]:
        return await admin_service.overview(session, days)

    @app.get("/api/admin/users")
    async def admin_users(
        _: AdminDep,
        session: SessionDep,
        q: Annotated[str, Query(max_length=64)] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        return await admin_service.list_users(session, q, offset)

    async def target_user(session: AsyncSession, user_id: int) -> User:
        user = await session.get(User, user_id)
        if user is None:
            raise api_error(404, "not_found", "Пользователь не найден.")
        return user

    @app.get("/api/admin/users/{user_id}")
    async def admin_user(
        user_id: int, _: AdminDep, state: StateDep, session: SessionDep
    ) -> dict[str, Any]:
        user = await target_user(session, user_id)
        return await admin_service.user_detail(session, user, state.settings)

    @app.post("/api/admin/users/{user_id}/premium")
    async def admin_premium(
        user_id: int, body: PremiumIn, _: AdminDep, state: StateDep, session: SessionDep
    ) -> dict[str, Any]:
        user = await target_user(session, user_id)
        extend_premium(user, body.days)
        await session.commit()
        if body.notify and body.days > 0:
            try:
                await notify_premium(state, user)
            except Exception:  # пользователь мог заблокировать бота — премиум всё равно выдан
                logger.warning("Could not notify %s about premium", user_id, exc_info=True)
        return await admin_service.user_detail(session, user, state.settings)

    @app.post("/api/admin/users/{user_id}/block")
    async def admin_block(
        user_id: int, body: BlockIn, _: AdminDep, state: StateDep, session: SessionDep
    ) -> dict[str, Any]:
        user = await target_user(session, user_id)
        if body.blocked and user.id in state.settings.admin_ids:
            raise api_error(422, "cannot_block_admin", "Админа заблокировать нельзя.")
        user.blocked = body.blocked
        await session.commit()
        return await admin_service.user_detail(session, user, state.settings)

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next: Any) -> Response:
        # Модули фронтенда грузятся без версии в URL — пусть браузер сверяет ETag каждый раз.
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.include_router(features_api.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def vocab_view(card: Vocab, now: datetime) -> dict[str, Any]:
    return {
        "id": card.id,
        "term": card.term,
        "translation_ru": card.translation_ru,
        "source": card.source,
        "note": card.note,
        "example": card.example,
        "status": card.status,
        "due": card.next_review_at is None or card.next_review_at <= now,
        "next_review_at": card.next_review_at.isoformat() if card.next_review_at else None,
    }
