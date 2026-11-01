"""Регрессии аудита 2026-10-25: сбои внешних API, деньги и лимиты, гонки, безопасность.

Все внешние API — фейковые (httpx.MockTransport и FakeTelegramSession).
"""

import asyncio
import json
import os
import random
from typing import Any

import httpx
import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, SendVoice
from aiogram.types import Voice
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import app.llm.openrouter as openrouter_module
import app.services.stt as stt_module
import app.services.tts as tts_module
from app.bot import texts
from app.bot.keyboards import TtsCb
from app.db import repo
from app.db.models import AudioCache, Message, Usage
from app.llm.jsonutil import extract_json_object
from app.services.companion import split_photo_tag
from app.services.lessons import clean_exercise
from app.services.tts import prepare_tts_text
from tests.conftest import FakeAPIs, is_lesson_request, openrouter_response
from tests.test_bot_flow import USER_ID, Harness, harness, onboard
from tests.test_web_api import Web, seed_user, web


class HookedAPIs(FakeAPIs):
    """FakeAPIs + хук на тест: hook(request) → Response | None (None — обычный фейковый ответ)."""

    def __init__(self) -> None:
        super().__init__()
        self.hook: Any = None
        self.paths: list[str] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:  # type: ignore[override]
        self.paths.append(request.url.path)
        if self.hook is not None:
            result = self.hook(request)
            if hasattr(result, "__await__"):
                result = await result
            if result is not None:
                return result
        return super().handler(request)


@pytest.fixture
def fake_apis() -> HookedAPIs:
    return HookedAPIs()


@pytest.fixture
def no_sleep(monkeypatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    for module in (openrouter_module, stt_module, tts_module):
        monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    return slept


def system_of(request: httpx.Request) -> str:
    return json.loads(request.content)["messages"][0]["content"]


def is_companion(request: httpx.Request) -> bool:
    if request.url.host != "openrouter.ai":
        return False
    body = json.loads(request.content)
    return not body.get("response_format") and not system_of(request).startswith(
        "You maintain the running memory"
    )


def is_tts(request: httpx.Request) -> bool:
    return request.url.path.startswith("/v1/text-to-speech/")


VOICE = Voice(file_id="v", file_unique_id="u", duration=3)


# --- 1. голос и текст при сбоях внешних API ---


@pytest.fixture
async def idle_probe():
    """Сколько соединений «idle in transaction» видно в момент внешнего вызова."""
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"], poolclass=NullPool)
    seen: dict[str, int] = {}

    async def check(label: str) -> None:
        # Внешний вызов «длится» 0,2 с: держащий транзакцию простаивает дольше 0,1 с.
        await asyncio.sleep(0.2)
        async with engine.connect() as conn:
            idle = await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND state = 'idle in transaction' "
                    "AND now() - state_change > interval '100 milliseconds'"
                )
            )
        seen[label] = max(seen.get(label, 0), idle)

    yield check, seen
    await engine.dispose()


async def test_no_open_transaction_while_waiting_for_apis(
    harness: Harness, fake_apis: HookedAPIs, settings, idle_probe
) -> None:
    """Пока ждём STT, TTS, собеседника и саммари, соединение с БД не висит в транзакции."""
    check, seen = idle_probe
    h = harness
    await onboard(h)
    async with h.session_factory() as session:
        for i in range(settings.summary_trigger + 2):
            await repo.add_message(session, user_id=USER_ID, role="user", kind="text", text=f"{i}")
        await session.commit()

    async def hook(request: httpx.Request) -> None:
        if request.url.path == "/v1/speech-to-text":
            await check("stt")
        elif is_tts(request):
            await check("tts")
        elif request.url.host == "openrouter.ai":
            if system_of(request).startswith("You maintain the running memory"):
                await check("summary")
            elif is_companion(request):
                last = json.loads(request.content)["messages"][-1]["content"]
                opener = isinstance(last, str) and last.startswith("[Chat note")
                await check("opener" if opener else "companion")

    fake_apis.hook = hook
    await h.feed(voice=VOICE)
    await h.feed(text="/reset")
    assert seen == {"stt": 0, "companion": 0, "tts": 0, "summary": 0, "opener": 0}


async def test_hanging_model_fails_over_fast(
    harness: Harness, fake_apis: HookedAPIs, settings, no_sleep
) -> None:
    """Модель не отвечает: не ждём 3 попытки по минуте на каждую модель — сразу запасная."""
    h = harness
    await onboard(h)

    def hook(request: httpx.Request) -> None:
        if is_companion(request):
            raise httpx.ReadTimeout("no answer", request=request)

    fake_apis.hook = hook
    fake_apis.paths.clear()
    await h.feed(text="Hello there")
    assert h.sent_texts() == [texts.LLM_FAILED]
    attempts = fake_apis.paths.count("/api/v1/chat/completions")
    assert attempts == 2  # по одной на основную и запасную модель


async def test_malformed_llm_body_falls_back_to_second_model(
    harness: Harness, fake_apis: HookedAPIs, settings, no_sleep
) -> None:
    h = harness
    await onboard(h)

    def hook(request: httpx.Request) -> httpx.Response | None:
        primary = json.loads(request.content).get("model") == settings.companion_model
        if is_companion(request) and primary:
            return httpx.Response(200, json={"choices": [{"message": None}]})
        return None

    fake_apis.hook = hook
    await h.feed(text="Hello there")
    assert h.sent_texts() == [fake_apis.companion_reply]


async def test_stt_non_json_body_asks_to_repeat(
    harness: Harness, fake_apis: HookedAPIs, no_sleep
) -> None:
    h = harness
    await onboard(h)
    fake_apis.hook = lambda r: (
        httpx.Response(200, text="<html>502 Bad Gateway</html>")
        if r.url.path == "/v1/speech-to-text"
        else None
    )
    await h.feed(voice=VOICE)
    assert h.sent_texts() == [texts.STT_FAILED]


async def test_send_voice_failure_falls_back_to_text(
    harness: Harness, fake_apis: HookedAPIs
) -> None:
    """Telegram не принял голосовое — ответ всё равно доходит текстом, а не теряется."""
    h = harness
    await onboard(h)
    original = h.tg.make_request

    async def make_request(bot, method, timeout=None) -> Any:
        if isinstance(method, SendVoice):
            raise TelegramNetworkError(method=method, message="Request timeout error")
        return await original(bot, method, timeout)

    h.tg.make_request = make_request  # type: ignore[method-assign]
    await h.feed(voice=VOICE)
    sent = h.sent_texts()
    assert texts.ERROR not in sent
    assert any(fake_apis.companion_reply in (t or "") for t in sent)


async def test_error_in_button_still_answers_callback(
    harness: Harness, fake_apis: HookedAPIs
) -> None:
    """Упало внутри кнопки — крутилка на кнопке всё равно гаснет."""
    h = harness
    await onboard(h)
    await h.feed(text="Hi there")
    async with h.session_factory() as session:
        reply_id = await session.scalar(select(func.max(Message.id)))
    original = h.tg.make_request

    async def make_request(bot, method, timeout=None) -> Any:
        if isinstance(method, SendVoice):
            raise TelegramNetworkError(method=method, message="Request timeout error")
        return await original(bot, method, timeout)

    h.tg.make_request = make_request  # type: ignore[method-assign]
    await h.click(TtsCb(message_id=reply_id).pack())
    assert h.tg.of_type(AnswerCallbackQuery)


async def test_unparseable_analysis_is_still_billed(
    harness: Harness, fake_apis: HookedAPIs
) -> None:
    h = harness
    await onboard(h)

    def hook(request: httpx.Request) -> httpx.Response | None:
        if request.url.host == "openrouter.ai" and system_of(request).startswith(
            "You analyse ONE message"
        ):
            return httpx.Response(200, json=openrouter_response('{"mistakes": [{"orig'))
        return None

    fake_apis.hook = hook
    await h.feed(text="I go to cinema yesterday")
    assert await h.count(Usage, Usage.kind == "llm_analyst") == 1


@pytest.mark.parametrize(
    "raw",
    [
        '{"a": 1}\n\nNote: I kept {placeholders} as is.',
        '```json\n{"a": 1}\n```\nTell me if you need {more}.',
        'Format {as requested}:\n{"a": 1}',
    ],
)
def test_json_survives_braces_in_prose(raw: str) -> None:
    assert extract_json_object(raw) == {"a": 1}


def test_json_still_rejects_garbage() -> None:
    for raw in ("no json here", "[1, 2]", '{"a": '):
        with pytest.raises(ValueError):
            extract_json_object(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "**[photo: a dog on a beach]**\nWhat a happy dog!",
        "What a happy dog! [photo: a dog on a beach]",
        "[photo: a dog on a beach]\n[photo: a dog on a beach]\nWhat a happy dog!",
    ],
)
def test_photo_tag_never_reaches_the_user(raw: str) -> None:
    description, reply = split_photo_tag(raw)
    assert description == "a dog on a beach"
    assert reply == "What a happy dog!"
    assert "photo:" not in prepare_tts_text(reply, 400)


def test_lesson_exercise_with_list_mistake_id() -> None:
    item = {"type": "fix", "mistake_id": [1, 2], "options": ["a b", "c d", "e f"], "answer": 0}
    exercise = clean_exercise(item, {1, 2}, random.Random(0))
    assert exercise is not None and exercise["mistake_id"] is None


def lesson_with_list_ids(request: httpx.Request) -> httpx.Response | None:
    if request.url.host != "openrouter.ai":
        return None
    body = json.loads(request.content)
    if not is_lesson_request(body):
        return None
    from tests.conftest import fake_lesson

    lesson = fake_lesson(json.loads(body["messages"][1]["content"]))
    for exercise in lesson["exercises"]:
        if exercise["type"] == "fix":
            exercise["mistake_id"] = [exercise["mistake_id"]]
    lesson["grammar"][0]["mistake_ids"] = "1, 2"
    return httpx.Response(200, json=openrouter_response(json.dumps(lesson), body["model"]))


async def test_odd_lesson_json_is_not_a_500(web: Web, session_factory, fake_apis) -> None:
    await seed_user(session_factory)
    fake_apis.hook = lesson_with_list_ids
    response = await web.post("/api/lessons")
    assert response.status_code == 200
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).where(Usage.kind == "llm_lesson")) == 1


async def test_empty_tts_audio_is_not_cached(web: Web, session_factory, fake_apis) -> None:
    await seed_user(session_factory)
    lesson = (await web.post("/api/lessons")).json()
    index = next(e["index"] for e in lesson["exercises"] if e["type"] == "listen")
    fake_apis.hook = lambda r: httpx.Response(200, content=b"") if is_tts(r) else None
    first = await web.get(f"/api/lessons/{lesson['id']}/audio/{index}")
    assert first.status_code == 503
    fake_apis.hook = None
    second = await web.get(f"/api/lessons/{lesson['id']}/audio/{index}")
    assert second.status_code == 200 and second.content
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AudioCache)) == 1


async def test_one_tts_429_does_not_burn_weekly_podcast(
    web: Web, session_factory, fake_apis, no_sleep
) -> None:
    """ElevenLabs ответил 429 на одну реплику из шести — выпуск всё равно получается."""
    await seed_user(session_factory)
    calls = {"tts": 0}

    def hook(request: httpx.Request) -> httpx.Response | None:
        if is_tts(request):
            calls["tts"] += 1
            if calls["tts"] == 2:
                return httpx.Response(429, json={"detail": {"status": "too_many_requests"}})
        return None

    fake_apis.hook = hook
    response = await web.post("/api/podcasts")
    assert response.status_code == 200
