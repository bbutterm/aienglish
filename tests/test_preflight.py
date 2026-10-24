import httpx
import pytest

from app.config import Settings
from app.preflight import Preflight
from tests.conftest import FakeAPIs

MODELS = {
    "google/gemini-3.8-flash": ["text", "image"],
    "qwen/qwen3-vl-235b-a22b-instruct": ["text", "image"],
    "google/gemini-3.5-flash-lite": ["text", "image"],
}


class PreflightAPIs(FakeAPIs):
    def __init__(self) -> None:
        super().__init__()
        self.tts_error: str | None = None
        self.key_data: dict = {"limit_remaining": None, "is_free_tier": False}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "api.telegram.org":
            if url.path.endswith("/getMe"):
                return httpx.Response(200, json={"ok": True, "result": {"username": "buddy_bot"}})
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        if url.host == "openrouter.ai" and url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": self.key_data})
        if url.host == "openrouter.ai" and url.path == "/api/v1/models":
            data = [
                {"id": mid, "architecture": {"input_modalities": mods}}
                for mid, mods in MODELS.items()
            ]
            return httpx.Response(200, json={"data": data})
        if url.path.startswith("/v1/text-to-speech/") and self.tts_error:
            return httpx.Response(401, json={"detail": {"status": self.tts_error}})
        return super().handler(request)

    def _llm(self, body: dict) -> str:
        if body.get("response_format"):
            return '{"ok": true}'
        return "Hey there, welcome aboard friend!"


async def run_preflight(settings: Settings, apis: PreflightAPIs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(apis.handler)) as http:
        return await Preflight(settings, http).run(include_database=False)


async def test_preflight_all_green(settings: Settings) -> None:
    apis = PreflightAPIs()
    checks = await run_preflight(settings, apis)
    assert all(c.ok for c in checks), [c.line() for c in checks]
    lines = "\n".join(c.line() for c in checks)
    assert "бот @buddy_bot" in lines
    assert "Модель (собеседник) — google/gemini-3.8-flash" in lines
    assert "«I go to cinema yesterday»" in lines  # STT распознал озвучку
    assert len(apis.tts_requests) == 2  # оба голоса
    assert settings.openrouter_api_key not in lines


async def test_preflight_reports_problems_with_hints(settings: Settings) -> None:
    apis = PreflightAPIs()
    apis.tts_error = "detected_unusual_activity"
    apis.key_data = {"limit_remaining": None, "is_free_tier": True}
    settings.analyst_model = "vendor/missing-model"
    checks = {c.name: c for c in await run_preflight(settings, apis)}

    assert checks["OpenRouter: баланс"].warning
    assert not checks["Модель (аналитик)"].ok
    voice = checks["Озвучка: голос Jake"]
    assert not voice.ok and "нужен платный план" in voice.detail
    assert not checks["Распознавание речи"].ok


async def test_preflight_database_check(settings: Settings, engine) -> None:
    async with httpx.AsyncClient() as http:
        checks = await Preflight(settings, http).database()
    assert checks[0].ok


@pytest.mark.parametrize(
    ("error", "expected"),
    [("HTTP 402: payment required", "пополните баланс"), ("weird", "weird")],
)
def test_hints(error: str, expected: str) -> None:
    from app.preflight import with_hint

    assert expected in with_hint(error)
