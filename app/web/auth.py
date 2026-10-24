"""Авторизация Mini App: Telegram подписывает initData токеном бота, сервер проверяет подпись."""

from datetime import UTC, datetime, timedelta

from aiogram.utils.web_app import WebAppUser, safe_parse_webapp_init_data


class InitDataError(Exception):
    pass


def parse_init_data(
    init_data: str, bot_token: str, max_age: timedelta, now: datetime | None = None
) -> WebAppUser:
    try:
        data = safe_parse_webapp_init_data(bot_token, init_data)
    except ValueError as exc:
        raise InitDataError("invalid signature") from exc
    if data.user is None:
        raise InitDataError("no user in init data")
    if (now or datetime.now(UTC)) - data.auth_date > max_age:
        raise InitDataError("init data expired")
    return data.user
