from dataclasses import dataclass

from app.config import Settings


@dataclass(frozen=True)
class Persona:
    key: str
    name: str
    flag: str
    origin: str
    bio: str
    voice_setting: str  # имя поля в Settings с voice_id

    def voice_id(self, settings: Settings) -> str:
        return getattr(settings, self.voice_setting)


PERSONAS: dict[str, Persona] = {
    "us_casual": Persona(
        key="us_casual",
        name="Jake",
        flag="🇺🇸",
        origin="Austin, Texas",
        bio=(
            "28, works at a coffee roastery, plays bass in a garage band, "
            "loves hiking, tacos and terrible puns"
        ),
        voice_setting="voice_us_casual",
    ),
    "uk_casual": Persona(
        key="uk_casual",
        name="Emma",
        flag="🇬🇧",
        origin="Manchester, England",
        bio=(
            "30, a graphic designer, mad about football and rainy-day baking, "
            "always planning her next cheap weekend trip"
        ),
        voice_setting="voice_uk_casual",
    ),
}

DEFAULT_PERSONA = "us_casual"


def get_persona(key: str | None) -> Persona:
    return PERSONAS.get(key or DEFAULT_PERSONA, PERSONAS[DEFAULT_PERSONA])
