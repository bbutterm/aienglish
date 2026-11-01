"""Общие зависимости API Mini App: состояние, сессия БД, пользователь из initData, админ."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import repo
from app.db.models import User
from app.features import FeatureFlags
from app.services.dialog import BackgroundTasks, DialogService
from app.services.ideas import IdeaService
from app.services.lessons import LessonService
from app.services.podcast import PodcastService
from app.web.auth import InitDataError, parse_init_data


@dataclass
class WebState:
    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    lessons: LessonService
    dialog: DialogService
    bot: Bot
    tasks: BackgroundTasks
    features: FeatureFlags
    ideas: IdeaService
    podcasts: PodcastService


def api_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message})


# --- зависимости ---


def get_state(request: Request) -> WebState:
    return request.app.state.web


async def get_session(
    state: Annotated[WebState, Depends(get_state)],
) -> AsyncIterator[AsyncSession]:
    async with state.session_factory() as session:
        yield session


async def get_user(
    request: Request,
    state: Annotated[WebState, Depends(get_state)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    header = request.headers.get("Authorization", "")
    if not header.startswith("tma "):
        raise api_error(401, "unauthorized", "Открой приложение из Telegram.")
    try:
        tg_user = parse_init_data(
            header[4:],
            state.settings.telegram_bot_token,
            timedelta(hours=state.settings.init_data_ttl_hours),
        )
    except InitDataError as exc:
        raise api_error(401, "unauthorized", "Сессия устарела — открой приложение заново.") from exc
    user = await repo.upsert_user(session, tg_user.id, tg_user.username, tg_user.first_name)
    if user.blocked:
        raise api_error(403, "blocked", "Доступ закрыт.")
    zone = request.headers.get("X-Timezone", "")
    if zone and zone != user.timezone and valid_timezone(zone):
        # Пояс устройства: по нему «друг пишет первым» молчит ночью, лимиты сбрасываются в полночь.
        user.timezone = zone
        await session.commit()
    return user


def valid_timezone(zone: str) -> bool:
    if len(zone) > 64:
        return False
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


async def get_admin(
    user: Annotated[User, Depends(get_user)], state: Annotated[WebState, Depends(get_state)]
) -> User:
    if user.id not in state.settings.admin_ids:
        raise api_error(403, "forbidden", "Раздел только для админов.")
    return user


StateDep = Annotated[WebState, Depends(get_state)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserDep = Annotated[User, Depends(get_user)]
AdminDep = Annotated[User, Depends(get_admin)]
