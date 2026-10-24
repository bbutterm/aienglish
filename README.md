# English Buddy Bot

Telegram-бот для практики английского через живое общение: друг-нейтив болтает текстом и голосом,
реагирует на фото, а в фоне копит ошибки и новые слова. Дизайн — в [DESIGN.md](DESIGN.md).
Сейчас реализована **фаза 1 (Voice MVP)**.

## Что умеет

- Диалог текстом и голосом: голосовое → ElevenLabs Scribe → собеседник (LLM через OpenRouter) → ответ.
- Кнопка «🔊 Озвучить» под ответом (ElevenLabs TTS, кэш `file_id`) и режим «всегда голосом».
- Фото → собеседник реагирует как друг и задаёт вопрос; в истории фото хранится только описанием.
- Режимы исправлений `/mode`: просто болтаем / ✏️ с исправлениями / 📝 разбор потом (каждые N реплик и по `/review`).
- Аналитик в фоне пишет ошибки (`mistakes`), слова (`vocab`) и оценку уровня; `/mistakes` показывает последние.
- Память: последние 12 реплик + саммари старой части (сворачивается дешёвой моделью после 30 реплик).
- Онбординг: имя, уровень A1–C1, собеседник (🇺🇸 Jake / 🇬🇧 Emma — разные голоса).
- Дневные лимиты реплик и озвучек, учёт расходов в `usage`, `/stats` для админов.

Команды: `/start`, `/mode`, `/voice`, `/mistakes`, `/review`, `/reset`, `/help`, `/stats` (админ).

## Запуск (Docker Compose)

```bash
cp .env.example .env   # заполнить TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, ELEVENLABS_API_KEY, ADMIN_IDS
docker compose up -d --build
docker compose logs -f bot
```

Сервисы: `bot` (при старте сам применяет миграции `alembic upgrade head`, long polling), `db` (Postgres 16
с volume) и `backup` (ежедневный `pg_dump` в `./backups`, хранит 14 дней).

## Локальная разработка

Нужны Python 3.12, [uv](https://docs.astral.sh/uv/), Postgres 16 и ffmpeg.

```bash
uv sync
export DATABASE_URL=postgresql+asyncpg://bot:bot@localhost:5432/bot
uv run alembic upgrade head
uv run python -m app.main          # остальные переменные — из .env

# тесты: юнит-тесты работают без БД, интеграционные и сквозные — с отдельной тестовой базой
export TEST_DATABASE_URL=postgresql+asyncpg://bot:bot@localhost:5432/bot_test
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Сквозные тесты (`tests/test_bot_flow.py`) гоняют настоящие апдейты Telegram через диспетчер aiogram
на реальном Postgres; Bot API, OpenRouter и ElevenLabs подменены фейками.

## Устройство

```
app/
├─ main.py               сборка сервисов и диспетчера, long polling
├─ config.py             настройки из .env (pydantic-settings)
├─ personas.py           собеседники: имя, биография, голос
├─ bot/
│  ├─ handlers/          start (онбординг), text, voice, photo, callbacks, commands, fallback
│  ├─ middlewares.py     сессия БД, upsert пользователя, онбординг + дневной лимит
│  ├─ keyboards.py       inline-кнопки и callback data
│  └─ texts.py           служебные тексты интерфейса (рус.)
├─ services/
│  ├─ dialog.py          MessageService: оркестрация текста, голоса, фото, озвучки
│  ├─ companion.py       собеседник: промпт, история, фото
│  ├─ analyst.py         разбор реплики в JSON → mistakes / vocab
│  ├─ memory.py          окно истории, саммари, /reset
│  ├─ stt.py / tts.py    ElevenLabs Scribe / TTS (+ ffmpeg, если пришёл не Ogg)
│  ├─ review.py          форматирование ошибок и разбора
│  └─ usage.py           учёт расходов, /stats
├─ llm/openrouter.py     клиент OpenRouter: ретраи, fallback-модель, стоимость из usage
├─ prompts/              jinja-шаблоны промптов
└─ db/                   модели SQLAlchemy 2 (async) и запросы
alembic/                 миграции
```

На каждую реплику: сохранить сообщение → контекст (саммари + 12 реплик) → собеседник → ответ с
кнопкой 🔊 → в фоне аналитик и, при необходимости, сворачивание истории в саммари.

## Проверено при старте (24.10.2026)

- **OpenRouter:** собеседник по умолчанию `google/gemini-3.8-flash` (vision), fallback
  `qwen/qwen3-vl-235b-a22b-instruct`; аналитик и саммари — `google/gemini-3.5-flash-lite`
  (дешевле — `deepseek/deepseek-v4-flash`). У Gemini 3.x reasoning включён по умолчанию, поэтому
  передаём `reasoning.effort=minimal` ради задержки. Стоимость запроса OpenRouter всегда отдаёт в
  `usage.cost` — она пишется в таблицу `usage`.
- **ElevenLabs:** STT — `scribe_v2` (`scribe_v1` устарел), TTS — `eleven_flash_v2_5` (~75 мс).
  Есть вывод `opus_48000_*`: если ответ уже Ogg (`OggS`), ffmpeg не вызывается, иначе
  конвертируем. Цены для учёта: Scribe v2 — $0.22/час, Flash — $0.04 за 1K символов.
- **Голоса по умолчанию** — стандартные (premade), есть в любом аккаунте: Will (US) для Джейка и
  Alice (UK) для Эммы. Меняются в `.env`.

Все ID моделей и голосов — в конфиге, не в коде.

## Отличия от черновика схемы (§8)

- `users`: добавлены `display_name` (имя из онбординга), `estimated_level` (оценка аналитика),
  `onboarded`, `last_review_at` (для режима review) и `context_start_id` — граница «живой» истории:
  всё, что не новее, уже свёрнуто в саммари или отброшено через `/reset`. Сообщения при этом не
  удаляются (на них ссылаются ошибки и кэш озвучки).
- `mistakes.type` дополнительно допускает `grammar`; `usage.kind` — ещё и `llm_summary`.
- Таблица `lessons` (фаза 3) пока не создана.
- Описание фото собеседник отдаёт служебной первой строкой `[photo: ...]`, которую бот вырезает.
- FSM онбординга хранится в памяти: после рестарта посреди онбординга бот просто начнёт его заново.

## Дальше

Фаза 2 по дизайну: «долги» на повторение в промпте собеседника (в шаблоне уже есть блок
`review_items`), инициатива бота по `ping_time`, стрики, недельный отчёт. Открытые вопросы из §14
(какая модель «живее», транскрипт всегда или по кнопке — сейчас `SHOW_TRANSCRIPT=true`) лучше решать
после пары недель теста на себе.
