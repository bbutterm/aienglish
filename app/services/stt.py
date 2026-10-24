import asyncio
import logging

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class STTError(Exception):
    pass


class SpeechToText:
    """ElevenLabs Scribe (batch): OGG/Opus голосовое Telegram → текст."""

    def __init__(self, http: httpx.AsyncClient, settings: Settings, retries: int = 1) -> None:
        self._http = http
        self._settings = settings
        self._retries = retries

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        data = {"model_id": self._settings.stt_model, "tag_audio_events": "false"}
        if self._settings.stt_language:
            data["language_code"] = self._settings.stt_language
        url = self._settings.elevenlabs_base_url.rstrip("/") + "/v1/speech-to-text"

        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = await self._http.post(
                    url,
                    data=data,
                    files={"file": (filename, audio, "audio/ogg")},
                    headers={"xi-api-key": self._settings.elevenlabs_api_key},
                    timeout=60,
                )
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code == 200:
                    return (response.json().get("text") or "").strip()
                last_error = STTError(f"HTTP {response.status_code}: {response.text[:300]}")
                if response.status_code < 500 and response.status_code != 429:
                    break
            logger.warning("STT attempt %d failed: %s", attempt + 1, last_error)
            if attempt < self._retries:
                await asyncio.sleep(1)
        raise STTError(str(last_error)) from last_error
