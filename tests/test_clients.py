import json

import httpx
import pytest

from app.config import Settings
from app.llm.openrouter import LLMError, OpenRouterClient
from app.services.stt import SpeechToText, STTError
from app.services.tts import TextToSpeech, TTSError
from tests.conftest import OGG_BYTES, openrouter_response


def client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_openrouter_success_parses_usage_and_sends_options() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["Authorization"], json.loads(request.content)))
        return httpx.Response(200, json=openrouter_response("  Hi!  ", "m1", cost=0.0021))

    async with client_with(handler) as http:
        llm = OpenRouterClient(http, api_key="key", backoff=0)
        result = await llm.chat(
            [{"role": "user", "content": "hey"}],
            models=["m1"],
            json_mode=True,
            reasoning_effort="minimal",
            max_tokens=50,
        )
    assert result.text == "Hi!"
    assert (result.model, result.input_tokens, result.output_tokens, result.cost_usd) == (
        "m1",
        100,
        20,
        0.0021,
    )
    auth, body = seen[0]
    assert auth == "Bearer key"
    assert body["response_format"] == {"type": "json_object"}
    assert body["reasoning"] == {"effort": "minimal", "exclude": True}
    assert body["max_tokens"] == 50


async def test_openrouter_retries_then_succeeds() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, text="overloaded")
        if len(calls) == 2:
            return httpx.Response(200, json=openrouter_response(""))  # пустой ответ
        return httpx.Response(200, json=openrouter_response("ok"))

    async with client_with(handler) as http:
        llm = OpenRouterClient(http, api_key="key", retries=2, backoff=0)
        result = await llm.chat([{"role": "user", "content": "x"}], models=["m1"])
    assert result.text == "ok"
    assert len(calls) == 3


async def test_openrouter_falls_back_to_second_model_on_client_error() -> None:
    models = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "primary":
            return httpx.Response(400, json={"error": {"message": "bad model"}})
        return httpx.Response(200, json=openrouter_response("from fallback", model))

    async with client_with(handler) as http:
        llm = OpenRouterClient(http, api_key="key", backoff=0)
        result = await llm.chat([{"role": "user", "content": "x"}], models=["primary", "fallback"])
    assert result.text == "from fallback"
    assert models == ["primary", "fallback"]


async def test_openrouter_raises_when_all_models_fail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "provider down"}})

    async with client_with(handler) as http:
        llm = OpenRouterClient(http, api_key="key", retries=1, backoff=0)
        with pytest.raises(LLMError):
            await llm.chat([{"role": "user", "content": "x"}], models=["a", "", "b"])


async def test_stt_sends_multipart_and_returns_text(settings: Settings) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": " Hello there ", "language_code": "en"})

    async with client_with(handler) as http:
        text = await SpeechToText(http, settings).transcribe(OGG_BYTES)
    assert text == "Hello there"
    request = seen[0]
    assert str(request.url) == "https://api.elevenlabs.io/v1/speech-to-text"
    assert request.headers["xi-api-key"] == "el-test"
    body = request.content
    assert b'name="model_id"\r\n\r\nscribe_v2' in body
    assert b'name="language_code"\r\n\r\nen' in body
    assert b'filename="voice.ogg"' in body


async def test_stt_raises_on_client_error_without_retry(settings: Settings) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, text="invalid key")

    async with client_with(handler) as http:
        with pytest.raises(STTError):
            await SpeechToText(http, settings).transcribe(OGG_BYTES)
    assert len(calls) == 1


async def test_tts_requests_opus_and_returns_ogg(settings: Settings) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=OGG_BYTES)

    async with client_with(handler) as http:
        audio = await TextToSpeech(http, settings).synthesize("Hi!", "voice123")
    assert audio == OGG_BYTES
    request = seen[0]
    assert request.url.path == "/v1/text-to-speech/voice123"
    assert request.url.params["output_format"] == "opus_48000_64"
    assert json.loads(request.content) == {"text": "Hi!", "model_id": "eleven_flash_v2_5"}


async def test_tts_error(settings: Settings) -> None:
    async with client_with(lambda r: httpx.Response(500, text="boom")) as http:
        with pytest.raises(TTSError):
            await TextToSpeech(http, settings).synthesize("Hi!", "voice123")
