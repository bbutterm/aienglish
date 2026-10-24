# Разворачивание English Buddy Bot — инструкция для агента

Эта инструкция написана для ИИ-агента (или человека), который разворачивает бота на Linux-сервере.
Шаги идут по порядку, у каждого есть проверка. Не переходи к следующему шагу, пока проверка не прошла.

**Результат:** бот работает на сервере в Docker Compose (бот + Postgres + ежедневные бэкапы),
переживает перезагрузку, владелец проверил его в Telegram, а агент прислал отчёт (шаг 10).

## Что даёт владелец

Только секреты. Всё остальное агент делает и выясняет сам.

| Переменная | Что это | Где взять |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | токен бота | @BotFather → `/newbot` |
| `OPENROUTER_API_KEY` | ключ LLM, на балансе должны быть деньги (хватит $5) | openrouter.ai → Keys |
| `ELEVENLABS_API_KEY` | ключ для озвучки и распознавания, права: Text to Speech, Speech to Text | elevenlabs.io → Developers → API Keys |
| `GITHUB_TOKEN` | доступ к приватному репозиторию `bbutterm/aienglish` | GitHub → Settings → Developer settings → Fine-grained tokens: только этот репозиторий, Contents: Read-only |

Плюс доступ к серверу, если агент работает не на нём (тогда все команды ниже выполняются через `ssh`).

`ADMIN_IDS` (Telegram ID владельца для `/stats`) агент определяет сам на шаге 8.

## Правила для агента

- **Не выводи секреты** в чат, логи и отчёт. Проверяй их наличие, но не значения.
- **Не коммить `.env`** и не публикуй порт Postgres наружу: в `docker-compose.yml` портов нет, так и оставь.
- **Не правь код на сервере.** Если падает код (Traceback в логах бота), не чини на месте: пришли
  владельцу ошибку и последние 50 строк лога.
- **Останавливайся и спрашивай владельца**, когда для исправления нужно его действие: неверный токен,
  нет денег на OpenRouter, заблокирован бесплатный тариф ElevenLabs, нет доступа к репозиторию.
- Сам решай технические вопросы: установить Docker, добавить swap, заменить исчезнувший ID модели (шаг 6).

## 0. Переменные

```bash
export REPO=bbutterm/aienglish
export BRANCH=main            # если в main ещё нет кода — claude/new-session-tz50oa
export APP_DIR=/opt/english-buddy
# секреты владельца (дальше в командах — только через переменные, не печатать их):
export TELEGRAM_BOT_TOKEN='...'
export OPENROUTER_API_KEY='...'
export ELEVENLABS_API_KEY='...'
export GITHUB_TOKEN='...'
```

Все команды ниже рассчитаны на одну shell-сессию, в которой заданы эти переменные. Если твои
команды выполняются в отдельных сессиях, повторяй `export` (или `cd "$APP_DIR"`) там, где они нужны.

## 1. Сервер

Требования: Ubuntu 22.04/24.04 или Debian 12, x86_64 или arm64, от 1 vCPU, от 1 ГБ RAM
(лучше 2 ГБ), от 10 ГБ свободного диска. Входящие порты не нужны: бот сам опрашивает Telegram.
Сервер должен быть **вне РФ**: OpenRouter и ElevenLabs могут не работать с российских IP.

```bash
. /etc/os-release && echo "$PRETTY_NAME, $(uname -m)"
free -h && df -h /
```

Если RAM меньше 2 ГБ и swap нет — добавь 2 ГБ swap:

```bash
if [ "$(free -m | awk '/Mem:/{print $2}')" -lt 1900 ] && [ -z "$(swapon --show)" ]; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi
```

Доступ во внешний мир — любой HTTP-код подходит, `000` значит хост недоступен:

```bash
for u in https://api.telegram.org https://openrouter.ai https://api.elevenlabs.io \
         https://github.com https://ghcr.io https://registry-1.docker.io https://pypi.org; do
  printf '%-30s %s\n' "$u" "$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$u")"
done
```

**Проверка:** ни одного `000`. Иначе — сервер не подходит, сообщи владельцу.

## 2. Docker

```bash
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
docker compose version
```

**Проверка:** `docker compose version` печатает версию v2. Если пользователь не root и `docker`
требует sudo — выполняй дальнейшие команды `docker` через `sudo`.

## 3. Код

Репозиторий приватный. Токен кладём в хранилище git, чтобы потом работал `git pull`:

```bash
git config --global credential.helper store
printf 'https://x-access-token:%s@github.com\n' "$GITHUB_TOKEN" > ~/.git-credentials
chmod 600 ~/.git-credentials
sudo mkdir -p "$APP_DIR" && sudo chown "$(id -u):$(id -g)" "$APP_DIR"
git clone --branch "$BRANCH" "https://github.com/$REPO.git" "$APP_DIR"
cd "$APP_DIR" && git log --oneline -1
```

**Проверка:** в каталоге есть `docker-compose.yml`, `.env.example`, `app/preflight.py`.
Если клон прошёл, а этих файлов нет — в ветке `main` нет кода: возьми `BRANCH=claude/new-session-tz50oa`
и сообщи владельцу. Ошибка 403/404 при клоне — у токена нет доступа к репозиторию, спроси владельца.

## 4. Конфигурация

```bash
cd "$APP_DIR"
cp .env.example .env && chmod 600 .env
sed -i "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}|" .env
sed -i "s|^OPENROUTER_API_KEY=.*|OPENROUTER_API_KEY=${OPENROUTER_API_KEY}|" .env
sed -i "s|^ELEVENLABS_API_KEY=.*|ELEVENLABS_API_KEY=${ELEVENLABS_API_KEY}|" .env
grep -cE '^(TELEGRAM_BOT_TOKEN|OPENROUTER_API_KEY|ELEVENLABS_API_KEY)=.+' .env
```

**Проверка:** последняя команда печатает `3`. Остальные значения в `.env` оставь как есть:
модели, голоса и лимиты уже выбраны. `DATABASE_URL` для Docker переопределяется в `docker-compose.yml`.

## 5. Сборка

```bash
docker compose build
```

**Проверка:** сборка закончилась без ошибок (первый раз — несколько минут).

## 6. Предстартовая проверка (preflight)

```bash
docker compose run --rm bot python -m app.preflight
```

Скрипт поднимает Postgres и проверяет всё, что нужно боту: токен Telegram, ключ и баланс OpenRouter,
наличие моделей из `.env` в каталоге, живые запросы к собеседнику и аналитику, озвучку обоими голосами
и распознавание речи, подключение к БД. Тратит меньше цента.

**Проверка:** в конце `Готово к запуску.`, все строки ✅ (⚠️ допустимы, упомяни их в отчёте).

Что делать с ❌:

| Строка / текст ошибки | Что делать |
|---|---|
| `.env: не заданы или неверны переменные` | вернись к шагу 4 |
| `Telegram: токен — Unauthorized` | неверный токен бота → спроси владельца |
| `OpenRouter … → неверный ключ` | неверный ключ → спроси владельца |
| `HTTP 402` / `аккаунт ни разу не пополнялся` | нет денег на OpenRouter → владелец пополняет баланс |
| `Модель (…) — … нет в каталоге` или `No endpoints found` | ID модели сменился → замени сам (см. ниже) |
| `→ нужен платный план` (`detected_unusual_activity`) | ElevenLabs заблокировал бесплатный тариф для IP сервера → владелец оформляет платный план (Starter) |
| `закончился лимит тарифа` | у ElevenLabs кончились символы → владелец повышает тариф |
| `не хватает прав` | ключ ElevenLabs создан с урезанными правами → владелец выпускает ключ с Text to Speech и Speech to Text |
| `База данных` | `docker compose logs db`; обычно не хватает места на диске |

Замена модели. Найди актуальные ID:

```bash
curl -s https://openrouter.ai/api/v1/models | python3 -c '
import json, sys
for m in json.load(sys.stdin)["data"]:
    mid = m["id"]
    if mid.startswith("google/gemini") and "flash" in mid and "image" not in mid and ":" not in mid:
        print(m["id"], m["architecture"]["input_modalities"], m["pricing"]["prompt"])'
```

Правила выбора (ID без суффиксов вида `:batch`): `COMPANION_MODEL` — самая новая не-preview Gemini Flash, у которой в
`input_modalities` есть `image`; `ANALYST_MODEL` и `SUMMARY_MODEL` — самая новая Gemini Flash-Lite;
`COMPANION_FALLBACK_MODEL` — vision-модель другого вендора (например, `qwen/qwen3-vl-*-instruct`).
Поправь `.env` и повтори preflight. Замену укажи в отчёте.

## 7. Запуск

```bash
docker compose up -d
sleep 20
docker compose ps
docker compose logs --tail=50 bot
ls -lh backups/
```

**Проверка:**
- `docker compose ps`: у `bot` и `backup` статус `Up …`, у `db` — `Up … (healthy)`; `Restarting` — проблема;
- в логе бота есть `Starting long polling` и `Run polling for bot @<имя>` и нет `Traceback`;
- при первом запуске в логе есть `Running upgrade  -> 0001, initial schema`;
- в `backups/` появился файл `bot-*.sql.gz`.

Если бот перезапускается по кругу — смотри раздел «Диагностика».

## 8. Проверка владельцем и права админа

Попроси владельца открыть `https://t.me/<имя бота>` и пройти сценарий:
1. `/start` → имя → уровень → собеседник; бот сам пишет первым;
2. текстовое сообщение по-английски, лучше с ошибкой (`I go to cinema yesterday`);
3. голосовое на 5–10 секунд;
4. фото с подписью или без;
5. кнопка «🔊 Озвучить» под любым ответом;
6. `/mistakes`.

Пока владелец пишет, смотри `docker compose logs --since 10m bot` — там не должно быть `Traceback`
и `failed`. Потом проверь базу:

```bash
q() { docker compose exec -T db psql -U bot -d bot -c "$1"; }
q "select id, username, display_name, level, persona, onboarded from users;"
q "select role, kind, left(text, 50) as text from messages order by id desc limit 8;"
q "select original, corrected, type from mistakes order by id desc limit 5;"
q "select kind, count(*), round(sum(cost_usd), 4) as usd from usage group by kind order by kind;"
```

**Проверка:** у владельца `onboarded = t`; в `messages` есть `text`, `voice` и `photo`;
в `mistakes` есть хотя бы одна запись; в `usage` есть `llm_companion`, `llm_analyst`, `stt` и `tts`.

Теперь сделай владельца админом:

```bash
ADMIN_ID=$(docker compose exec -T db psql -U bot -d bot -tAc \
  "select id from users where onboarded order by created_at limit 1")
echo "$ADMIN_ID"   # убедись, что это владелец: сверь username с первым запросом выше
sed -i "s|^ADMIN_IDS=.*|ADMIN_IDS=${ADMIN_ID}|" .env
docker compose up -d --force-recreate bot
```

**Проверка:** владелец отправляет `/stats` и получает статистику, а не «Команда только для админов».

## 9. Перезагрузка

```bash
systemctl is-enabled docker   # должно быть enabled
```

Если владелец не против короткого простоя — `sudo reboot`, затем `docker compose ps` в `$APP_DIR`:
все три сервиса должны подняться сами (`restart: unless-stopped`).

## 10. Отчёт владельцу

Пришли коротко, без секретов:
- сервер (ОС, RAM, регион, если известен), каталог `$APP_DIR`, коммит (`git log --oneline -1`);
- имя бота (`@...`);
- результат preflight, все ⚠️, заменённые модели (если были);
- результат проверки владельцем и SQL-проверок из шага 8;
- `ADMIN_IDS` выставлен, `/stats` работает;
- где лежат бэкапы и что они хранятся только на этом сервере;
- что осталось нерешённым.

---

## Обновление

```bash
cd "$APP_DIR"
docker compose exec -T db psql -U bot -d bot -tAc "select version_num from alembic_version"  # запомни ревизию
git log --oneline -1                                                                       # запомни коммит
git pull --ff-only
docker compose build bot
docker compose run --rm bot python -m app.preflight
docker compose up -d
docker compose logs --tail=50 bot
```

Миграции применяются автоматически при старте бота.

## Откат

```bash
cd "$APP_DIR"
# если в обновлении была новая миграция — сначала откати схему, пока стоит новый код:
docker compose run --rm bot alembic downgrade <ревизия до обновления>
git checkout <коммит до обновления>
docker compose up -d --build
```

## Восстановление из бэкапа

```bash
cd "$APP_DIR"
docker compose stop bot
docker compose exec -T db psql -U bot -d postgres \
  -c "DROP DATABASE bot WITH (FORCE);" -c "CREATE DATABASE bot OWNER bot;"
gunzip -c backups/bot-ГГГГММДД-ЧЧММ.sql.gz | docker compose exec -T db psql -U bot -d bot
docker compose start bot
```

Бэкапы делаются раз в сутки и хранятся 14 дней в `$APP_DIR/backups` — **на том же сервере**.
Копию за пределами сервера лучше настроить отдельно.

## Диагностика

| Симптом в `docker compose logs bot` | Причина и действие |
|---|---|
| `ValidationError` при старте, бот перезапускается | в `.env` пусто или опечатка → шаг 4, preflight |
| `TelegramUnauthorizedError` | неверный токен бота → владелец |
| `TelegramConflictError: terminated by other getUpdates request` | тот же токен запущен где-то ещё (локально или на другом сервере) → остановить другой экземпляр |
| `TelegramNetworkError` | нет доступа к api.telegram.org → шаг 1 |
| `all models failed: HTTP 402` | кончились деньги на OpenRouter → владелец |
| `all models failed: HTTP 404/400` или `No endpoints found` | ID модели устарел → шаг 6, замена модели |
| `TTS failed … detected_unusual_activity` | бесплатный тариф ElevenLabs заблокирован → владелец, платный план |
| `TTS failed … quota_exceeded` | кончились символы ElevenLabs → владелец |
| `STT failed` | права ключа ElevenLabs или лимит → preflight покажет точнее |
| бот отвечает «Ой, я задумался и потерял мысль» | ошибка LLM; причина в строке `Companion failed` выше |
| `No space left on device` | `docker system prune -f`, удалить старые файлы из `backups/` |

Полезное:

```bash
docker compose logs --since 1h bot | grep -E "ERROR|Traceback" -A 5
docker compose restart bot
docker compose exec -T db psql -U bot -d bot -c "select now();"
```
