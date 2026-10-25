import asyncio
import logging

import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.bot.handlers import build_router
from app.bot.middlewares import DbSessionMiddleware, UserMiddleware
from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.features import FeatureFlags
from app.llm.openrouter import OpenRouterClient
from app.services.analyst import Analyst
from app.services.companion import Companion
from app.services.dialog import BackgroundTasks, DialogService
from app.services.extract import WordExtractor
from app.services.memory import Memory
from app.services.pings import PingScheduler
from app.services.rewrite import Rewriter
from app.services.stt import SpeechToText
from app.services.tts import TextToSpeech

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="settings", description="Озвучка, собеседник, уровень"),
    BotCommand(command="mistakes", description="Последние ошибки"),
    BotCommand(command="review", description="Разбор ошибок"),
    BotCommand(command="reset", description="Новая тема"),
    BotCommand(command="stop", description="Выйти из ролевой сцены"),
    BotCommand(command="app", description="Уроки и прогресс"),
    BotCommand(command="premium", description="Премиум"),
    BotCommand(command="help", description="Как это работает"),
]


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_llm(settings: Settings, http: httpx.AsyncClient) -> OpenRouterClient:
    return OpenRouterClient(
        http,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        timeout=settings.llm_timeout_seconds,
    )


def build_dialog(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    http: httpx.AsyncClient,
    tasks: BackgroundTasks,
    features: FeatureFlags | None = None,
) -> DialogService:
    llm = build_llm(settings, http)
    return DialogService(
        settings=settings,
        session_factory=session_factory,
        companion=Companion(llm, settings),
        analyst=Analyst(llm, settings),
        memory=Memory(llm, settings, session_factory),
        stt=SpeechToText(http, settings),
        tts=TextToSpeech(http, settings),
        tasks=tasks,
        features=features or FeatureFlags(session_factory),
        rewriter=Rewriter(llm, settings),
        extractor=WordExtractor(llm, settings),
    )


def build_dispatcher(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    dialog: DialogService,
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(DbSessionMiddleware(session_factory))
    dp.message.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(UserMiddleware())
    dp.include_router(build_router())
    dp["settings"] = settings
    dp["dialog"] = dialog
    dp["features"] = dialog.features
    return dp


async def run(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
    tasks = BackgroundTasks()
    dialog = build_dialog(settings, session_factory, http, tasks)
    dp = build_dispatcher(settings, session_factory, dialog)
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    try:
        # getUpdates не работает, пока у бота висит webhook (например, от прошлого хостинга).
        await bot.delete_webhook()
        await bot.set_my_commands(BOT_COMMANDS)
        if settings.webapp_url:
            # Кнопка «Уроки» слева от поля ввода открывает Mini App.
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text=texts.APP_MENU_BUTTON, web_app=WebAppInfo(url=settings.webapp_url)
                )
            )
        # «Друг пишет первым» — фоновый планировщик в процессе бота.
        pings = asyncio.create_task(PingScheduler(bot, dialog, session_factory).run())
        logger.info("Starting long polling")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if "pings" in locals():
            pings.cancel()
        await tasks.drain()
        await bot.session.close()
        await http.aclose()
        await engine.dispose()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
