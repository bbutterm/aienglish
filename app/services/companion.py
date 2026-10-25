import base64
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.config import Settings
from app.db.models import Message, User
from app.llm.openrouter import LLMResult, OpenRouterClient
from app.personas import get_persona
from app.prompts import render

LEVEL_GUIDELINES = {
    "A1": "very simple words, short present-tense sentences, no idioms or slang.",
    "A2": "simple everyday words and short sentences; basic past and future; avoid idioms.",
    "B1": "natural everyday English with common phrasal verbs; "
    "avoid rare words and complex grammar.",
    "B2": "natural conversational English with idioms and phrasal verbs; "
    "now and then use a useful new expression.",
    "C1": "talk like you would with a native friend: idioms, slang, humour and nuance.",
    "C2": "talk like you would with a native friend: idioms, slang, humour and nuance.",
}

MODE_INSTRUCTIONS = {
    "chat": (
        "never point out the user's mistakes. If they make one, you may naturally reuse "
        "the correct form in your own reply (a recast), the way a native friend would."
    ),
    "correct": (
        "if the user's latest message has meaningful mistakes, start your answer with ONE line "
        "in exactly this format: ✏️ wrong fragment → correct fragment (max 2 corrections, "
        "separated by ' · '; ignore punctuation, capital letters and casual style). Then reply "
        "normally on the next line. If there are no meaningful mistakes, skip that line."
    ),
    "review": (
        "never mention or correct the user's mistakes — they will be reviewed later. Just chat."
    ),
}

OPENER_NOTES = {
    "onboarding": (
        "[Chat note, not written by the user: {name} just joined the chat. Say hi, introduce "
        "yourself in one short sentence and ask one easy, fun question to get talking.]"
    ),
    "reset": (
        "[Chat note, not written by the user: {name} wants to talk about something new. "
        "Start a fresh topic, different from the earlier ones, with one short fun question. "
        "Don't mention this note.]"
    ),
    "lesson": (
        "[Chat note, not written by the user: {name} just finished a short English lesson in the "
        'app; its topic was "{topic}". Bring this topic up naturally, the way a friend would, '
        "with one short question. Don't call it a lesson and don't mention this note.]"
    ),
    "idea": (
        "[Chat note, not written by the user: {name} picked a topic to talk about in the app: "
        '"{topic}". Start talking about it the way a friend would — share a short thought of '
        "your own and ask one question. Don't mention the app or this note.]"
    ),
    "mission": (
        "[Chat note, not written by the user: the role-play scene starts now. Open it in "
        "character with one or two short lines that set the scene and give {name} a natural "
        "reason to speak. Don't mention this note.]"
    ),
    "comeback": (
        "[Chat note, not written by the user: {name} hasn't written for about a day. Message "
        "them first like a real friend would: pick up something specific from your earlier "
        "conversation (a plan, a problem, a story they told) and ask how it went. One or two "
        "short sentences, warm and casual. Don't mention this note or that they were away.]"
    ),
}

OpenerReason = Literal["onboarding", "reset", "lesson", "idea", "mission", "comeback"]

# Служебная строка о фото; модель иногда ставит её в конец, повторяет или оборачивает в **.
_PHOTO_TAG_RE = re.compile(r"[*_]*\[photo:\s*(?P<desc>[^\]\n]*)\][*_]*[ \t]*\n?", re.IGNORECASE)


@dataclass(frozen=True)
class CompanionReply:
    text: str
    photo_description: str | None
    llm: LLMResult


def build_system_prompt(user: User, review_items: Sequence[str] = ()) -> str:
    level = user.level if user.level in LEVEL_GUIDELINES else "B1"
    return render(
        "companion_system.md.j2",
        persona=get_persona(user.persona),
        user_name=user.name,
        level=level,
        level_guidelines=LEVEL_GUIDELINES[level],
        mode_instructions=MODE_INSTRUCTIONS.get(user.mode, MODE_INSTRUCTIONS["chat"]),
        review_items=list(review_items),
        mission=user.active_mission,
        mission_goals="; ".join(g["en"] for g in (user.active_mission or {}).get("goals", [])),
    )


def history_to_messages(history: Sequence[Message]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in history:
        if message.kind == "photo" and message.role == "user":
            parts = [f"[photo: {message.photo_caption or 'a photo'}]"]
            if message.text:
                parts.append(message.text)
            content = "\n".join(parts)
        else:
            content = message.text or ""
        if content.strip():
            result.append({"role": message.role, "content": content})
    return result


def split_photo_tag(text: str) -> tuple[str | None, str]:
    """Отделяет служебную строку `[photo: ...]` от ответа собеседника — где бы она ни стояла."""
    match = _PHOTO_TAG_RE.search(text)
    if not match:
        return None, text.strip()
    description = match.group("desc").strip() or None
    return description, _PHOTO_TAG_RE.sub("", text).strip()


class Companion:
    def __init__(self, llm: OpenRouterClient, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings

    def build_messages(
        self,
        user: User,
        history: Sequence[Message],
        summary: str | None,
        *,
        text: str | None = None,
        image: bytes | None = None,
        note: str | None = None,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": build_system_prompt(user)}]
        if summary:
            messages.append(
                {
                    "role": "system",
                    "content": f"Summary of your earlier conversation with {user.name}: {summary}",
                }
            )
        messages.extend(history_to_messages(history))
        if image is not None:
            encoded = base64.b64encode(image).decode()
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text or "(sent a photo without a caption)"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                        },
                    ],
                }
            )
        elif text:
            messages.append({"role": "user", "content": text})
        if note:
            messages.append({"role": "user", "content": note})
        return messages

    async def reply(
        self,
        user: User,
        history: Sequence[Message],
        summary: str | None,
        *,
        text: str | None,
        image: bytes | None = None,
    ) -> CompanionReply:
        messages = self.build_messages(user, history, summary, text=text, image=image)
        return await self._complete(messages, expect_photo=image is not None)

    async def opener(
        self,
        user: User,
        history: Sequence[Message],
        summary: str | None,
        *,
        reason: OpenerReason,
        topic: str | None = None,
    ) -> CompanionReply:
        note = OPENER_NOTES[reason].format(name=user.name, topic=topic or "everyday life")
        messages = self.build_messages(user, history, summary, note=note)
        return await self._complete(messages, expect_photo=False)

    async def _complete(self, messages: list[dict[str, Any]], expect_photo: bool) -> CompanionReply:
        result = await self._llm.chat(
            messages,
            models=[self._settings.companion_model, self._settings.companion_fallback_model],
            temperature=0.9,
            max_tokens=1024,
            reasoning_effort=self._settings.companion_reasoning_effort or None,
        )
        description, text = split_photo_tag(result.text)
        if not text:
            text = result.text
        return CompanionReply(
            text=text,
            photo_description=description if expect_photo else None,
            llm=result,
        )
