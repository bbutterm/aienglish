import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json_object(raw: str) -> dict[str, Any]:
    """JSON-объект из ответа модели: терпит ```-обёртку и текст вокруг."""
    text = _FENCE_RE.sub("", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model response")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("model response is not a JSON object")
    return data
