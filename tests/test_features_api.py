"""API включаемых фич: темы и миссии, подкаст, приглашения, переключатели и источники в админке."""

from aiogram.methods import SendAudio, SendMessage
from sqlalchemy import select

from app.db import repo
from app.db.models import TopicIdea, User, Vocab
from app.features import FEATURES
from tests.test_web_api import ADMIN_ID, USER_ID, Web, seed_user, web


async def test_ideas_titles_then_content_on_demand(web: Web, session_factory, fake_apis) -> None:
    await seed_user(session_factory)
    data = (await web.get("/api/ideas")).json()
    # Заголовки готовы сразу (пустой отброшен), содержимого ещё нет.
    assert [(i["kind"], i["title_ru"]) for i in data["ideas"]] == [
        ("talk", "Самый странный баг в твоей карьере"),
        ("talk", "MacBook или Windows: спорим"),
        ("mission", "Верни ноутбук с битым пикселем"),
    ]
    assert all(i["content"] is None for i in data["ideas"])
    assert data["refreshes_left"] == 1 and data["missions"] is True
    profile = [
        r for r in fake_apis.llm_requests if "conversation topics" in r["messages"][0]["content"]
    ][0]["messages"][1]["content"]
    assert "I go to cinema yesterday → I went to the cinema yesterday" in profile

    # Повторный заход — без новой генерации.
    again = (await web.get("/api/ideas")).json()
    assert [i["id"] for i in again["ideas"]] == [i["id"] for i in data["ideas"]]

    talk = data["ideas"][0]
    opened = (await web.post(f"/api/ideas/{talk['id']}/open")).json()
    assert opened["content"]["questions"] == ["What happened?", "How did you fix it?"]
    assert opened["status"] == "opened"

    started = await web.post(f"/api/ideas/{talk['id']}/start")
    assert started.json() == {"ok": True, "kind": "talk"}
    await web.state.tasks.drain()
    note = fake_apis.companion_requests()[-1]["messages"][-1]["content"]
    assert "picked a topic" in note and "The weirdest bug of your career" in note
    assert [m.chat_id for m in web.tg.of_type(SendMessage)] == [USER_ID]

    # Лимит обновлений: 2 в день (одно ушло на первую порцию).
    assert (await web.post("/api/ideas/refresh")).status_code == 200
    response = await web.post("/api/ideas/refresh")
    assert response.status_code == 429


async def test_mission_start_from_app(web: Web, session_factory, fake_apis) -> None:
    await seed_user(session_factory)
    ideas = (await web.get("/api/ideas")).json()["ideas"]
    mission = next(i for i in ideas if i["kind"] == "mission")
    opened = (await web.post(f"/api/ideas/{mission['id']}/open")).json()
    assert [g["ru"] for g in opened["content"]["goals"]] == [
        "Объясни, что не так",
        "Попроси вернуть деньги",
        "Вежливо откажись от бонусов",
    ]
    assert (await web.post(f"/api/ideas/{mission['id']}/start")).json()["kind"] == "mission"
    await web.state.tasks.drain()
    intro, opener = web.tg.of_type(SendMessage)
    assert intro.text.startswith("🎭 <b>Миссия: Верни ноутбук с битым пикселем</b>")
    async with session_factory() as session:
        user = await session.get(User, USER_ID)
    assert user.active_mission["role_en"] == "a shop assistant at an electronics store"
    # Пока идёт сцена, вторую не начать.
    response = await web.post(f"/api/ideas/{mission['id']}/start")
    assert response.status_code == 409


async def test_missions_off_hides_missions(web: Web, session_factory) -> None:
    await seed_user(session_factory)
    await web.state.features.set("missions", False)
    data = (await web.get("/api/ideas")).json()
    assert data["missions"] is False
    assert {i["kind"] for i in data["ideas"]} == {"talk"}


async def test_feature_off_returns_404(web: Web, session_factory) -> None:
    await seed_user(session_factory)
    for key, url in (("topic_ideas", "/api/ideas"), ("podcast", "/api/podcasts")):
        await web.state.features.set(key, False)
        response = await web.get(url)
        assert response.status_code == 404 and response.json()["detail"]["code"] == "feature_off"
    me = (await web.get("/api/me")).json()
    assert me["features"]["topic_ideas"] is False and me["features"]["missions"] is True


async def test_podcast(web: Web, session_factory, fake_apis) -> None:
    await seed_user(session_factory)
    assert (await web.get("/api/podcasts")).json() == {"items": [], "remaining": 1}
    response = await web.post("/api/podcasts")
    assert response.status_code == 200, response.text
    podcast = response.json()
    assert podcast["title"] == "Неделя кино и ноутбуков"
    assert [line["speaker"] for line in podcast["lines"]] == [
        "jake",
        "emma",
        "jake",
        "emma",
        "jake",
    ]
    assert podcast["has_audio"] is True
    # Два голоса: Jake и Emma озвучены своими голосами.
    voices = {r["path"].rsplit("/", 1)[-1] for r in fake_apis.tts_requests}
    assert voices == {web.state.settings.voice_us_casual, web.state.settings.voice_uk_casual}
    material = [
        r for r in fake_apis.llm_requests if "two-host podcast" in r["messages"][0]["content"]
    ][0]["messages"][1]["content"]
    assert "I go to cinema yesterday → I went to the cinema yesterday" in material

    audio = await web.get(f"/api/podcasts/{podcast['id']}/audio")
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/mpeg"
    assert len(audio.content) > 0

    assert (await web.post("/api/podcasts")).status_code == 429  # бесплатно — раз в неделю
    assert (await web.post(f"/api/podcasts/{podcast['id']}/send")).json() == {"ok": True}
    (sent,) = web.tg.of_type(SendAudio)
    assert sent.chat_id == USER_ID and sent.title == "Неделя кино и ноутбуков"
    # чужой выпуск не отдаём
    assert (await web.get(f"/api/podcasts/{podcast['id']}/audio", user_id=777)).status_code == 404


async def test_referral_link(web: Web, session_factory) -> None:
    await seed_user(session_factory)
    async with session_factory() as session:
        friend = await repo.upsert_user(session, 900, "f", "F")
        friend.referred_by = USER_ID
        friend.referral_rewarded = True
        await session.commit()
    data = (await web.get("/api/referral")).json()
    assert data["link"] == f"https://t.me/buddy_test_bot?start=ref_{USER_ID}"
    assert (data["invited"], data["rewarded"], data["days"]) == (1, 1, 7)


async def test_admin_features_and_sources(web: Web, session_factory) -> None:
    await seed_user(session_factory, source="tgads_nov")
    assert (await web.get("/api/admin/features")).status_code == 403
    features = (await web.get("/api/admin/features", user_id=ADMIN_ID)).json()["features"]
    assert [f["key"] for f in features] == list(FEATURES)
    # по умолчанию всё включено, кроме того, что стоит денег или меняет оплату
    assert {f["key"] for f in features if not f["enabled"]} == {"payments_stars", "trial_week"}

    response = await web.post(
        "/api/admin/features/podcast", user_id=ADMIN_ID, json={"enabled": False}
    )
    podcast = next(f for f in response.json()["features"] if f["key"] == "podcast")
    assert podcast["enabled"] is False
    assert (await web.get("/api/me")).json()["features"]["podcast"] is False
    assert (
        await web.post("/api/admin/features/nope", user_id=ADMIN_ID, json={"enabled": True})
    ).status_code == 404

    sources = (await web.get("/api/admin/sources", user_id=ADMIN_ID)).json()["sources"]
    by_source = {s["source"]: s for s in sources}
    assert by_source["tgads_nov"]["users"] == 1


async def test_vocab_shows_upgrade_fields(web: Web, session_factory) -> None:
    await seed_user(session_factory)
    async with session_factory() as session:
        await repo.save_vocab(
            session,
            USER_ID,
            [
                repo.UpgradeItem(
                    basic="very big", better="huge", translation_ru="огромный", example="It's huge."
                ).as_vocab()
            ],
        )
        await session.commit()
        assert await session.scalar(select(Vocab.note).where(Vocab.term == "huge")) == "very big"
    items = (await web.get("/api/vocab")).json()["items"]
    huge = next(i for i in items if i["term"] == "huge")
    assert (huge["source"], huge["note"], huge["example"]) == ("upgrade", "very big", "It's huge.")


async def test_ideas_need_onboarding(web: Web, session_factory) -> None:
    async with session_factory() as session:
        await repo.upsert_user(session, USER_ID, "a", "A")
    data = (await web.get("/api/ideas")).json()
    assert data["ideas"] == []
    async with session_factory() as session:
        assert await session.scalar(select(TopicIdea.id)) is None
