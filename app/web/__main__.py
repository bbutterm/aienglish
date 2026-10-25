"""Запуск Mini App: python -m app.web"""

import asyncio

import httpx
import uvicorn
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.main import secrets_of, setup_logging
from app.web.app import build_state, create_app


async def serve(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    state = build_state(settings, create_session_factory(engine), http, bot)
    config = uvicorn.Config(
        create_app(state),
        host=settings.web_host,
        port=settings.web_port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level=settings.log_level.lower(),
    )
    try:
        await uvicorn.Server(config).serve()
    finally:
        await state.tasks.drain()
        await bot.session.close()
        await http.aclose()
        await engine.dispose()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, secrets_of(settings))
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
