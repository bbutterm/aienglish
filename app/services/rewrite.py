"""«Скажи как носитель»: фраза ученика так, как сказал бы носитель, и что поменялось."""

import json
from dataclasses import dataclass, field

from app.config import Settings
from app.db.models import User
from app.llm.jsonutil import extract_json_object
from app.llm.openrouter import LLMResult, OpenRouterClient
from app.prompts import render

MAX_CHANGES = 3


@dataclass
class Rewrite:
    native: str
    changes: list[dict[str, str]] = field(default_factory=list)

    def is_same(self, original: str) -> bool:
        normalize = lambda text: "".join(c for c in text.lower() if c.isalnum())  # noqa: E731
        return not self.changes or normalize(self.native) == normalize(original)


def parse_rewrite(raw: str, original: str) -> Rewrite:
    data = extract_json_object(raw)
    native = data.get("native")
    native = " ".join(native.split()) if isinstance(native, str) and native.strip() else original
    changes = []
    raw_changes = data.get("changes")
    for item in raw_changes if isinstance(raw_changes, list) else []:
        if not isinstance(item, dict):
            continue
        before, after = item.get("from"), item.get("to")
        why = item.get("why_ru")
        if isinstance(before, str) and isinstance(after, str) and before.strip() != after.strip():
            changes.append(
                {
                    "from": before.strip()[:120],
                    "to": after.strip()[:120],
                    "why_ru": why.strip()[:200] if isinstance(why, str) else "",
                }
            )
        if len(changes) >= MAX_CHANGES:
            break
    return Rewrite(native=native[:1000], changes=changes)


class Rewriter:
    def __init__(self, llm: OpenRouterClient, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings

    async def rewrite(
        self, user: User, text: str, prev_bot_text: str | None
    ) -> tuple[Rewrite, LLMResult]:
        context = {"friend_previous_message": prev_bot_text, "learner_message": text}
        result = await self._llm.chat(
            [
                {
                    "role": "system",
                    "content": render(
                        "rewrite_system.md.j2", user_name=user.name, level=user.level
                    ),
                },
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            models=[self._settings.analyst_model, self._settings.companion_model],
            temperature=0.3,
            max_tokens=800,
            json_mode=True,
            reasoning_effort=self._settings.analyst_reasoning_effort or None,
        )
        return parse_rewrite(result.text, text), result
