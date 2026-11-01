import asyncio
import logging
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import repo
from app.db.models import Message, User
from app.llm.openrouter import OpenRouterClient
from app.personas import get_persona
from app.prompts import render
from app.services.usage import record_llm

logger = logging.getLogger(__name__)


def format_transcript(messages: Sequence[Message], user_name: str, bot_name: str) -> str:
    lines = []
    for message in messages:
        speaker = user_name if message.role == "user" else bot_name
        text = message.text or ""
        if message.kind == "photo" and message.role == "user":
            text = f"[photo: {message.photo_caption or 'a photo'}] {text}".strip()
        if text:
            lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


class Memory:
    """Контекст для собеседника: саммари старой части диалога + последние N реплик (§7)."""

    def __init__(
        self,
        llm: OpenRouterClient,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._llm = llm
        self._settings = settings
        self._session_factory = session_factory
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def context(self, session: AsyncSession, user: User) -> tuple[str | None, list[Message]]:
        history = await repo.get_history(session, user, self._settings.history_window)
        return user.conversation_summary, history

    async def reset(self, session: AsyncSession, user: User) -> None:
        user.conversation_summary = None
        user.context_start_id = await repo.last_message_id(session, user.id)
        await session.commit()

    async def maybe_summarize(self, user_id: int) -> None:
        """Когда живая история длиннее summary_trigger — сворачивает старую часть в саммари."""
        lock = self._locks[user_id]
        if lock.locked():
            return
        async with lock:
            async with self._session_factory() as session:
                user = await session.get(User, user_id)
                if user is None:
                    return
                if await repo.count_live_messages(session, user) <= self._settings.summary_trigger:
                    return
                live = await repo.get_live_messages(session, user)
            old = live[: -self._settings.history_window]
            if not old:
                return

            # Запрос к модели — без открытого соединения с БД.
            persona = get_persona(user.persona)
            transcript = format_transcript(old, user.name, persona.name)
            prompt = render("summary_system.md.j2", persona_name=persona.name, user_name=user.name)
            content = (
                f"Current summary: {user.conversation_summary or '(empty)'}\n\n"
                f"New messages:\n{transcript}"
            )
            result = await self._llm.chat(
                [{"role": "system", "content": prompt}, {"role": "user", "content": content}],
                models=[self._settings.summary_model],
                temperature=0.3,
                max_tokens=1024,
                reasoning_effort=self._settings.analyst_reasoning_effort or None,
            )
            async with self._session_factory() as session:
                # Compare-and-set: если пока считали саммари, был /reset — не затираем.
                updated = await session.execute(
                    update(User)
                    .where(User.id == user_id, User.context_start_id == user.context_start_id)
                    .values(conversation_summary=result.text, context_start_id=old[-1].id)
                )
                await record_llm(session, user_id, "llm_summary", result)
                await session.commit()
            if updated.rowcount:
                logger.info("Summarized %d messages for user %s", len(old), user_id)
