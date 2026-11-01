import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(Exception):
    pass


class _RetryableError(LLMError):
    pass


class _TimeoutError(LLMError):
    """Модель не ответила за таймаут — повтор той же модели снова займёт минуту."""


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class LLMParseError(ValueError):
    """Модель ответила (и это стоило денег), но ответ не разобрать — расход всё равно учитываем."""

    def __init__(self, message: str, result: LLMResult) -> None:
        super().__init__(message)
        self.result = result


class OpenRouterClient:
    """Клиент OpenRouter (OpenAI-совместимый chat/completions) с ретраями и fallback-моделью."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 60.0,
        retries: int = 2,
        backoff: float = 1.0,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._timeout = timeout
        self._retries = retries
        self._backoff = backoff

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        models: Sequence[str],
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        reasoning_effort: str | None = None,
    ) -> LLMResult:
        candidates = [m for m in models if m]
        if not candidates:
            raise LLMError("no model configured")
        last_error: Exception | None = None
        for model in candidates:
            for attempt in range(self._retries + 1):
                try:
                    return await self._call(
                        model, messages, temperature, max_tokens, json_mode, reasoning_effort
                    )
                except _RetryableError as exc:
                    last_error = exc
                    logger.warning("LLM %s attempt %d failed: %s", model, attempt + 1, exc)
                    if attempt < self._retries:
                        await asyncio.sleep(self._backoff * 2**attempt)
                except LLMError as exc:  # в т.ч. таймаут: сразу к запасной модели
                    last_error = exc
                    logger.warning("LLM %s failed: %s", model, exc)
                    break
        raise LLMError(f"all models failed: {last_error}") from last_error

    async def _call(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool,
        reasoning_effort: str | None,
    ) -> LLMResult:
        # usage.include — чтобы OpenRouter вернул стоимость запроса (usage.cost) для учёта.
        body: dict[str, Any] = {"model": model, "messages": messages, "usage": {"include": True}}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort, "exclude": True}

        try:
            response = await self._http.post(
                self._url,
                json=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "X-Title": "English Buddy Bot",
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise _TimeoutError(f"timeout: {exc!r}") from exc
        except httpx.HTTPError as exc:
            raise _RetryableError(f"transport error: {exc!r}") from exc

        if response.status_code in RETRYABLE_STATUSES:
            raise _RetryableError(f"HTTP {response.status_code}: {response.text[:300]}")
        if response.status_code >= 400:
            raise LLMError(f"HTTP {response.status_code}: {response.text[:300]}")

        try:
            data = response.json()
        except ValueError as exc:
            raise _RetryableError("invalid JSON in response") from exc
        if not isinstance(data, dict):
            raise _RetryableError("response is not a JSON object")
        if data.get("error"):
            # OpenRouter может вернуть 200 с ошибкой провайдера внутри.
            raise _RetryableError(f"provider error: {data['error']}")

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _RetryableError("empty choices")
        message = choices[0].get("message")
        text = _extract_text(message.get("content") if isinstance(message, dict) else None)
        if not text.strip():
            reason = choices[0].get("finish_reason")
            if reason == "length":
                # Рассуждения съели max_tokens: повтор той же модели снова заплатит за пустоту.
                raise LLMError("empty content: max_tokens spent on reasoning")
            raise _RetryableError(f"empty content (finish_reason={reason})")

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return LLMResult(
            text=text.strip(),
            model=data.get("model") or model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cost_usd=usage.get("cost"),
        )


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""
