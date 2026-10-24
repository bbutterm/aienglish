from app.config import Settings


def test_admin_ids_parsed_from_comma_separated_string(settings: Settings) -> None:
    assert settings.admin_ids == [1, 2]


def test_admin_ids_empty_string() -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_token="t",
        openrouter_api_key="k",
        elevenlabs_api_key="k",
        admin_ids="",
    )
    assert settings.admin_ids == []


def test_admin_ids_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_IDS", "42")
    settings = Settings(
        _env_file=None, telegram_bot_token="t", openrouter_api_key="k", elevenlabs_api_key="k"
    )
    assert settings.admin_ids == [42]
