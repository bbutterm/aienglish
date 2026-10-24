"""API Mini App поверх реального Postgres; Telegram, OpenRouter и ElevenLabs — фейки."""

import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from sqlalchemy import func, select

from app.db import repo
from app.db.models import AudioCache, Lesson, Mistake, Usage, User, Vocab
from app.web.app import build_state, create_app
from tests.conftest import FakeTelegramSession, sign_init_data

USER_ID = 555
ADMIN_ID = 1


class Web:
    def __init__(self, client: httpx.AsyncClient, state, tg: FakeTelegramSession, token: str):
        self.client = client
        self.state = state
        self.tg = tg
        self.token = token

    def headers(self, user_id: int = USER_ID, **kwargs) -> dict[str, str]:
        return {"Authorization": f"tma {sign_init_data(self.token, user_id, **kwargs)}"}

    async def get(self, url: str, user_id: int = USER_ID, **kwargs) -> httpx.Response:
        return await self.client.get(url, headers=self.headers(user_id), **kwargs)

    async def post(self, url: str, user_id: int = USER_ID, **kwargs) -> httpx.Response:
        return await self.client.post(url, headers=self.headers(user_id), **kwargs)


@pytest.fixture
async def web(settings, session_factory, http) -> AsyncGenerator[Web]:
    tg = FakeTelegramSession()
    bot = Bot(settings.telegram_bot_token, session=tg)
    state = build_state(settings, session_factory, http, bot)
    app = create_app(state)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Web(client, state, tg, settings.telegram_bot_token)
    await state.tasks.drain()


async def seed_user(session_factory, user_id: int = USER_ID, **fields) -> None:
    async with session_factory() as session:
        user = await repo.upsert_user(session, user_id, "alex", "Alex")
        user.onboarded = True
        user.display_name = "Sasha"
        for key, value in fields.items():
            setattr(user, key, value)
        msg = await repo.add_message(session, user_id=user_id, role="user", kind="text", text="hi")
        await repo.save_mistakes(
            session,
            user_id,
            msg.id,
            [
                repo.MistakeItem(
                    "I go to cinema yesterday",
                    "I went to the cinema yesterday",
                    "tense",
                    "go → went",
                    2,
                )
            ],
        )
        await repo.save_vocab(
            session, user_id, [repo.VocabItem("blockbuster", "блокбастер", "bot")]
        )
        await session.commit()


async def stored_lesson(session_factory, lesson_id: int) -> Lesson:
    async with session_factory() as session:
        return await session.get(Lesson, lesson_id)


# --- авторизация ---


async def test_auth_required_and_validated(web: Web) -> None:
    assert (await web.client.get("/api/me")).status_code == 401
    bad = {"Authorization": "tma " + sign_init_data("999:OTHER", USER_ID)}
    assert (await web.client.get("/api/me", headers=bad)).status_code == 401
    old = int(time.time()) - 25 * 3600
    expired = {"Authorization": "tma " + sign_init_data(web.token, USER_ID, auth_date=old)}
    response = await web.client.get("/api/me", headers=expired)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "unauthorized"

    response = await web.get("/api/me")
    assert response.status_code == 200
    me = response.json()
    assert (me["id"], me["onboarded"], me["is_admin"], me["premium"]) == (
        USER_ID,
        False,
        False,
        False,
    )
    assert me["limits"] == {"messages": 50, "tts": 20, "lessons": 1}


async def test_lesson_requires_onboarding(web: Web) -> None:
    response = await web.post("/api/lessons")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "not_onboarded"


async def test_static_index(web: Web) -> None:
    response = await web.client.get("/")
    assert response.status_code == 200
    assert "__VERSION__" not in response.text
    script = await web.client.get("/static/js/app.js")
    assert script.status_code == 200 and script.headers["cache-control"] == "no-cache"


# --- урок ---


async def test_full_lesson_flow(web: Web, session_factory, fake_apis) -> None:
    await seed_user(session_factory)
    today = (await web.get("/api/lessons/today")).json()
    assert (today["lessons"], today["remaining"], today["limit"]) == ([], 1, 1)
    assert today["material"] == {"mistakes": 1, "words": 1}

    response = await web.post("/api/lessons")
    assert response.status_code == 200, response.text
    lesson = response.json()
    assert lesson["title"] == "Что было вчера"
    assert [e["type"] for e in lesson["exercises"]] == ["fix", "gap", "translate", "listen"]
    assert all("answer" not in e and "result" not in e for e in lesson["exercises"])
    assert "text" not in lesson["exercises"][3]
    assert lesson["cards"][0]["term"] == "blockbuster"
    material = fake_apis.lesson_requests()[0]["messages"][1]["content"]
    assert "I go to cinema yesterday" in material and "blockbuster" in material

    stored = await stored_lesson(session_factory, lesson["id"])
    fix = stored.payload["exercises"][0]
    url = f"/api/lessons/{lesson['id']}/answers"
    result = (await web.post(url, json={"index": 0, "choice": fix["answer"]})).json()
    assert result["correct"] is True and result["explanation_ru"] == "Прошедшее время"
    async with session_factory() as session:
        mistake = await session.scalar(select(Mistake))
    assert mistake.resolved is True
    assert mistake.next_review_at > datetime.now(UTC) + timedelta(days=6)

    # повторный ответ не переигрывает первый
    wrong_choice = (fix["answer"] + 1) % len(fix["options"])
    again = (await web.post(url, json={"index": 0, "choice": wrong_choice})).json()
    assert again["correct"] is True and again["choice"] == fix["answer"]

    gap = stored.payload["exercises"][1]
    wrong = (gap["answer"] + 1) % len(gap["options"])
    assert (await web.post(url, json={"index": 1, "choice": wrong})).json()["correct"] is False
    assert (await web.post(url, json={"index": 9, "choice": 0})).status_code == 422
    assert (await web.post(url, json={"index": 2, "choice": 7})).status_code == 422

    listen_answer = stored.payload["exercises"][3]["answer"]
    listen = (await web.post(url, json={"index": 3, "choice": listen_answer})).json()
    assert listen["text"] == "We went to the cinema last night"

    audio_url = f"/api/lessons/{lesson['id']}/audio/3"
    audio = await web.get(audio_url)
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/mpeg"
    assert (await web.get(audio_url)).content == audio.content
    assert len(fake_apis.tts_requests) == 1  # второй раз — из кэша
    assert fake_apis.tts_requests[0]["params"]["output_format"] == "mp3_44100_64"
    assert (await web.get(f"/api/lessons/{lesson['id']}/audio/0")).status_code == 404

    # чужой урок не виден
    assert (await web.post(url, user_id=777, json={"index": 2, "choice": 0})).status_code == 404

    done = (await web.post(f"/api/lessons/{lesson['id']}/complete")).json()
    assert (done["status"], done["score"], done["total"]) == ("done", 2, 4)
    await web.state.tasks.drain()
    # собеседник сам пишет в чат на тему урока
    followup = web.tg.of_type(SendMessage)
    assert [m.text for m in followup] == [fake_apis.companion_reply]
    note = fake_apis.companion_requests()[-1]["messages"][-1]["content"]
    assert "Favourite films" in note

    response = await web.post("/api/lessons")
    assert response.status_code == 429
    today = (await web.get("/api/lessons/today")).json()
    assert today["remaining"] == 0 and today["lessons"][0]["status"] == "done"

    async with session_factory() as session:
        kinds = set(await session.scalars(select(Usage.kind)))
        assert await session.scalar(select(func.count()).select_from(AudioCache)) == 1
    assert {"llm_lesson", "tts_lesson", "llm_companion"} <= kinds


async def test_lesson_fallback_when_llm_returns_garbage(
    web: Web, session_factory, fake_apis
) -> None:
    await seed_user(session_factory)
    fake_apis.lesson_mode = "garbage"
    lesson = (await web.post("/api/lessons")).json()
    assert lesson["title"] == "Повторение"
    assert [e["type"] for e in lesson["exercises"]] == ["fix"]
    assert lesson["cards"][0]["term"] == "blockbuster"


async def test_lesson_generation_failure_keeps_daily_limit(
    web: Web, session_factory, fake_apis
) -> None:
    async with session_factory() as session:
        user = await repo.upsert_user(session, USER_ID, "alex", "Alex")
        user.onboarded = True
        await session.commit()
    fake_apis.lesson_mode = "error"
    response = await web.post("/api/lessons")
    assert response.status_code == 503
    assert (await web.get("/api/lessons/today")).json()["remaining"] == 1


# --- прогресс и слова ---


async def test_progress(web: Web, session_factory) -> None:
    await seed_user(session_factory)
    await web.post("/api/lessons")
    progress = (await web.get("/api/progress")).json()
    assert progress["level"] == {"self": "B1", "estimated": None}
    assert progress["activity"]["streak"] == 1
    assert progress["totals"]["messages"] == 1
    assert progress["totals"]["words"] == 1 and progress["totals"]["words_due"] == 1
    assert progress["mistake_rate"]["week"] == 10.0
    assert progress["mistakes_by_type"][0] == {
        "type": "tense",
        "label_ru": "времена",
        "count": 1,
        "prev": 0,
    }
    assert progress["insights"]["weaknesses"] == ["Путаешь времена"]


async def test_vocab_review(web: Web, session_factory) -> None:
    await seed_user(session_factory)
    vocab = (await web.get("/api/vocab")).json()
    assert vocab["due"] == 1
    card_id = vocab["items"][0]["id"]
    card = (await web.post(f"/api/vocab/{card_id}/review", json={"grade": "good"})).json()
    assert (card["status"], card["due"]) == ("learning", False)
    assert (await web.get("/api/vocab")).json()["due"] == 0
    bad = await web.post(f"/api/vocab/{card_id}/review", json={"grade": "meh"})
    assert bad.status_code == 422
    other = await web.post(f"/api/vocab/{card_id}/review", user_id=777, json={"grade": "good"})
    assert other.status_code == 404


# --- админка ---


async def test_admin_access_and_overview(web: Web, session_factory) -> None:
    await seed_user(session_factory)
    assert (await web.get("/api/admin/overview")).status_code == 403
    response = await web.get("/api/admin/overview", user_id=ADMIN_ID)
    assert response.status_code == 200
    overview = response.json()
    assert overview["users"]["total"] == 2  # ученик и сам админ
    assert overview["users"]["dau"] == 1
    assert len(overview["series"]) == 14
    assert overview["series"][-1]["messages"] == 1

    users = (await web.get("/api/admin/users?q=@ale", user_id=ADMIN_ID)).json()
    assert users["total"] == 2
    assert {u["id"] for u in users["items"]} == {USER_ID, ADMIN_ID}
    by_id = (await web.get(f"/api/admin/users?q={USER_ID}", user_id=ADMIN_ID)).json()
    assert [u["id"] for u in by_id["items"]] == [USER_ID]

    detail = (await web.get(f"/api/admin/users/{USER_ID}", user_id=ADMIN_ID)).json()
    assert detail["name"] == "Sasha"
    assert detail["mistakes"][0]["corrected"] == "I went to the cinema yesterday"
    assert detail["progress"]["totals"]["messages"] == 1
    assert (await web.get("/api/admin/users/424242", user_id=ADMIN_ID)).status_code == 404


async def test_admin_premium_and_block(web: Web, session_factory) -> None:
    await seed_user(session_factory)
    url = f"/api/admin/users/{USER_ID}"
    detail = (await web.post(f"{url}/premium", user_id=ADMIN_ID, json={"days": 30})).json()
    assert detail["premium"] is True
    assert detail["limits"] == {"messages": 200, "tts": 100, "lessons": 3}
    notice = web.tg.of_type(SendMessage)[-1]
    assert notice.chat_id == USER_ID and "премиум" in notice.text
    me = (await web.get("/api/me")).json()
    assert me["premium"] is True
    assert (await web.get("/api/lessons/today")).json()["remaining"] == 3

    first_until = datetime.fromisoformat(detail["premium_until"])
    extended = (await web.post(f"{url}/premium", user_id=ADMIN_ID, json={"days": 10})).json()
    assert datetime.fromisoformat(extended["premium_until"]) - first_until == timedelta(days=10)
    revoked = (await web.post(f"{url}/premium", user_id=ADMIN_ID, json={"days": 0})).json()
    assert revoked["premium"] is False and revoked["premium_until"] is None

    blocked = (await web.post(f"{url}/block", user_id=ADMIN_ID, json={"blocked": True})).json()
    assert blocked["blocked"] is True
    response = await web.get("/api/me")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "blocked"
    await web.post(f"{url}/block", user_id=ADMIN_ID, json={"blocked": False})
    assert (await web.get("/api/me")).status_code == 200

    self_block = await web.post(
        f"/api/admin/users/{ADMIN_ID}/block", user_id=ADMIN_ID, json={"blocked": True}
    )
    assert self_block.status_code == 422
    async with session_factory() as session:
        assert (await session.get(User, ADMIN_ID)).blocked is False
        assert await session.scalar(select(func.count(Vocab.id))) == 1
