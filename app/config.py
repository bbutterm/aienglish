from functools import lru_cache
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: str
    admin_ids: Annotated[list[int], NoDecode] = []

    # LLM (OpenRouter)
    openrouter_api_key: str
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    companion_model: str = "google/gemini-3.8-flash"
    companion_fallback_model: str = "qwen/qwen3-vl-235b-a22b-instruct"
    analyst_model: str = "google/gemini-3.5-flash-lite"
    summary_model: str = "google/gemini-3.5-flash-lite"
    # Глубина «размышлений» reasoning-моделей: none|minimal|low|medium|high. Пусто — не передаём.
    companion_reasoning_effort: str = "minimal"
    analyst_reasoning_effort: str = "minimal"
    llm_timeout_seconds: float = 60.0

    # ElevenLabs
    elevenlabs_api_key: str
    elevenlabs_base_url: str = "https://api.elevenlabs.io"
    stt_model: str = "scribe_v2"
    stt_language: str = "en"
    tts_model: str = "eleven_flash_v2_5"
    tts_output_format: str = "opus_48000_64"
    voice_us_casual: str = "bIHbv24MWmeRgasZH58o"
    voice_uk_casual: str = "Xb7hH8MSUJpSbSDYk0k2"
    # Цены для учёта расходов в `usage` (LLM-стоимость OpenRouter отдаёт сам).
    stt_usd_per_hour: float = 0.22
    tts_usd_per_1k_chars: float = 0.04

    # БД
    database_url: str = "postgresql+asyncpg://bot:bot@db:5432/bot"

    # Лимиты и поведение
    daily_msg_limit: int = 50
    daily_tts_limit: int = 20
    history_window: int = 12
    summary_trigger: int = 30
    max_voice_seconds: int = 60
    tts_max_chars: int = 400
    review_every: int = 10
    show_transcript: bool = True
    log_level: str = "INFO"

    # Премиум: лимиты вместо дневных выше, пока users.premium_until в будущем
    premium_daily_msg_limit: int = 200
    premium_daily_tts_limit: int = 100

    # Mini App
    webapp_domain: str = ""  # домен Mini App; Caddy выпускает для него HTTPS
    webapp_url: str = ""  # по умолчанию https://<webapp_domain>; пусто — Mini App выключен
    web_host: str = "0.0.0.0"
    web_port: int = 8000
    init_data_ttl_hours: int = 24
    lesson_model: str = "google/gemini-3.8-flash"
    lesson_reasoning_effort: str = "low"
    daily_lesson_limit: int = 1
    premium_daily_lesson_limit: int = 3
    # Аудирование в уроках играет браузер Telegram: mp3 поддерживается везде, в отличие от Ogg/Opus
    lesson_audio_format: str = "mp3_44100_64"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_admin_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.replace(" ", "").split(",") if part]
        if isinstance(value, int):
            return [value]
        return value

    @model_validator(mode="after")
    def _derive_webapp_url(self) -> "Settings":
        if not self.webapp_url and self.webapp_domain:
            self.webapp_url = f"https://{self.webapp_domain}"
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
