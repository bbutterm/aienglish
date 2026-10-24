from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app.config import Settings
from app.db import repo
from app.db.models import Mistake, Usage, User, Vocab
from app.llm.openrouter import OpenRouterClient
from app.services.memory import Memory
from app.services.usage import collect_stats, format_stats, record_usage


def test_day_start_utc_respects_timezone() -> None:
    now = datetime(2026, 10, 24, 22, 30, tzinfo=UTC)  # 01:30 следующего дня в Москве
    assert repo.day_start_utc("Europe/Moscow", now) == datetime(2026, 10, 24, 21, 0, tzinfo=UTC)
    assert repo.day_start_utc("Not/AZone", now) == datetime(2026, 10, 24, 0, 0, tzinfo=UTC)


async def test_upsert_user_inserts_then_updates(session) -> None:
    user = await repo.upsert_user(session, 7, "alex", "Alex")
    assert (user.id, user.mode, user.level, user.onboarded) == (7, "chat", "B1", False)
    user.level = "A2"
    await session.commit()
    again = await repo.upsert_user(session, 7, "alex_new", "Alexander")
    assert again is user
    assert (again.username, again.first_name, again.level) == ("alex_new", "Alexander", "A2")


async def test_history_window_and_reset(session, settings: Settings, session_factory) -> None:
    user = await repo.upsert_user(session, 1, None, "Alex")
    for i in range(5):
        await repo.add_message(session, user_id=user.id, role="user", kind="text", text=f"u{i}")
        await repo.add_message(
            session, user_id=user.id, role="assistant", kind="text", text=f"a{i}"
        )
    await session.commit()

    history = await repo.get_history(session, user, 3)
    assert [m.text for m in history] == ["a3", "u4", "a4"]

    memory = Memory(llm=None, settings=settings, session_factory=session_factory)  # type: ignore[arg-type]
    user.conversation_summary = "old"
    await memory.reset(session, user)
    assert user.conversation_summary is None
    assert await repo.get_history(session, user, 3) == []
    await repo.add_message(session, user_id=user.id, role="user", kind="text", text="fresh")
    assert [m.text for m in await repo.get_history(session, user, 3)] == ["fresh"]


async def test_save_mistakes_dedupes_and_vocab_ignores_duplicates(session) -> None:
    user = await repo.upsert_user(session, 1, None, "Alex")
    msg = await repo.add_message(session, user_id=user.id, role="user", kind="text", text="x")
    item = repo.MistakeItem("I go there", "I went there", "tense", "Прошедшее", 2)
    await repo.save_mistakes(session, user.id, msg.id, [item])
    await repo.save_mistakes(
        session, user.id, msg.id, [repo.MistakeItem("i go THERE", "I went there", "tense", None, 1)]
    )
    vocab = [repo.VocabItem("cosy", "уютный", "bot")]
    await repo.save_vocab(session, user.id, vocab)
    await repo.save_vocab(
        session, user.id, vocab + [repo.VocabItem("knackered", "вымотанный", "bot")]
    )
    await session.commit()

    mistakes = await repo.recent_mistakes(session, user.id, 10)
    assert len(mistakes) == 1 and mistakes[0].times_seen == 2
    assert await session.scalar(select(func.count(Vocab.id))) == 2


async def test_counters_for_limits(session) -> None:
    user = await repo.upsert_user(session, 1, None, "Alex")
    for _ in range(3):
        await repo.add_message(session, user_id=user.id, role="user", kind="text", text="hi")
    await repo.add_message(session, user_id=user.id, role="assistant", kind="text", text="yo")
    await record_usage(session, user_id=user.id, kind="tts", chars=10, cost_usd=0.0004)
    await session.commit()
    since = repo.day_start_utc(user.timezone)
    assert await repo.count_user_messages_since(session, user.id, since) == 3
    assert await repo.count_usage_since(session, user.id, "tts", since) == 1
    future = datetime.now(UTC) + timedelta(hours=1)
    assert await repo.count_user_messages_since(session, user.id, future) == 0


async def test_maybe_summarize_folds_old_messages(
    session, settings: Settings, session_factory
) -> None:
    settings.summary_trigger = 6
    settings.history_window = 4
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "summary-model",
                "choices": [{"message": {"content": "Alex loves pizza."}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10, "cost": 0.00001},
            },
        )

    user = await repo.upsert_user(session, 1, None, "Alex")
    ids = []
    for i in range(4):
        ids.append(
            (await repo.add_message(session, user_id=1, role="user", kind="text", text=f"u{i}")).id
        )
        ids.append(
            (
                await repo.add_message(
                    session, user_id=1, role="assistant", kind="text", text=f"a{i}"
                )
            ).id
        )
    await session.commit()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        memory = Memory(OpenRouterClient(http, api_key="k", backoff=0), settings, session_factory)
        await memory.maybe_summarize(1)
        assert len(requests) == 1
        await memory.maybe_summarize(1)  # уже свернули — второй вызов ничего не делает
        assert len(requests) == 1

    await session.refresh(user)
    assert user.conversation_summary == "Alex loves pizza."
    assert user.context_start_id == ids[3]
    assert [m.text for m in await repo.get_live_messages(session, user)] == ["u2", "a2", "u3", "a3"]
    assert (
        await session.scalar(select(func.count(Usage.id)).where(Usage.kind == "llm_summary")) == 1
    )


async def test_stats(session) -> None:
    user = await repo.upsert_user(session, 1, None, "Alex")
    user.onboarded = True
    await repo.add_message(session, user_id=1, role="user", kind="text", text="hi")
    await record_usage(session, user_id=1, kind="llm_companion", cost_usd=0.0015)
    await record_usage(session, user_id=1, kind="tts", cost_usd=0.002)
    await session.commit()
    stats = await collect_stats(session)
    assert (stats.users_total, stats.users_onboarded, stats.users_active_day) == (1, 1, 1)
    assert stats.day.user_messages == 1
    assert str(stats.day.total_cost) == "0.003500"
    text = format_stats(stats)
    assert "llm_companion: 1 шт · $0.0015" in text
    assert isinstance(await session.get(User, 1), User)
    assert await session.scalar(select(func.count(Mistake.id))) == 0
