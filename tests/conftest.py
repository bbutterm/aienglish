import json
import os
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, GetFile, SendAudio, SendMessage, SendVoice
from aiogram.methods.base import TelegramMethod
from aiogram.types import Chat, File, Message, Voice
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Base
from app.db.session import create_engine, create_session_factory

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
OGG_BYTES = b"OggS" + b"\x00" * 64


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        telegram_bot_token="123456:TEST",
        openrouter_api_key="or-test",
        elevenlabs_api_key="el-test",
        admin_ids="1, 2",
        database_url=TEST_DATABASE_URL or "postgresql+asyncpg://unused/unused",
    )


# --- база данных (нужен TEST_DATABASE_URL) ---


@pytest.fixture(scope="session")
async def engine() -> AsyncGenerator[AsyncEngine]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    engine = create_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE users, messages, mistakes, vocab, usage RESTART IDENTITY CASCADE")
        )
    return create_session_factory(engine)


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    async with session_factory() as session:
        yield session


# --- фейковые внешние API ---


def openrouter_response(content: str, model: str = "test/model", cost: float = 0.0001) -> dict:
    return {
        "id": "gen-1",
        "model": model,
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": cost},
    }


class FakeAPIs:
    """OpenRouter + ElevenLabs за одним httpx.MockTransport."""

    def __init__(self) -> None:
        self.llm_requests: list[dict[str, Any]] = []
        self.stt_requests: list[httpx.Request] = []
        self.tts_requests: list[dict[str, Any]] = []
        self.companion_reply = "That sounds fun! What did you watch?"
        self.stt_text = "I go to cinema yesterday"

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            body = json.loads(request.content)
            self.llm_requests.append(body)
            return httpx.Response(200, json=openrouter_response(self._llm(body), body["model"]))
        if request.url.path == "/v1/speech-to-text":
            self.stt_requests.append(request)
            return httpx.Response(200, json={"text": self.stt_text, "language_code": "en"})
        if request.url.path.startswith("/v1/text-to-speech/"):
            self.tts_requests.append(
                {
                    "path": request.url.path,
                    "params": dict(request.url.params),
                    **json.loads(request.content),
                }
            )
            return httpx.Response(200, content=OGG_BYTES)
        return httpx.Response(404)

    def _llm(self, body: dict[str, Any]) -> str:
        system = body["messages"][0]["content"]
        if body.get("response_format"):
            learner = body["messages"][-1]["content"]
            mistakes = []
            if "I go to cinema" in learner:
                mistakes.append(
                    {
                        "original": "I go to cinema yesterday",
                        "corrected": "I went to the cinema yesterday",
                        "type": "tense",
                        "explanation_ru": "Прошедшее время: go → went; cinema с артиклем the",
                        "severity": 2,
                    }
                )
            return json.dumps(
                {
                    "mistakes": mistakes,
                    "vocab": [
                        {"term": "Blockbuster", "translation_ru": "блокбастер", "source": "bot"}
                    ],
                    "estimated_level": "A2",
                },
                ensure_ascii=False,
            )
        if system.startswith("You maintain the running memory"):
            return "Alex likes movies and lives in Moscow."
        last = body["messages"][-1]["content"]
        if isinstance(last, list):
            return "[photo: a dog running on a beach]\nWhat a happy dog! Is it yours?"
        return self.companion_reply

    def companion_requests(self) -> list[dict[str, Any]]:
        return [
            r
            for r in self.llm_requests
            if not r.get("response_format")
            and not r["messages"][0]["content"].startswith("You maintain the running memory")
        ]


@pytest.fixture
def fake_apis() -> FakeAPIs:
    return FakeAPIs()


@pytest.fixture
async def http(fake_apis: FakeAPIs) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake_apis.handler)) as client:
        yield client


class FakeTelegramSession(BaseSession):
    """Записывает вызовы Bot API и отвечает правдоподобными объектами."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._message_id = 1000

    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        self.requests.append(method)
        if isinstance(method, SendMessage | SendVoice | SendAudio | EditMessageText):
            self._message_id += 1
            fields: dict[str, Any] = {
                "message_id": self._message_id,
                "date": datetime.now(UTC),
                "chat": Chat(id=method.chat_id or 0, type="private"),
            }
            if isinstance(method, SendVoice):
                fields["voice"] = Voice(
                    file_id=f"voice-{self._message_id}", file_unique_id="u", duration=2
                )
            else:
                fields["text"] = getattr(method, "text", None)
            return Message(**fields)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path="voice/file.oga")
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncIterator[bytes]:
        yield OGG_BYTES

    async def close(self) -> None:
        return None

    def of_type[T](self, method_type: type[T]) -> list[T]:
        return [r for r in self.requests if isinstance(r, method_type)]
