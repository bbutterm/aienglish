from datetime import datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    REAL,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    Text,
    Time,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {
        str: Text(),
        int: Integer(),
        bool: Boolean(),
        datetime: DateTime(timezone=True),
        time: Time(),
        dict[str, Any]: JSONB(),
    }


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None]
    first_name: Mapped[str | None]
    display_name: Mapped[str | None]  # как пользователь попросил себя называть
    level: Mapped[str] = mapped_column(default="B1", server_default="B1")
    estimated_level: Mapped[str | None]  # последняя оценка аналитика
    mode: Mapped[str] = mapped_column(default="chat", server_default="chat")
    # Озвучка ответов: auto — голосом на голосовые, always — всегда, off — только по кнопке 🔊
    voice_mode: Mapped[str] = mapped_column(default="auto", server_default="auto")
    persona: Mapped[str] = mapped_column(default="us_casual", server_default="us_casual")
    timezone: Mapped[str] = mapped_column(default="Europe/Moscow", server_default="Europe/Moscow")
    ping_time: Mapped[time | None]
    conversation_summary: Mapped[str | None]
    # Сообщения с id <= context_start_id уже свёрнуты в саммари (или отброшены через /reset).
    context_start_id: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    onboarded: Mapped[bool] = mapped_column(default=False, server_default=false())
    premium_until: Mapped[datetime | None]  # премиум активен, пока это время в будущем
    blocked: Mapped[bool] = mapped_column(default=False, server_default=false())
    last_review_at: Mapped[datetime | None]
    # Откуда пришёл: параметр /start (src_<кампания>, ref_<id>, landing…) — для маркетинга
    source: Mapped[str | None]
    referred_by: Mapped[int | None] = mapped_column(BigInteger)
    referral_rewarded: Mapped[bool] = mapped_column(default=False, server_default=false())
    # «Друг пишет первым»: можно выключить в настройках; после 3 сообщений без ответа — пауза
    ping_enabled: Mapped[bool] = mapped_column(default=True, server_default=true())
    last_ping_at: Mapped[datetime | None]
    pings_unanswered: Mapped[int] = mapped_column(default=0, server_default="0")
    # Ролевая миссия, которая сейчас идёт в чате: цели, выполненные цели, роль собеседника
    active_mission: Mapped[dict[str, Any] | None]
    # Подсказки первых дней, которые уже показаны (ключи из app/services/journey.py)
    hints: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    streak_days: Mapped[int] = mapped_column(default=0, server_default="0")
    last_active_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    @property
    def name(self) -> str:
        return self.display_name or self.first_name or "friend"


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_user_created", "user_id", text("created_at DESC")),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str]  # user|assistant
    kind: Mapped[str]  # text|voice|photo
    text: Mapped[str | None]  # для voice — транскрипт
    photo_caption: Mapped[str | None]  # описание фото
    topic: Mapped[str | None]  # тема разговора на этой реплике (рус., 1–3 слова) — от аналитика
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    tts_file_id: Mapped[str | None]  # кэш озвучки
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Mistake(Base):
    __tablename__ = "mistakes"
    __table_args__ = (Index("ix_mistakes_user_review", "user_id", "resolved", "next_review_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="SET NULL")
    )
    original: Mapped[str]
    corrected: Mapped[str]
    type: Mapped[str]
    explanation_ru: Mapped[str | None]
    severity: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    times_seen: Mapped[int] = mapped_column(default=1, server_default="1")
    resolved: Mapped[bool] = mapped_column(default=False, server_default=false())
    next_review_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Vocab(Base):
    __tablename__ = "vocab"
    __table_args__ = (UniqueConstraint("user_id", "term", name="uq_vocab_user_term"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    term: Mapped[str]
    translation_ru: Mapped[str | None]
    source: Mapped[str | None]  # bot|user|photo|upgrade|forward
    # Для «прокачки слов»: какое простое слово заменяет (very big) и фраза ученика с новым словом
    note: Mapped[str | None]
    example: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="new", server_default="new")
    ease: Mapped[float] = mapped_column(REAL, default=2.5, server_default="2.5")
    interval_days: Mapped[float] = mapped_column(REAL, default=0, server_default="0")
    reps: Mapped[int] = mapped_column(default=0, server_default="0")
    next_review_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Usage(Base):
    __tablename__ = "usage"
    __table_args__ = (Index("ix_usage_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    kind: Mapped[str]  # llm_companion|llm_analyst|llm_summary|stt|tts
    model: Mapped[str | None]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    audio_seconds: Mapped[float | None] = mapped_column(REAL)
    chars: Mapped[int | None]
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Lesson(Base):
    __tablename__ = "lessons"
    __table_args__ = (Index("ix_lessons_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    payload: Mapped[dict[str, Any]]  # упражнения с ответами — клиенту отдаётся без них
    # {"<номер упражнения>": {"choice": int, "correct": bool}}
    answers: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(default="new", server_default="new")  # new|done
    score: Mapped[int | None] = mapped_column(SmallInteger)
    total: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None]


class AudioCache(Base):
    """Озвучка для аудирования в уроках; ключ — хэш текста, голоса и формата."""

    __tablename__ = "audio_cache"

    key: Mapped[str] = mapped_column(primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    mime: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class FeatureFlag(Base):
    """Включённость фич из app/features.py; нет строки — действует значение по умолчанию."""

    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(primary_key=True)
    enabled: Mapped[bool]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class TopicIdea(Base):
    """«Темы для тебя»: заголовок виден сразу, содержание генерируется по нажатию."""

    __tablename__ = "topic_ideas"
    __table_args__ = (Index("ix_topic_ideas_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str]  # talk|mission
    title_ru: Mapped[str]
    title_en: Mapped[str | None]
    focus_ru: Mapped[str | None]  # что потренируешь: «Present Perfect», «сравнения»
    content: Mapped[dict[str, Any] | None]
    status: Mapped[str] = mapped_column(
        default="new", server_default="new"
    )  # new|opened|started|done
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]


class Podcast(Base):
    """«Подкаст про тебя»: сценарий и склеенная озвучка двумя голосами."""

    __tablename__ = "podcasts"
    __table_args__ = (Index("ix_podcasts_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str]
    script: Mapped[
        dict[str, Any]
    ]  # {"lines": [{"speaker": "jake", "text": "..."}], "words": [...]}
    audio: Mapped[bytes | None] = mapped_column(LargeBinary)
    mime: Mapped[str | None]
    chars: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
