import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
# Начало JSON-объекта: «{"» или «{}». Скобки в прозе вроде «{placeholders}» пропускаем.
_OBJECT_START_RE = re.compile(r"\{\s*[\"}]")
_DECODER = json.JSONDecoder()


def extract_json_object(raw: str) -> dict[str, Any]:
    """JSON-объект из ответа модели: терпит ```-обёртку и текст вокруг, даже со {скобками}."""
    text = _FENCE_RE.sub("", raw.strip())
    match = _OBJECT_START_RE.search(text)
    if match is None:
        raise ValueError("no JSON object in model response")
    # Первый объект; всё, что после него (пояснения модели, второй объект), не мешает.
    data, _ = _DECODER.raw_decode(text, match.start())
    return data
