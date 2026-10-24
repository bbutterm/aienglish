import asyncio
import logging
import re

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

_MARKDOWN_RE = re.compile(r"[*_`#~]")
_SPACES_RE = re.compile(r"\s+")


class TTSError(Exception):
    pass


def prepare_tts_text(text: str, max_chars: int) -> str:
    """Текст для озвучки: без строки исправлений ✏️ и разметки, не длиннее max_chars."""
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("✏")
    ]
    cleaned = _SPACES_RE.sub(" ", _MARKDOWN_RE.sub("", " ".join(lines))).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    cut = cleaned[:max_chars]
    if cleaned[max_chars] == " " and cut.endswith((".", "!", "?")):
        return cut
    boundary = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if boundary + 1 >= max_chars // 3:
        return cut[: boundary + 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut).rstrip(",;:—- ") + "…"


async def convert_to_ogg_opus(audio: bytes, timeout: float = 30) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "libopus",
        "-b:a",
        "48k",
        "-f",
        "ogg",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(audio), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        raise TTSError("ffmpeg timed out") from exc
    if proc.returncode != 0 or not out:
        raise TTSError(f"ffmpeg failed: {err.decode(errors='replace')[:300]}")
    return out


async def ensure_ogg_opus(audio: bytes) -> bytes:
    """Telegram sendVoice ждёт OGG/Opus. Если ElevenLabs уже отдал Ogg — ffmpeg не нужен."""
    if audio[:4] == b"OggS":
        return audio
    return await convert_to_ogg_opus(audio)


class TextToSpeech:
    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        self._settings = settings

    async def synthesize(self, text: str, voice_id: str) -> bytes:
        """Озвучка для голосового сообщения Telegram (OGG/Opus)."""
        return await ensure_ogg_opus(await self.synthesize_raw(text, voice_id))

    async def synthesize_raw(
        self, text: str, voice_id: str, output_format: str | None = None
    ) -> bytes:
        """Аудио в формате ElevenLabs как есть (по умолчанию — TTS_OUTPUT_FORMAT)."""
        if not voice_id:
            raise TTSError("voice_id is not configured")
        url = f"{self._settings.elevenlabs_base_url.rstrip('/')}/v1/text-to-speech/{voice_id}"
        try:
            response = await self._http.post(
                url,
                params={"output_format": output_format or self._settings.tts_output_format},
                json={"text": text, "model_id": self._settings.tts_model},
                headers={"xi-api-key": self._settings.elevenlabs_api_key},
                timeout=60,
            )
        except httpx.HTTPError as exc:
            raise TTSError(f"transport error: {exc!r}") from exc
        if response.status_code != 200:
            raise TTSError(f"HTTP {response.status_code}: {response.text[:300]}")
        return response.content
