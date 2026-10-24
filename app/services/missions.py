"""Ролевые миссии: собеседник играет роль, аналитик отмечает выполненные цели ученика.

Миссия живёт в users.active_mission (JSONB):
{"idea_id", "title_ru", "role_en", "setting_en", "goals": [{"en", "ru"}], "done": [0, 2],
 "started_at": iso, "mistakes_at_start": int}
"""

from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

MISSION_TTL = timedelta(hours=24)
GOALS = 3


def pending_goals(mission: dict[str, Any] | None) -> list[str]:
    """Цели для аналитика — все, по-английски; выполненные он просто не повторит."""
    if not mission:
        return []
    return [goal["en"] for goal in mission.get("goals", [])]


def apply_goals(mission: dict[str, Any], indexes: list[int]) -> tuple[dict[str, Any], list[int]]:
    """Отмечает цели выполненными; возвращает новую миссию и только что выполненные цели."""
    done = set(mission.get("done", []))
    new = [i for i in indexes if i not in done and 0 <= i < len(mission.get("goals", []))]
    return {**mission, "done": sorted(done | set(new))}, new


def is_complete(mission: dict[str, Any]) -> bool:
    goals = mission.get("goals", [])
    return bool(goals) and len(set(mission.get("done", []))) >= len(goals)


def is_expired(mission: dict[str, Any], now: datetime | None = None) -> bool:
    started = mission.get("started_at")
    if not started:
        return False
    return (now or datetime.now(UTC)) - datetime.fromisoformat(started) > MISSION_TTL


def new_mission(idea_id: int, title_ru: str, content: dict[str, Any], mistakes: int) -> dict:
    return {
        "idea_id": idea_id,
        "title_ru": title_ru,
        "role_en": content["role_en"],
        "setting_en": content["setting_en"],
        "goals": content["goals"],
        "done": [],
        "started_at": datetime.now(UTC).isoformat(),
        "mistakes_at_start": mistakes,
    }


def goals_list_ru(mission: dict[str, Any]) -> str:
    done = set(mission.get("done", []))
    return "\n".join(
        f"{'✅' if i in done else '▫️'} {escape(goal['ru'])}"
        for i, goal in enumerate(mission["goals"])
    )
