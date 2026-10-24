"""Включаемые фичи. Список — здесь, включённость — в таблице feature_flags (меняется в админке).

Бот и Mini App — разные процессы, поэтому состояние читается из БД и кэшируется на несколько секунд.
"""

import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import FeatureFlag

CACHE_SECONDS = 10.0


@dataclass(frozen=True)
class Feature:
    key: str
    title: str
    description: str
    where: str  # где видно пользователю: бот, приложение
    default: bool = True


FEATURES: dict[str, Feature] = {
    f.key: f
    for f in (
        Feature(
            "word_upgrades",
            "Прокачка слов",
            "Аналитик замечает простые слова (very good, big, nice) и делает карточки с более "
            "сильными вариантами — на твоей же фразе.",
            "бот + приложение",
        ),
        Feature(
            "topic_ideas",
            "Темы для тебя",
            "Личные темы для разговора по интересам и слабым местам. Заголовки видны сразу, "
            "фразы и вопросы генерируются по нажатию; «Обсудить» — и друг пишет в чат.",
            "приложение",
        ),
        Feature(
            "missions",
            "Миссии",
            "Ролевые сценарии с целями из жизни ученика: собеседник играет роль, бот отмечает "
            "выполненные цели и подводит итог.",
            "приложение + бот",
        ),
        Feature(
            "native_rewrite",
            "Скажи как носитель",
            "Кнопка под ответом друга: твоя последняя фраза так, как сказал бы носитель, и что "
            "именно поменялось.",
            "бот",
        ),
        Feature(
            "podcast",
            "Подкаст про тебя",
            "Выпуск на 2 минуты: Jake и Emma двумя голосами обсуждают твою неделю — темы, новые "
            "слова и ошибки. Слушать в приложении или прислать в чат.",
            "приложение",
        ),
        Feature(
            "friend_ping",
            "Друг пишет первым",
            "Пропал на день — собеседник сам пишет вечером, продолжая ваш последний разговор. "
            "Не чаще раза в день, после трёх сообщений без ответа замолкает.",
            "бот",
        ),
        Feature(
            "photo_words",
            "Слова с фото",
            "К фото, кроме реакции друга, приходят 5 слов с него — сразу в карточки.",
            "бот",
        ),
        Feature(
            "forward_words",
            "Разбор пересланного",
            "Перешли боту английский пост или сообщение — он перескажет суть и вытащит слова "
            "в карточки.",
            "бот",
        ),
        Feature(
            "referrals",
            "Пригласи друга",
            "Личная ссылка-приглашение: обоим +7 дней премиума, когда друг сделает 5 реплик.",
            "приложение + бот",
        ),
        Feature(
            "first_steps",
            "Подсказки первых дней",
            "Постепенное знакомство: после 3-й реплики — «вот что я заметил», после 8-й — "
            "приглашение в урок, потом — проверка уровня и настройки. Без анкет на входе.",
            "бот",
        ),
    )
}


class FeatureFlags:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], ttl: float = CACHE_SECONDS
    ) -> None:
        self._session_factory = session_factory
        self._ttl = ttl
        self._cache: dict[str, bool] | None = None
        self._loaded_at = 0.0

    def invalidate(self) -> None:
        self._cache = None

    async def snapshot(self) -> dict[str, bool]:
        if self._cache is None or time.monotonic() - self._loaded_at > self._ttl:
            async with self._session_factory() as session:
                rows = await session.execute(select(FeatureFlag.key, FeatureFlag.enabled))
                stored = dict(rows.all())
            self._cache = {key: stored.get(key, f.default) for key, f in FEATURES.items()}
            self._loaded_at = time.monotonic()
        return dict(self._cache)

    async def enabled(self, key: str) -> bool:
        if key not in FEATURES:
            raise KeyError(key)
        return (await self.snapshot())[key]

    async def set(self, key: str, enabled: bool) -> None:
        if key not in FEATURES:
            raise KeyError(key)
        async with self._session_factory() as session:
            stmt = pg_insert(FeatureFlag).values(key=key, enabled=enabled)
            stmt = stmt.on_conflict_do_update(
                index_elements=[FeatureFlag.key], set_={"enabled": enabled}
            )
            await session.execute(stmt)
            await session.commit()
        self.invalidate()
