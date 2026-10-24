"""Что происходит после ответа собеседника: прогресс миссии, приглашения, подсказки, слова с фото.

Ответ уже отправлен — здесь можно спокойно дождаться аналитика.
"""

import asyncio
import logging
from datetime import UTC, datetime
from html import escape

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards import level_hint_kb, settings_kb, webapp_kb
from app.db import repo
from app.db.models import Message, Mistake, TopicIdea, User
from app.services import journey
from app.services.dialog import DialogService, Turn
from app.services.missions import goals_list_ru, is_complete, new_mission
from app.services.usage import record_llm

logger = logging.getLogger(__name__)

ANALYST_WAIT_SECONDS = 20


async def analysis_result(turn: Turn) -> list[int] | None:
    """Ждём фонового аналитика этой реплики; None — не дождались или он упал."""
    if turn.analysis is None:
        return None
    done, _ = await asyncio.wait({turn.analysis}, timeout=ANALYST_WAIT_SECONDS)
    if not done:
        return None
    return turn.analysis.result()


def app_button(
    dialog: DialogService, label: str, page: str | None = None
) -> InlineKeyboardMarkup | None:
    url = dialog.settings.webapp_url
    return webapp_kb(url, label, page) if url else None


async def after_turn(
    bot: Bot,
    chat_id: int,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
    turn: Turn,
) -> None:
    flags = await dialog.features.snapshot()
    if user.active_mission and flags["missions"]:
        await mission_progress(bot, chat_id, session, user, dialog, turn)
        return  # во время сцены не отвлекаем подсказками
    messages = await journey.user_messages(session, user.id)
    # Дальше может быть ожидание аналитика до 20 с — соединение с БД не держим.
    await session.commit()
    if flags["referrals"]:
        await referral_reward(bot, session, user, messages)
    if flags["first_steps"]:
        await first_steps(bot, chat_id, session, user, dialog, turn, messages)


# --- миссии ---


async def mission_progress(
    bot: Bot,
    chat_id: int,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
    turn: Turn,
) -> None:
    await session.commit()  # ждём аналитика без открытой транзакции
    new_goals = await analysis_result(turn)
    await session.refresh(user, ["active_mission"])
    mission = user.active_mission
    if not mission or not new_goals:
        return
    total = len(mission["goals"])
    if not is_complete(mission):
        before = len(mission["done"]) - len(new_goals)
        for step, index in enumerate(new_goals, start=1):
            await bot.send_message(
                chat_id,
                texts.MISSION_GOAL.format(
                    done=before + step, total=total, goal=escape(mission["goals"][index]["ru"])
                ),
            )
        return
    await finish_mission(bot, chat_id, session, user, dialog, completed=True)


async def finish_mission(
    bot: Bot,
    chat_id: int,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
    *,
    completed: bool,
) -> None:
    mission = user.active_mission
    if not mission:
        return
    user.active_mission = None
    idea_id = mission.get("idea_id")
    idea = await session.get(TopicIdea, idea_id) if idea_id else None
    if idea is not None and completed:
        idea.status = "done"
    await session.commit()
    if not completed:
        await bot.send_message(
            chat_id,
            texts.MISSION_STOPPED.format(
                title=escape(mission["title_ru"]),
                done=len(mission["done"]),
                total=len(mission["goals"]),
            ),
        )
        return
    started = mission.get("started_at")
    messages = 0
    if started:
        messages = (
            await session.scalar(
                select(func.count(Message.id)).where(
                    Message.user_id == user.id,
                    Message.role == "user",
                    Message.created_at >= datetime.fromisoformat(started),
                )
            )
        ) or 0
    mistakes = max(
        0,
        (
            await session.scalar(select(func.count(Mistake.id)).where(Mistake.user_id == user.id))
            or 0
        )
        - mission.get("mistakes_at_start", 0),
    )
    await bot.send_message(
        chat_id,
        texts.MISSION_DONE.format(
            title=escape(mission["title_ru"]),
            goals=goals_list_ru(mission),
            messages=messages,
            mistakes=mistakes,
        ),
        reply_markup=app_button(dialog, texts.MISSION_APP_BUTTON),
    )


# --- приглашения ---


async def referral_reward(bot: Bot, session: AsyncSession, user: User, messages: int) -> None:
    referrer = await journey.reward_referral(session, user, messages)
    if referrer is None:
        return
    days = journey.REFERRAL_DAYS
    for target, text in (
        (user, texts.REFERRAL_REWARD_NEW.format(days=days, until=f"{user.premium_until:%d.%m}")),
        (
            referrer,
            texts.REFERRAL_REWARD.format(
                name=escape(user.name), days=days, until=f"{referrer.premium_until:%d.%m}"
            ),
        ),
    ):
        try:
            await bot.send_message(target.id, text)
        except Exception:  # пригласивший мог заблокировать бота — награда всё равно выдана
            logger.warning("Could not notify %s about referral reward", target.id, exc_info=True)


# --- подсказки первых дней ---


async def first_steps(
    bot: Bot,
    chat_id: int,
    session: AsyncSession,
    user: User,
    dialog: DialogService,
    turn: Turn,
    messages: int,
) -> None:
    app = bool(dialog.settings.webapp_url)
    if journey.required_hints(app) <= set(user.hints) or messages < journey.INSIGHT_AFTER:
        return
    # Подсказки опираются на разбор этой реплики: ошибки, простые слова, оценку уровня.
    await analysis_result(turn)
    await session.refresh(user, ["estimated_level"])
    mistakes, upgrades = [], []
    if journey.HINT_INSIGHT not in user.hints:
        mistakes, upgrades = await journey.insight_items(session, user)
    has_insight = bool(mistakes or upgrades)

    hint = journey.next_hint(user, messages, has_insight, app=app)
    if hint is None or not await journey.mark_hint(session, user, hint):
        return
    if hint == journey.HINT_INSIGHT:
        if not has_insight:
            return  # за 10 реплик ни одной ошибки — просто не показываем
        items = [f"✏️ <s>{escape(m.original)}</s> → <b>{escape(m.corrected)}</b>" for m in mistakes]
        items += [f"💡 {escape(v.note or '')} → <b>{escape(v.term)}</b>" for v in upgrades]
        await bot.send_message(
            chat_id,
            texts.HINT_INSIGHT.format(items="\n".join(items)),
            reply_markup=app_button(dialog, texts.HINT_INSIGHT_LESSON),
        )
    elif hint == journey.HINT_LEVEL:
        if not journey.level_gap(user.level, user.estimated_level):
            return
        await bot.send_message(
            chat_id,
            texts.HINT_LEVEL.format(estimated=user.estimated_level, level=user.level),
            reply_markup=level_hint_kb(user.estimated_level, user.level),
        )
    elif hint == journey.HINT_APP:
        topic = await session.scalar(
            select(Message.topic)
            .where(Message.user_id == user.id, Message.topic.is_not(None))
            .order_by(Message.id.desc())
            .limit(1)
        )
        await bot.send_message(
            chat_id,
            texts.HINT_APP.format(topic=f" про «{escape(topic)}»" if topic else ""),
            reply_markup=app_button(dialog, texts.HINT_INSIGHT_LESSON),
        )
    elif hint == journey.HINT_SETTINGS:
        ping = await dialog.features.enabled("friend_ping")
        await bot.send_message(chat_id, texts.HINT_SETTINGS, reply_markup=settings_kb(user, ping))


# --- слова с фото ---


async def photo_words(
    bot: Bot, chat_id: int, user_id: int, dialog: DialogService, image: bytes
) -> None:
    """Фоновая задача: 5 слов с фото в карточки и короткое сообщение со списком."""
    async with dialog.session_factory() as session:
        user = await session.get(User, user_id)
    if user is None:
        return
    # Запрос к модели — без открытого соединения с БД.
    extracted = await dialog.extractor.from_photo(user, image)
    async with dialog.session_factory() as session:
        await record_llm(session, user.id, "llm_extract", extracted.llm)
        await repo.save_vocab(session, user.id, extracted.words)
        await session.commit()
    if not extracted.words:
        return
    words = "\n".join(
        f"• <b>{escape(w.term)}</b> — {escape(w.translation_ru or '')}" for w in extracted.words
    )
    await bot.send_message(
        chat_id,
        texts.PHOTO_WORDS.format(words=words),
        reply_markup=app_button(dialog, texts.WORDS_BUTTON, "words"),
    )


async def start_mission(
    bot: Bot, session: AsyncSession, user: User, dialog: DialogService, idea: TopicIdea
) -> None:
    """Миссия из «Тем для тебя»: карточка сцены с целями и первая реплика собеседника в роли."""
    from app.bot.handlers.common import send_opener  # common импортирует этот модуль

    await session.refresh(user, ["active_mission"], with_for_update=True)
    if user.active_mission:
        await session.commit()
        return  # параллельный запуск уже начал сцену
    mistakes = (
        await session.scalar(select(func.count(Mistake.id)).where(Mistake.user_id == user.id))
    ) or 0
    user.active_mission = new_mission(idea.id, idea.title_ru, idea.content, mistakes)
    idea.status = "started"
    idea.started_at = datetime.now(UTC)
    await session.commit()
    content = idea.content
    await bot.send_message(
        user.id,
        texts.MISSION_START.format(
            title=escape(idea.title_ru),
            setting=escape(content.get("setting_ru") or ""),
            goals="\n".join(f"{i + 1}. {escape(g['ru'])}" for i, g in enumerate(content["goals"])),
        ),
    )
    await send_opener(bot, user.id, session, user, dialog, "mission")


async def start_talk(
    bot: Bot, session: AsyncSession, user: User, dialog: DialogService, idea: TopicIdea
) -> None:
    """Тема из «Тем для тебя»: собеседник сам заводит разговор о ней."""
    from app.bot.handlers.common import send_opener

    idea.status = "started"
    idea.started_at = datetime.now(UTC)
    await session.commit()
    await send_opener(bot, user.id, session, user, dialog, "idea", idea.title_en or idea.title_ru)
