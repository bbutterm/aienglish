"""API включаемых фич: «Темы для тебя» и миссии, подкаст, приглашения, переключатели в админке."""

import logging
from typing import Annotated, Any

from aiogram.types import BufferedInputFile
from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select

from app.bot import texts
from app.bot.after_turn import start_mission, start_talk
from app.db.models import Podcast, TopicIdea, User
from app.features import FEATURES
from app.services import admin as admin_service
from app.services import journey, payments
from app.services.ideas import IdeaError, idea_view
from app.services.plans import is_premium
from app.services.podcast import PodcastError, podcast_view
from app.web.deps import AdminDep, SessionDep, StateDep, UserDep, WebState, api_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

ERRORS = {
    "feature_off": (404, "Эта функция сейчас выключена."),
    "not_onboarded": (403, "Сначала познакомься с ботом в чате — нажми /start."),
    "limit_reached": (429, "На сегодня лимит исчерпан — загляни завтра."),
    "generation_failed": (503, "Не получилось придумать — попробуй ещё раз чуть позже."),
    "audio_failed": (503, "Не получилось озвучить выпуск. Попробуй чуть позже."),
    "mission_active": (409, "Сначала закончи текущую сцену в чате (/stop)."),
}


def error(code: str) -> Exception:
    status, message = ERRORS.get(code, (400, "Ошибка."))
    return api_error(status, code, message)


async def require(state: WebState, key: str) -> None:
    if not await state.features.enabled(key):
        raise error("feature_off")


# --- «Темы для тебя» и миссии ---


async def own_idea(session: SessionDep, user: User, idea_id: int) -> TopicIdea:
    idea = await session.get(TopicIdea, idea_id)
    if idea is None or idea.user_id != user.id:
        raise api_error(404, "not_found", "Тема не найдена.")
    return idea


@router.get("/ideas")
async def ideas(user: UserDep, state: StateDep, session: SessionDep) -> dict[str, Any]:
    await require(state, "topic_ideas")
    missions = await state.features.enabled("missions")
    items = await state.ideas.visible(session, user, missions)
    if not items and user.onboarded and await state.ideas.refreshes_left(session, user) > 0:
        # Первый заход: заголовки готовы сразу, без лишней кнопки.
        try:
            await state.ideas.generate(session, user, missions)
            items = await state.ideas.visible(session, user, missions)
        except IdeaError as exc:
            logger.warning("Ideas generation failed for %s: %s", user.id, exc.code)
    return {
        "ideas": [idea_view(i) for i in items],
        "refreshes_left": await state.ideas.refreshes_left(session, user),
        "missions": missions,
        "active_mission": user.active_mission,
    }


@router.post("/ideas/refresh")
async def refresh_ideas(user: UserDep, state: StateDep, session: SessionDep) -> dict[str, Any]:
    await require(state, "topic_ideas")
    missions = await state.features.enabled("missions")
    try:
        await state.ideas.generate(session, user, missions)
    except IdeaError as exc:
        raise error(exc.code) from exc
    return await ideas(user, state, session)


@router.post("/ideas/{idea_id}/open")
async def open_idea(
    idea_id: int, user: UserDep, state: StateDep, session: SessionDep
) -> dict[str, Any]:
    await require(state, "topic_ideas")
    idea = await own_idea(session, user, idea_id)
    try:
        idea = await state.ideas.open(session, user, idea)
    except IdeaError as exc:
        raise error(exc.code) from exc
    return idea_view(idea)


@router.post("/ideas/{idea_id}/start")
async def start_idea(
    idea_id: int, user: UserDep, state: StateDep, session: SessionDep
) -> dict[str, Any]:
    """«Обсудить» / «Начать сцену»: собеседник пишет в чат, приложение можно закрывать."""
    await require(state, "topic_ideas")
    idea = await own_idea(session, user, idea_id)
    if idea.kind == "mission":
        await require(state, "missions")
        if user.active_mission:
            raise error("mission_active")
    if idea.content is None:
        try:
            idea = await state.ideas.open(session, user, idea)
        except IdeaError as exc:
            raise error(exc.code) from exc
    starter = start_mission if idea.kind == "mission" else start_talk
    state.tasks.spawn(chat_start(state, user.id, idea.id, starter), name=f"idea:{user.id}")
    return {"ok": True, "kind": idea.kind}


async def chat_start(state: WebState, user_id: int, idea_id: int, starter: Any) -> None:
    async with state.session_factory() as session:
        user = await session.get(User, user_id)
        idea = await session.get(TopicIdea, idea_id)
        if user is None or idea is None or user.blocked:
            return
        await starter(state.bot, session, user, state.dialog, idea)


# --- «Подкаст про тебя» ---


async def own_podcast(session: SessionDep, user: User, podcast_id: int) -> Podcast:
    podcast = await session.get(Podcast, podcast_id)
    if podcast is None or podcast.user_id != user.id:
        raise api_error(404, "not_found", "Выпуск не найден.")
    return podcast


@router.get("/podcasts")
async def podcasts(user: UserDep, state: StateDep, session: SessionDep) -> dict[str, Any]:
    await require(state, "podcast")
    items = await state.podcasts.recent(session, user)
    return {
        "items": [podcast_view(p) for p in items],
        "remaining": await state.podcasts.remaining(session, user),
    }


@router.post("/podcasts")
async def create_podcast(user: UserDep, state: StateDep, session: SessionDep) -> dict[str, Any]:
    await require(state, "podcast")
    try:
        podcast = await state.podcasts.create(session, user)
    except PodcastError as exc:
        raise error(exc.code) from exc
    return podcast_view(podcast)


@router.get("/podcasts/{podcast_id}/audio")
async def podcast_audio(
    podcast_id: int, user: UserDep, state: StateDep, session: SessionDep
) -> Response:
    await require(state, "podcast")
    podcast = await own_podcast(session, user, podcast_id)
    if podcast.audio is None:
        raise api_error(404, "no_audio", "У выпуска нет аудио.")
    return Response(
        podcast.audio,
        media_type=podcast.mime or "audio/mpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("/podcasts/{podcast_id}/send")
async def send_podcast(
    podcast_id: int, user: UserDep, state: StateDep, session: SessionDep
) -> dict[str, Any]:
    """Прислать выпуск в чат с ботом — слушать в плеере Telegram."""
    await require(state, "podcast")
    podcast = await own_podcast(session, user, podcast_id)
    if podcast.audio is None:
        raise api_error(404, "no_audio", "У выпуска нет аудио.")
    await state.bot.send_audio(
        user.id,
        BufferedInputFile(podcast.audio, filename="english-buddy-podcast.mp3"),
        caption=texts.PODCAST_CAPTION.format(title=podcast.title),
        title=podcast.title,
        performer="Jake & Emma · English Buddy",
    )
    return {"ok": True}


# --- приглашения ---


@router.get("/referral")
async def referral(user: UserDep, state: StateDep, session: SessionDep) -> dict[str, Any]:
    await require(state, "referrals")
    username = state.settings.bot_username or (await state.bot.me()).username
    invited = await session.execute(
        select(
            func.count(User.id),
            func.count(User.id).filter(User.referral_rewarded.is_(True)),
        ).where(User.referred_by == user.id)
    )
    total, rewarded = invited.one()
    return {
        "link": f"https://t.me/{username}?start=ref_{user.id}",
        "invited": total,
        "rewarded": rewarded,
        "days": journey.REFERRAL_DAYS,
        "activation": journey.REFERRAL_ACTIVATION,
    }


# --- премиум за звёзды ---


@router.get("/premium")
async def premium(user: UserDep, state: StateDep) -> dict[str, Any]:
    await require(state, "payments_stars")
    return {
        "premium": is_premium(user),
        "premium_until": user.premium_until.isoformat() if user.premium_until else None,
        "plans": [
            {
                "key": p.key,
                "title": p.title,
                "description": p.description,
                "days": p.days,
                "subscription": p.subscription,
                "stars": p.stars(state.settings),
            }
            for p in payments.PLANS.values()
        ],
    }


@router.post("/premium/{plan_key}/invoice")
async def premium_invoice(plan_key: str, user: UserDep, state: StateDep) -> dict[str, Any]:
    """Ссылка на счёт в звёздах — Mini App открывает её через Telegram.WebApp.openInvoice."""
    await require(state, "payments_stars")
    plan = payments.PLANS.get(plan_key)
    if plan is None:
        raise api_error(404, "not_found", "Нет такого тарифа.")
    return {"link": await payments.invoice_link(state.bot, state.settings, user.id, plan)}


# --- админка: фичи и источники ---


class FeatureIn(BaseModel):
    enabled: bool


async def features_view(state: WebState) -> list[dict[str, Any]]:
    snapshot = await state.features.snapshot()
    return [
        {
            "key": f.key,
            "title": f.title,
            "description": f.description,
            "where": f.where,
            "default": f.default,
            "enabled": snapshot[f.key],
        }
        for f in FEATURES.values()
    ]


@router.get("/admin/features")
async def admin_features(_: AdminDep, state: StateDep) -> dict[str, Any]:
    return {"features": await features_view(state)}


@router.post("/admin/features/{key}")
async def admin_feature(key: str, body: FeatureIn, _: AdminDep, state: StateDep) -> dict[str, Any]:
    if key not in FEATURES:
        raise api_error(404, "not_found", "Нет такой фичи.")
    await state.features.set(key, body.enabled)
    return {"features": await features_view(state)}


@router.get("/admin/sources")
async def admin_sources(
    _: AdminDep, session: SessionDep, days: Annotated[int, Query(ge=1, le=365)] = 30
) -> dict[str, Any]:
    return {
        "days": days,
        "sources": await admin_service.sources(session, days),
        "payments": await admin_service.payments_summary(session, days),
    }
