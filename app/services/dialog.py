import asyncio
import enum
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any, Literal

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile
from aiogram.types import Message as TgMessage
from aiogram.utils.chat_action import ChatActionSender
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import repo
from app.db.models import Message, User
from app.personas import get_persona
from app.services.analyst import Analyst
from app.services.companion import Companion, OpenerReason
from app.services.memory import Memory
from app.services.plans import limits_for
from app.services.stt import SpeechToText
from app.services.tts import TextToSpeech, TTSError, prepare_tts_text
from app.services.usage import record_llm, record_usage, stt_cost, tts_cost

logger = logging.getLogger(__name__)


class BackgroundTasks:
    """Фоновые задачи (аналитик, саммари): держим ссылки и логируем падения."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(self._guard(coro, name), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @staticmethod
    async def _guard(coro: Coroutine[Any, Any, Any], name: str) -> Any:
        try:
            return await coro
        except Exception:
            logger.exception("Background task %s failed", name)
            return None

    async def drain(self, timeout: float = 30) -> None:
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)


VOICE_OFFER_AFTER_TAPS = 2


def wants_voice(user: User, input_kind: str | None) -> bool:
    """Отвечать ли голосом: always — всегда, auto — на голосовые, off — только по кнопке."""
    if user.voice_mode == "always":
        return True
    return user.voice_mode == "auto" and input_kind == "voice"


class SpeakStatus(enum.Enum):
    SENT = "sent"
    LIMIT = "limit"
    FAILED = "failed"


@dataclass
class Turn:
    reply: Message
    analysis: asyncio.Task[Any] | None


class DialogService:
    """MessageService из дизайна: оркестрация текста, голоса, фото и озвучки."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        companion: Companion,
        analyst: Analyst,
        memory: Memory,
        stt: SpeechToText,
        tts: TextToSpeech,
        tasks: BackgroundTasks,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.companion = companion
        self.analyst = analyst
        self.memory = memory
        self.stt = stt
        self.tts = tts
        self.tasks = tasks
        self._tts_limit_notified: set[tuple[int, date]] = set()
        self._tts_taps: dict[tuple[int, date], int] = {}
        self._voice_offered: set[int] = set()

    # --- лимиты ---

    async def messages_left(self, session: AsyncSession, user: User) -> int:
        since = repo.day_start_utc(user.timezone)
        used = await repo.count_user_messages_since(session, user.id, since)
        return limits_for(user, self.settings).messages - used

    # --- диалог ---

    async def user_turn(
        self,
        session: AsyncSession,
        user: User,
        *,
        kind: Literal["text", "voice", "photo"],
        text: str | None,
        image: bytes | None = None,
        tg_message_id: int | None = None,
    ) -> Turn:
        summary, history = await self.memory.context(session, user)
        user_message = await repo.add_message(
            session,
            user_id=user.id,
            role="user",
            kind=kind,
            text=text or None,
            tg_message_id=tg_message_id,
        )
        # Реплику пользователя сохраняем, даже если собеседник упадёт.
        await session.commit()

        reply = await self.companion.reply(user, history, summary, text=text, image=image)
        if kind == "photo":
            user_message.photo_caption = reply.photo_description or text or "a photo"
        assistant = await repo.add_message(
            session, user_id=user.id, role="assistant", kind="text", text=reply.text
        )
        await record_llm(session, user.id, "llm_companion", reply.llm)
        await session.commit()

        analysis = None
        if text and text.strip():
            prev_bot = history[-1].text if history and history[-1].role == "assistant" else None
            analysis = self.tasks.spawn(
                self._analyze(
                    user_id=user.id,
                    level=user.level,
                    message_id=user_message.id,
                    user_text=text,
                    prev_bot_text=prev_bot,
                    bot_reply=reply.text,
                    kind=kind,
                ),
                name=f"analyst:{user.id}",
            )
        self.tasks.spawn(self.memory.maybe_summarize(user.id), name=f"summary:{user.id}")
        return Turn(reply=assistant, analysis=analysis)

    async def opener(
        self,
        session: AsyncSession,
        user: User,
        reason: OpenerReason,
        topic: str | None = None,
    ) -> Message:
        summary, history = await self.memory.context(session, user)
        reply = await self.companion.opener(user, history, summary, reason=reason, topic=topic)
        message = await repo.add_message(
            session, user_id=user.id, role="assistant", kind="text", text=reply.text
        )
        await record_llm(session, user.id, "llm_companion", reply.llm)
        await session.commit()
        return message

    async def _analyze(
        self,
        *,
        user_id: int,
        level: str,
        message_id: int,
        user_text: str,
        prev_bot_text: str | None,
        bot_reply: str,
        kind: str,
    ) -> None:
        result = await self.analyst.analyze(
            level=level,
            user_text=user_text,
            prev_bot_text=prev_bot_text,
            bot_reply=bot_reply,
            is_voice=kind == "voice",
        )
        analysis = result.analysis
        vocab = analysis.vocab
        if kind == "photo":
            vocab = [replace(v, source="photo") if v.source == "bot" else v for v in vocab]
        async with self.session_factory() as session:
            await repo.save_mistakes(session, user_id, message_id, analysis.mistakes)
            await repo.save_vocab(session, user_id, vocab)
            if analysis.estimated_level:
                await session.execute(
                    update(User)
                    .where(User.id == user_id)
                    .values(estimated_level=analysis.estimated_level)
                )
            await record_llm(session, user_id, "llm_analyst", result.llm)
            await session.commit()
        logger.info(
            "Analyst: user %s, %d mistakes, %d vocab",
            user_id,
            len(analysis.mistakes),
            len(vocab),
        )

    # --- голос ---

    async def transcribe(
        self, session: AsyncSession, user: User, audio: bytes, duration: float
    ) -> str:
        text = await self.stt.transcribe(audio)
        await record_usage(
            session,
            user_id=user.id,
            kind="stt",
            model=self.settings.stt_model,
            audio_seconds=duration,
            cost_usd=stt_cost(duration, self.settings),
        )
        await session.commit()
        return text

    async def speak(
        self,
        bot: Bot,
        chat_id: int,
        session: AsyncSession,
        user: User,
        message: Message,
        *,
        caption: str | None = None,
        notify_limit: bool = False,
    ) -> SpeakStatus:
        """Озвучка ответа: кэш file_id → лимит → ElevenLabs TTS → sendVoice.

        С caption голосовое и есть сам ответ (текст в подписи) — его id запоминаем в message.
        """
        if message.tts_file_id:
            sent = await self._send_voice(bot, chat_id, message.tts_file_id, caption)
            if caption is not None:
                message.tg_message_id = sent.message_id
                await session.commit()
            return SpeakStatus.SENT

        since = repo.day_start_utc(user.timezone)
        used = await repo.count_usage_since(session, user.id, "tts", since)
        if used >= limits_for(user, self.settings).tts:
            if notify_limit:
                key = (user.id, since.date())
                if key not in self._tts_limit_notified:
                    self._tts_limit_notified.add(key)
                    await bot.send_message(
                        chat_id, "🔇 Лимит голосовых ответов на сегодня исчерпан — дальше текстом."
                    )
            return SpeakStatus.LIMIT

        text = prepare_tts_text(message.text or "", self.settings.tts_max_chars)
        if not text:
            return SpeakStatus.FAILED
        voice_id = get_persona(user.persona).voice_id(self.settings)
        async with ChatActionSender.record_voice(bot=bot, chat_id=chat_id):
            try:
                audio = await self.tts.synthesize(text, voice_id)
            except TTSError:
                logger.exception("TTS failed for message %s", message.id)
                return SpeakStatus.FAILED
        await record_usage(
            session,
            user_id=user.id,
            kind="tts",
            model=self.settings.tts_model,
            chars=len(text),
            cost_usd=tts_cost(len(text), self.settings),
        )
        try:
            file = BufferedInputFile(audio, filename="reply.ogg")
            sent = await self._send_voice(bot, chat_id, file, caption)
            if sent.voice:
                message.tts_file_id = sent.voice.file_id
            if caption is not None:
                message.tg_message_id = sent.message_id
        finally:
            await session.commit()
        return SpeakStatus.SENT

    @staticmethod
    async def _send_voice(
        bot: Bot, chat_id: int, voice: BufferedInputFile | str, caption: str | None
    ) -> TgMessage:
        try:
            return await bot.send_voice(chat_id, voice, caption=caption)
        except TelegramBadRequest as exc:
            # Premium-пользователь запретил голосовые от ботов — отправляем как аудиофайл.
            if "VOICE_MESSAGES_FORBIDDEN" not in str(exc) or isinstance(voice, str):
                raise
            return await bot.send_audio(chat_id, voice, caption=caption)

    def should_offer_voice(self, user: User) -> bool:
        """После второго нажатия 🔊 за день — один раз предложить озвучивать без кнопки."""
        if user.voice_mode == "always" or user.id in self._voice_offered:
            return False
        today = repo.day_start_utc(user.timezone).date()
        key = (user.id, today)
        self._tts_taps = {k: v for k, v in self._tts_taps.items() if k[1] == today}
        self._tts_taps[key] = self._tts_taps.get(key, 0) + 1
        if self._tts_taps[key] < VOICE_OFFER_AFTER_TAPS:
            return False
        self._voice_offered.add(user.id)
        return True

    # --- режим review ---

    async def review_due(self, session: AsyncSession, user: User) -> bool:
        if user.mode != "review":
            return False
        since = user.last_review_at or user.created_at or datetime.now(UTC)
        count = await repo.count_user_messages_since(session, user.id, since)
        return count >= self.settings.review_every
