# English Buddy Bot — Design Doc (v0.1)

Telegram-бот для практики английского через живое общение. Бот — друг-нейтив: болтает текстом и голосом, реагирует на фото, тихо копит твои ошибки и слова, а потом сам возвращает их в разговор.

> Статус: черновик для старта разработки. Фаза 1 — голосовой MVP.

---

## 1. Принципы продукта

1. **Это общение, а не урок.** Никаких «Lesson 1». Бот реагирует, шутит, задаёт встречный вопрос. Обучение спрятано внутри.
2. **Два потока на каждую реплику:**
   - **Собеседник (Companion)** — отвечает живо, в диалог не влезает с разбором (если режим не просит);
   - **Аналитик (Analyst)** — в фоне разбирает реплику пользователя, пишет ошибки/слова в БД. Пользователь его не видит.
3. **Фото = повод поговорить.** Прислал фото → бот реагирует как друг и задаёт один вопрос.
4. **Голос — первоклассный.** Пользователь может говорить голосом; бот озвучивает ответ по кнопке (или всегда — в голосовом режиме).
5. **Всё по user_id в одной БД** — заготовка под память, инициативу бота и будущие уроки в Mini App.

---

## 2. Скоуп

### Фаза 1 — Voice MVP (делаем сейчас)
- Диалог текстом и голосом (STT → LLM → текст + опционально TTS).
- Фото → бот начинает разговор по теме фото.
- Режимы исправлений: `chat` / `correct` / `review` (см. §5).
- Кнопка «🔊 Озвучить» под ответом; режим «всегда голосом».
- Аналитик в фоне пишет ошибки и слова в БД.
- Онбординг: имя, примерный уровень (самооценка A1–C1), выбор голоса-персонажа.
- Дневной лимит реплик (без оплаты пока — просто лимит, чтобы контролировать расход).
- Команды: `/start`, `/mode`, `/voice`, `/mistakes` (последние ошибки), `/reset` (новая тема).

### Фаза 2 — Память и инициатива
- Собеседник получает из БД 2–3 «долга» (ошибки/слова на повторение) и ненавязчиво вворачивает их.
- Бот пишет первым в выбранное пользователем время (APScheduler).
- Стрики.
- Недельный отчёт: топ-3 ошибки, новые слова, динамика.

### Фаза 3 — Mini App с уроками
- Ночной джоб генерирует урок из ошибок/слов пользователя (JSON).
- Mini App (FastAPI + фронт) рендерит: исправь свою фразу, карточки с интервальным повтором, пропуски, аудирование голосом ElevenLabs.
- После урока бот заводит разговор на ту же тему.

### Фаза 4 — Монетизация
- Telegram Stars, подписка, free-лимит.

### Не входит в MVP
- Живые голосовые звонки (Agents/Realtime).
- Оценка произношения.
- Мультиязычность (только английский).

---

## 3. Стек

| Слой | Выбор |
|---|---|
| Язык | Python 3.12 |
| Бот | aiogram 3 |
| LLM | OpenRouter (OpenAI-совместимый API) |
| Собеседник | Gemini Flash (vision). Fallback: Qwen VL / Grok |
| Аналитик | Gemini Flash-Lite или DeepSeek (JSON-ответ) |
| STT | ElevenLabs Scribe v2 (batch) |
| TTS | ElevenLabs Flash v2.5 (или v3 Conversational) |
| Аудио | ffmpeg (конвертация в OGG/Opus для sendVoice) |
| БД | PostgreSQL 16, SQLAlchemy 2 (async) + Alembic |
| Фоновые задачи | asyncio tasks (фаза 1), APScheduler (фаза 2) |
| Деплой | Docker Compose на своём сервере |

**Проверить при старте** (ID и форматы могли поменяться):
- точные model id на OpenRouter для Gemini Flash / Flash-Lite;
- model id Scribe v2 и TTS в ElevenLabs;
- поддерживает ли ElevenLabs TTS вывод в opus — если да, можно обойтись без ffmpeg.

Все model id — в конфиге, не в коде.

---

## 4. Архитектура

```
Telegram
   │
   ▼
aiogram handlers ──► MessageService
   │                    │
   │      ┌─────────────┼───────────────────┐
   │      ▼             ▼                   ▼
   │   STT (Scribe)  Companion (LLM)    Analyst (LLM, фон)
   │                    │                   │
   │                    ▼                   ▼
   │               ответ текстом      mistakes / vocab → Postgres
   │                    │
   │              [🔊] TTS (ElevenLabs) → ffmpeg → sendVoice
   ▼
Postgres (users, messages, mistakes, vocab, usage)
```

### Поток: текст
1. Сохранить сообщение пользователя.
2. Собрать контекст (§7) → Companion → ответ.
3. Отправить ответ + inline-кнопка «🔊». Если режим «всегда голосом» — сразу TTS.
4. Параллельно (`asyncio.create_task`) → Analyst → запись в `mistakes` / `vocab`.

### Поток: голос
1. Скачать OGG из Telegram → Scribe → текст.
2. (Опционально) показать распознанный текст мелким сообщением / в ответе — полезно для обучения.
3. Дальше как текст.

### Поток: фото
1. Скачать фото, передать в Companion как image input + подпись пользователя (если есть).
2. Инструкция: «отреагируй как друг, одно-два предложения, один вопрос по теме».
3. Сохранить в `messages` краткое описание фото (из ответа модели или отдельным полем), чтобы не гонять картинку в каждый следующий запрос.

### Поток: кнопка «🔊»
1. Взять текст ответа из `messages` по id.
2. Кэш: если аудио уже генерировалось — отдать `file_id` из Telegram.
3. Иначе TTS → OGG/Opus → `sendVoice` → сохранить `file_id`.

---

## 5. Режимы исправлений

| Режим | Поведение собеседника |
|---|---|
| `chat` (по умолчанию) | Ошибки не упоминает. Может мягко «перефразировать правильно» в своём ответе (recast), как делает нейтив. |
| `correct` | Короткая строка исправлений перед ответом: `✏️ I went → I have been`. Максимум 2 исправления, только существенные. |
| `review` | В диалоге молчит; по команде `/review` или после N реплик — разбор сессии из таблицы `mistakes`. |

Аналитик работает **во всех режимах** одинаково.

---

## 6. Промпты (скелеты)

### Companion (system)
```
You are {persona_name}, a friendly native English speaker from {persona_origin}.
You are chatting with {user_name}, an English learner (level ~{level}).

Rules:
- This is a casual chat between friends, NOT a lesson.
- Keep replies short: 1–3 sentences, then usually one natural follow-up question.
- Match the user's level: {level_guidelines}.
- Never switch to Russian unless the user explicitly asks for a translation.
- Correction mode: {mode_instructions}
- If the user sends a photo: react to it like a friend would, then ask one question about it.
{phase2: - Naturally use or prompt these items when it fits: {review_items}}
```

### Analyst (system)
Отвечает строго JSON (использовать `response_format` json, если модель поддерживает):
```json
{
  "mistakes": [
    {
      "original": "I go to cinema yesterday",
      "corrected": "I went to the cinema yesterday",
      "type": "tense|article|preposition|word_choice|word_order|spelling|other",
      "explanation_ru": "Прошедшее время: go → went; cinema с артиклем the",
      "severity": 1
    }
  ],
  "vocab": [
    { "term": "blockbuster", "translation_ru": "блокбастер", "source": "bot|user" }
  ],
  "estimated_level": "B1"
}
```
Правила: не считать ошибкой разговорные нормы (gonna, lowercase, отсутствие точки); severity 1–3; пустые массивы, если всё ок.

---

## 7. Контекст и память

- В запрос собеседнику: system + **саммари старой части диалога** + **последние 12 реплик**.
- Когда история > 30 реплик — дешёвой моделью обновить `conversation_summary` в `users` и обрезать.
- Фото в истории — только текстовым описанием (кроме текущего сообщения).
- System prompt держать стабильным в начале — для кэширования на стороне провайдера, где поддерживается.

---

## 8. Модель данных (черновик)

```sql
CREATE TABLE users (
  id               BIGINT PRIMARY KEY,          -- telegram user id
  username         TEXT,
  first_name       TEXT,
  level            TEXT DEFAULT 'B1',            -- A1..C2
  mode             TEXT DEFAULT 'chat',          -- chat|correct|review
  voice_always     BOOLEAN DEFAULT FALSE,
  persona          TEXT DEFAULT 'us_casual',
  timezone         TEXT DEFAULT 'Europe/Moscow',
  ping_time        TIME,                         -- фаза 2
  conversation_summary TEXT,
  streak_days      INT DEFAULT 0,
  last_active_at   TIMESTAMPTZ,
  created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE messages (
  id            BIGSERIAL PRIMARY KEY,
  user_id       BIGINT REFERENCES users(id),
  role          TEXT NOT NULL,                   -- user|assistant
  kind          TEXT NOT NULL,                   -- text|voice|photo
  text          TEXT,                            -- для voice — транскрипт
  photo_caption TEXT,                            -- описание фото
  tg_message_id BIGINT,
  tts_file_id   TEXT,                            -- кэш озвучки
  created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON messages (user_id, created_at DESC);

CREATE TABLE mistakes (
  id            BIGSERIAL PRIMARY KEY,
  user_id       BIGINT REFERENCES users(id),
  message_id    BIGINT REFERENCES messages(id),
  original      TEXT NOT NULL,
  corrected     TEXT NOT NULL,
  type          TEXT NOT NULL,
  explanation_ru TEXT,
  severity      SMALLINT DEFAULT 1,
  times_seen    INT DEFAULT 1,
  resolved      BOOLEAN DEFAULT FALSE,           -- фаза 2/3
  next_review_at TIMESTAMPTZ,                    -- фаза 2/3
  created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON mistakes (user_id, resolved, next_review_at);

CREATE TABLE vocab (
  id            BIGSERIAL PRIMARY KEY,
  user_id       BIGINT REFERENCES users(id),
  term          TEXT NOT NULL,
  translation_ru TEXT,
  source        TEXT,                            -- bot|user|photo
  status        TEXT DEFAULT 'new',              -- new|learning|known
  ease          REAL DEFAULT 2.5,                -- SRS (SM-2), фаза 3
  next_review_at TIMESTAMPTZ,
  created_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE (user_id, term)
);

CREATE TABLE usage (
  id            BIGSERIAL PRIMARY KEY,
  user_id       BIGINT REFERENCES users(id),
  kind          TEXT NOT NULL,                   -- llm_companion|llm_analyst|stt|tts
  model         TEXT,
  input_tokens  INT,
  output_tokens INT,
  audio_seconds REAL,
  chars         INT,
  cost_usd      NUMERIC(10,6),
  created_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON usage (user_id, created_at);

-- фаза 3
CREATE TABLE lessons (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT REFERENCES users(id),
  payload     JSONB NOT NULL,
  status      TEXT DEFAULT 'new',                -- new|done
  created_at  TIMESTAMPTZ DEFAULT now()
);
```

`usage` — с первого дня: из неё считаем реальную себестоимость пользователя и лимиты.

---

## 9. Лимиты и стоимость

- Дневной лимит реплик на пользователя (конфиг, напр. 50); голосовой ответ считается отдельно (напр. 20/день).
- Максимальная длина голосового входа — 60 сек (длиннее — просим короче).
- Максимальная длина ответа для TTS — ~400 символов.
- Ориентир себестоимости среднего пользователя: ~$4–6/мес (LLM ~$1, Scribe ~$0.4, TTS ~$2.5–3 при озвучке по кнопке — меньше).
- Админ-команда `/stats` (только для ADMIN_IDS): пользователи, реплики, расход за сутки/месяц из `usage`.

---

## 10. Конфиг (.env)

```
TELEGRAM_BOT_TOKEN=
ADMIN_IDS=

OPENROUTER_API_KEY=
COMPANION_MODEL=            # Gemini Flash id на OpenRouter
COMPANION_FALLBACK_MODEL=
ANALYST_MODEL=              # Flash-Lite / DeepSeek
SUMMARY_MODEL=

ELEVENLABS_API_KEY=
STT_MODEL=                  # Scribe v2 id
TTS_MODEL=                  # eleven_flash_v2_5 или актуальный
VOICE_US_CASUAL=            # voice_id
VOICE_UK_CASUAL=

DATABASE_URL=postgresql+asyncpg://bot:bot@db:5432/bot
DAILY_MSG_LIMIT=50
DAILY_TTS_LIMIT=20
HISTORY_WINDOW=12
```

---

## 11. Структура репозитория

```
english-buddy/
├─ DESIGN.md
├─ README.md
├─ docker-compose.yml
├─ Dockerfile
├─ .env.example
├─ alembic/                 # миграции
├─ app/
│  ├─ main.py               # запуск бота
│  ├─ config.py             # pydantic-settings
│  ├─ bot/
│  │  ├─ handlers/          # start, text, voice, photo, callbacks, commands
│  │  ├─ keyboards.py
│  │  └─ middlewares.py     # user upsert, лимиты
│  ├─ services/
│  │  ├─ dialog.py          # MessageService: оркестрация потоков
│  │  ├─ companion.py
│  │  ├─ analyst.py
│  │  ├─ memory.py          # контекст, саммари
│  │  ├─ stt.py             # ElevenLabs Scribe
│  │  ├─ tts.py             # ElevenLabs TTS + ffmpeg
│  │  └─ usage.py           # учёт расходов
│  ├─ llm/
│  │  └─ openrouter.py      # клиент, ретраи, fallback
│  ├─ prompts/              # шаблоны промптов (.md / jinja)
│  ├─ personas.py
│  └─ db/
│     ├─ models.py
│     └─ repo.py
└─ tests/
```

---

## 12. Деплой

- `docker-compose`: сервисы `bot` и `db` (Postgres с volume).
- В образе бота — `ffmpeg`.
- Long polling для MVP (без вебхуков и домена).
- Логи — stdout (structlog/loguru), ошибки LLM/STT/TTS — с ретраями и понятным ответом пользователю («не расслышал, повтори?»).
- Бэкап БД — `pg_dump` по cron.

---

## 13. План работ фазы 1

1. Скелет репо, конфиг, docker-compose, Postgres + миграции.
2. Онбординг и `/start`, upsert пользователя.
3. Текстовый диалог с Companion + история + режим `chat`.
4. Голосовой вход (Scribe).
5. Кнопка «🔊» и режим «всегда голосом» (TTS + кэш file_id).
6. Фото → разговор.
7. Analyst в фоне + `/mistakes`.
8. Режимы `correct` / `review`.
9. Лимиты + таблица `usage` + `/stats`.
10. Деплой на сервер, тест на себе 1–2 недели.

**Критерий готовности фазы 1:** 10–15 минут разговора подряд ощущаются как болтовня с человеком, а не с учебником.

---

## 14. Открытые вопросы

- Сколько персонажей-голосов на старте (2 достаточно?).
- Показывать ли транскрипт голосового пользователю всегда или по кнопке.
- Какая модель-собеседник реально «живее» — сравнить 2–3 кандидата на одинаковых диалогах до финального выбора.
- Название бота.
