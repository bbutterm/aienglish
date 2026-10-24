"""«Подкаст про тебя»: короткий выпуск, где Jake и Emma двумя голосами обсуждают неделю ученика."""

import asyncio
import json
import logging
import shutil
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Message, Mistake, Podcast, User, Vocab
from app.llm.jsonutil import extract_json_object
from app.llm.openrouter import LLMError, OpenRouterClient
from app.personas import PERSONAS
from app.prompts import render
from app.services.plans import is_premium
from app.services.tts import TextToSpeech, TTSError
from app.services.usage import record_llm, record_usage, tts_cost

logger = logging.getLogger(__name__)

SPEAKERS = {"jake": "us_casual", "emma": "uk_casual"}
MAX_LINES = 18
MAX_CHARS = 1800
PER_WEEK = 1
PREMIUM_PER_WEEK = 7
TTS_CONCURRENCY = 3
PAUSE_SECONDS = 0.35


class PodcastError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def clean_script(raw: dict[str, Any]) -> dict[str, Any]:
    """Сценарий от модели → реплики двух ведущих в пределах лимита символов."""
    lines: list[dict[str, str]] = []
    total = 0
    for item in raw.get("lines") or []:
        if not isinstance(item, dict):
            continue
        speaker = str(item.get("speaker") or "").strip().lower()
        text = item.get("text")
        if speaker not in SPEAKERS or not isinstance(text, str) or not text.strip():
            continue
        text = " ".join(text.split())[:300]
        if total + len(text) > MAX_CHARS:
            break
        lines.append({"speaker": speaker, "text": text})
        total += len(text)
        if len(lines) >= MAX_LINES:
            break
    if len(lines) < 4:
        raise ValueError("podcast script is too short")
    words = [
        {"en": w["en"].strip()[:80], "ru": str(w.get("ru") or "").strip()[:80]}
        for w in raw.get("words") or []
        if isinstance(w, dict) and isinstance(w.get("en"), str) and w["en"].strip()
    ][:5]
    title = raw.get("title")
    question = raw.get("question")
    return {
        "title": title.strip()[:80] if isinstance(title, str) and title.strip() else "Твоя неделя",
        "lines": lines,
        "words": words,
        "question": question.strip()[:300] if isinstance(question, str) else None,
    }


def podcast_view(podcast: Podcast) -> dict[str, Any]:
    return {
        "id": podcast.id,
        "title": podcast.title,
        "created_at": podcast.created_at.isoformat() if podcast.created_at else None,
        "lines": podcast.script.get("lines", []),
        "words": podcast.script.get("words", []),
        "question": podcast.script.get("question"),
        "has_audio": podcast.audio is not None,
    }


async def join_mp3(parts: list[bytes]) -> bytes:
    """Склейка реплик с короткими паузами через ffmpeg; без ffmpeg — просто подряд."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg and len(parts) > 1:
        with tempfile.TemporaryDirectory() as tmp:
            inputs: list[str] = []
            for i, part in enumerate(parts):
                path = Path(tmp) / f"{i:02d}.mp3"
                path.write_bytes(part)
                inputs += ["-i", str(path)]
            n = len(parts)
            # Каждую реплику дополняем тишиной в конце и склеиваем в один mp3.
            pads = "".join(f"[{i}:a]apad=pad_dur={PAUSE_SECONDS}[a{i}];" for i in range(n))
            joined = "".join(f"[a{i}]" for i in range(n))
            out = Path(tmp) / "out.mp3"
            proc = await asyncio.create_subprocess_exec(
                ffmpeg,
                "-loglevel",
                "error",
                *inputs,
                "-filter_complex",
                f"{pads}{joined}concat=n={n}:v=0:a=1[out]",
                "-map",
                "[out]",
                "-ac",
                "1",
                "-b:a",
                "64k",
                str(out),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0 and out.exists():
                return out.read_bytes()
            logger.warning("ffmpeg podcast join failed: %s", stderr.decode(errors="ignore")[:300])
    return b"".join(parts)


class PodcastService:
    def __init__(self, llm: OpenRouterClient, tts: TextToSpeech, settings: Settings) -> None:
        self._llm = llm
        self._tts = tts
        self._settings = settings
        self._locks: dict[int, asyncio.Lock] = {}

    async def recent(self, session: AsyncSession, user: User, limit: int = 5) -> list[Podcast]:
        rows = await session.scalars(
            select(Podcast)
            .where(Podcast.user_id == user.id)
            .order_by(Podcast.id.desc())
            .limit(limit)
        )
        return list(rows)

    async def remaining(self, session: AsyncSession, user: User) -> int:
        since = datetime.now(UTC) - timedelta(days=7)
        made = await session.scalar(
            select(func.count(Podcast.id)).where(
                Podcast.user_id == user.id, Podcast.created_at >= since
            )
        )
        limit = PREMIUM_PER_WEEK if is_premium(user) else PER_WEEK
        return max(0, limit - (made or 0))

    async def create(self, session: AsyncSession, user: User) -> Podcast:
        if not user.onboarded:
            raise PodcastError("not_onboarded")
        lock = self._locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            if await self.remaining(session, user) <= 0:
                raise PodcastError("limit_reached")
            script = await self._script(session, user)
            audio, chars = await self._voice(session, user, script["lines"])
            podcast = Podcast(
                user_id=user.id,
                title=script["title"],
                script=script,
                audio=audio,
                mime="audio/mpeg" if audio else None,
                chars=chars,
            )
            session.add(podcast)
            await session.commit()
            return podcast

    async def _script(self, session: AsyncSession, user: User) -> dict[str, Any]:
        system = render("podcast_system.md.j2", user_name=user.name, level=user.level)
        try:
            result = await self._llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": await self._material(session, user)},
                ],
                models=[self._settings.lesson_model, self._settings.companion_fallback_model],
                temperature=0.9,
                max_tokens=3000,
                json_mode=True,
                reasoning_effort=self._settings.analyst_reasoning_effort or None,
            )
        except LLMError as exc:
            raise PodcastError("generation_failed") from exc
        await record_llm(session, user.id, "llm_podcast", result)
        try:
            return clean_script(extract_json_object(result.text))
        except ValueError as exc:
            await session.commit()
            raise PodcastError("generation_failed") from exc

    async def _voice(
        self, session: AsyncSession, user: User, lines: list[dict[str, str]]
    ) -> tuple[bytes | None, int]:
        semaphore = asyncio.Semaphore(TTS_CONCURRENCY)
        audio_format = self._settings.lesson_audio_format

        async def speak(line: dict[str, str]) -> bytes:
            voice = PERSONAS[SPEAKERS[line["speaker"]]].voice_id(self._settings)
            async with semaphore:
                return await self._tts.synthesize_raw(line["text"], voice, audio_format)

        try:
            parts = await asyncio.gather(*(speak(line) for line in lines))
        except TTSError:
            logger.warning("Podcast TTS failed for %s", user.id, exc_info=True)
            await session.commit()
            raise PodcastError("audio_failed") from None
        chars = sum(len(line["text"]) for line in lines)
        await record_usage(
            session,
            user_id=user.id,
            kind="tts_podcast",
            model=self._settings.tts_model,
            chars=chars,
            cost_usd=tts_cost(chars, self._settings),
        )
        return await join_mp3(list(parts)), chars

    async def _material(self, session: AsyncSession, user: User) -> str:
        since = datetime.now(UTC) - timedelta(days=7)
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
        lines = await session.scalars(
            select(Message.text)
            .where(
                Message.user_id == user.id,
                Message.role == "user",
                Message.text.is_not(None),
                Message.created_at >= since,
            )
            .order_by(Message.id.desc())
            .limit(12)
        )
        mistakes = await session.scalars(
            select(Mistake)
            .where(Mistake.user_id == user.id, Mistake.created_at >= since)
            .order_by(Mistake.severity.desc(), Mistake.times_seen.desc())
            .limit(6)
        )
        words = await session.scalars(
            select(Vocab)
            .where(Vocab.user_id == user.id, Vocab.created_at >= since)
            .order_by(Vocab.id.desc())
            .limit(8)
        )
        words = list(words)
        return json.dumps(
            {
                "listener": {"name": user.name, "level": user.level},
                "what_they_know_about_the_listener": user.conversation_summary,
                "topics_this_week": [t for t, _ in topics.most_common(5)],
                "some_things_they_said": list(reversed(list(lines))),
                "mistakes": [f"{m.original} → {m.corrected}" for m in mistakes],
                "new_words": [
                    f"{w.term} ({w.translation_ru})" for w in words if w.source != "upgrade"
                ],
                "plain_words_upgrades": [
                    f"{w.note} → {w.term}" for w in words if w.source == "upgrade"
                ],
            },
            ensure_ascii=False,
        )
