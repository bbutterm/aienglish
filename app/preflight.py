"""Предстартовая проверка ключей и внешних сервисов.

Запуск в контейнере: docker compose run --rm bot python -m app.preflight
Тратит копейки: два коротких запроса к LLM, ~50 символов озвучки и ~2 секунды распознавания.
Код выхода 0 — всё готово к запуску, 1 — есть ошибки.
"""

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.session import create_engine
from app.llm.openrouter import OpenRouterClient
from app.personas import PERSONAS
from app.services.stt import SpeechToText
from app.services.tts import TextToSpeech

TTS_SAMPLE = "Hey! How is it going?"

HINTS = {
    "detected_unusual_activity": (
        "ElevenLabs отключил бесплатный тариф для этого IP — нужен платный план (Starter)"
    ),
    "quota_exceeded": "в ElevenLabs закончился лимит тарифа",
    "missing_permissions": (
        "у ключа ElevenLabs не хватает прав: нужны Text to Speech и Speech to Text"
    ),
    "invalid_api_key": "неверный ключ",
    "HTTP 401": "неверный ключ",
    "HTTP 402": "на балансе OpenRouter нет денег — пополните баланс",
    "No endpoints found": "модель недоступна на OpenRouter — смените ID в .env",
}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    warning: bool = False

    def line(self) -> str:
        icon = "⚠️" if self.warning else ("✅" if self.ok else "❌")
        return f"{icon} {self.name}" + (f" — {self.detail}" if self.detail else "")


def with_hint(error: str) -> str:
    for marker, hint in HINTS.items():
        if marker in error:
            return f"{error[:200]} → {hint}"
    return error[:300]


class Preflight:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        telegram_base_url: str = "https://api.telegram.org",
    ) -> None:
        self.settings = settings
        self.http = http
        self.telegram_base_url = telegram_base_url
        self._secrets = [
            s
            for s in (
                settings.telegram_bot_token,
                settings.openrouter_api_key,
                settings.elevenlabs_api_key,
            )
            if s
        ]

    def scrub(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, "***")
        return value

    async def run(self, *, include_database: bool = True) -> list[Check]:
        steps: list[tuple[str, Callable[[], Awaitable[list[Check]]]]] = [
            ("Telegram", self.telegram),
            ("OpenRouter: ключ", self.openrouter_key),
            ("OpenRouter: модели", self.openrouter_models),
            ("OpenRouter: запросы", self.llm_requests),
            ("ElevenLabs", self.speech),
        ]
        if include_database:
            steps.append(("База данных", self.database))
        checks: list[Check] = []
        for name, step in steps:
            try:
                checks.extend(await step())
            except Exception as exc:  # отчёт должен дойти до конца при любой ошибке
                checks.append(Check(name, False, with_hint(f"{type(exc).__name__}: {exc}")))
        return [Check(c.name, c.ok, self.scrub(c.detail), c.warning) for c in checks]

    async def telegram(self) -> list[Check]:
        base = f"{self.telegram_base_url}/bot{self.settings.telegram_bot_token}"
        me = (await self.http.get(f"{base}/getMe", timeout=15)).json()
        if not me.get("ok"):
            return [Check("Telegram: токен", False, with_hint(str(me.get("description"))))]
        checks = [Check("Telegram: токен", True, f"бот @{me['result']['username']}")]
        webhook = (await self.http.get(f"{base}/getWebhookInfo", timeout=15)).json()
        url = (webhook.get("result") or {}).get("url")
        if url:
            checks.append(
                Check(
                    "Telegram: webhook",
                    True,
                    f"установлен webhook {url}; бот снимет его при старте (long polling)",
                    warning=True,
                )
            )
        return checks

    async def openrouter_key(self) -> list[Check]:
        response = await self.http.get(
            f"{self.settings.openrouter_base_url.rstrip('/')}/key",
            headers={"Authorization": f"Bearer {self.settings.openrouter_api_key}"},
            timeout=15,
        )
        if response.status_code != 200:
            return [
                Check(
                    "OpenRouter: ключ",
                    False,
                    with_hint(f"HTTP {response.status_code}: {response.text[:200]}"),
                )
            ]
        data = response.json().get("data") or {}
        remaining = data.get("limit_remaining")
        if remaining is not None and remaining <= 0:
            return [Check("OpenRouter: ключ", False, "лимит ключа исчерпан")]
        checks = [
            Check(
                "OpenRouter: ключ",
                True,
                "лимит ключа: " + ("нет" if remaining is None else f"осталось ${remaining:.2f}"),
            )
        ]
        if data.get("is_free_tier"):
            checks.append(
                Check(
                    "OpenRouter: баланс",
                    True,
                    "аккаунт ни разу не пополнялся — платные модели не ответят",
                    warning=True,
                )
            )
        return checks

    async def openrouter_models(self) -> list[Check]:
        response = await self.http.get(
            f"{self.settings.openrouter_base_url.rstrip('/')}/models", timeout=30
        )
        response.raise_for_status()
        catalog = {m["id"]: m for m in response.json()["data"]}
        s = self.settings
        roles = [
            ("собеседник", s.companion_model, True),
            ("fallback собеседника", s.companion_fallback_model, True),
            ("аналитик", s.analyst_model, False),
            ("саммари", s.summary_model, False),
        ]
        checks = []
        for role, model_id, needs_vision in roles:
            name = f"Модель ({role})"
            if not model_id:
                checks.append(Check(name, True, "не задана", warning=True))
                continue
            model = catalog.get(model_id)
            if model is None:
                checks.append(Check(name, False, f"{model_id} нет в каталоге OpenRouter"))
                continue
            modalities = (model.get("architecture") or {}).get("input_modalities") or []
            if needs_vision and "image" not in modalities:
                checks.append(Check(name, False, f"{model_id} не принимает картинки"))
                continue
            expires = model.get("expiration_date")
            checks.append(
                Check(
                    name,
                    True,
                    model_id + (f", снимается с OpenRouter {expires}" if expires else ""),
                    warning=bool(expires),
                )
            )
        return checks

    async def llm_requests(self) -> list[Check]:
        s = self.settings
        llm = OpenRouterClient(
            self.http, api_key=s.openrouter_api_key, base_url=s.openrouter_base_url, backoff=0.5
        )
        checks = []
        reply = await llm.chat(
            [{"role": "user", "content": "Say hi to an English learner in five words."}],
            models=[s.companion_model],
            max_tokens=300,
            reasoning_effort=s.companion_reasoning_effort or None,
        )
        cost = f", ${reply.cost_usd:.6f}" if reply.cost_usd is not None else ""
        checks.append(Check("LLM: собеседник отвечает", True, f"«{reply.text[:60]}»{cost}"))

        analysis = await llm.chat(
            [
                {"role": "system", "content": 'Return only this JSON object: {"ok": true}'},
                {"role": "user", "content": "Check"},
            ],
            models=[s.analyst_model],
            max_tokens=300,
            json_mode=True,
            reasoning_effort=s.analyst_reasoning_effort or None,
        )
        raw = analysis.text.strip().removeprefix("```json").removesuffix("```")
        json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        checks.append(Check("LLM: аналитик отдаёт JSON", True, analysis.model))
        return checks

    async def speech(self) -> list[Check]:
        tts = TextToSpeech(self.http, self.settings)
        checks = []
        sample: bytes | None = None
        for persona in PERSONAS.values():
            name = f"Озвучка: голос {persona.name}"
            try:
                audio = await tts.synthesize(TTS_SAMPLE, persona.voice_id(self.settings))
            except Exception as exc:
                checks.append(Check(name, False, with_hint(str(exc))))
                continue
            sample = sample or audio
            checks.append(Check(name, True, f"OGG/Opus {len(audio) // 1024 + 1} КБ"))
        if sample is None:
            checks.append(Check("Распознавание речи", False, "нечего распознавать: озвучка упала"))
            return checks
        try:
            transcript = await SpeechToText(self.http, self.settings).transcribe(sample)
        except Exception as exc:
            checks.append(Check("Распознавание речи", False, with_hint(str(exc))))
            return checks
        checks.append(Check("Распознавание речи", bool(transcript), f"«{transcript}»"))
        return checks

    async def database(self) -> list[Check]:
        engine = create_engine(self.settings.database_url)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                try:
                    revision = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                except SQLAlchemyError:
                    revision = None
        finally:
            await engine.dispose()
        detail = (
            f"схема на ревизии {revision}"
            if revision
            else "подключение есть, схема создастся при старте бота"
        )
        return [Check("База данных", True, detail)]


async def main() -> int:
    logging.basicConfig(level=logging.ERROR)  # ретраи клиентов не засоряют отчёт
    try:
        settings = Settings()
    except ValidationError as exc:
        missing = ", ".join(str(err["loc"][0]).upper() for err in exc.errors())
        print(f"❌ .env: не заданы или неверны переменные: {missing}")
        return 1
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as http:
        checks = await Preflight(settings, http).run()
    for check in checks:
        print(check.line())
    failed = [c for c in checks if not c.ok]
    print()
    print("Готово к запуску." if not failed else f"Ошибок: {len(failed)}. Запуск не готов.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
