"""Small async client for chat-style LLM APIs, built on httpx (no vendor SDKs).

Two wire formats:
  * "openai"    -> POST {base_url}/chat/completions   (OpenAI, Groq, Gemini, OpenRouter, Ollama, ...)
  * "anthropic" -> POST {base_url}/v1/messages        (Claude API)

Robustness details:
  * If a provider rejects an optional parameter (e.g. response_format or
    temperature on some reasoning models) we drop that parameter, remember it
    for this provider and retry immediately.
  * Errors are raised as LLMError(retryable=...) so the interpreter can decide
    whether to retry, switch to the backup provider, or fall back.
  * Error messages are redacted: API keys never reach logs or responses.
"""

from __future__ import annotations

import logging
import math
import re
import time
from email.utils import parsedate_to_datetime

import httpx

from .config import ProviderConfig

log = logging.getLogger("gridwise.llm")

MAX_OUTPUT_TOKENS = 4096
_DROPPABLE_OPENAI = ("response_format", "temperature", "reasoning_effort", "top_p", "seed")
_DROPPABLE_ANTHROPIC = ("temperature", "top_p")  # max_tokens is mandatory for the Claude API
_KEY_PATTERN = re.compile(r"(sk-[A-Za-z0-9_\-*]{6,}|gsk_[A-Za-z0-9]{6,}|AIza[0-9A-Za-z_\-]{10,}|Bearer\s+\S+)")


class LLMError(Exception):
    def __init__(self, message: str, retryable: bool, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _retry_after_seconds(value: str | None) -> float | None:
    """Accept Retry-After seconds or an HTTP date; reject unsafe numeric values."""
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def _redact(text: str, secret: str) -> str:
    if secret:
        text = text.replace(secret, "[redacted]")
    return _KEY_PATTERN.sub("[redacted]", text)


class LLMClient:
    def __init__(self, providers: tuple[ProviderConfig, ...] | list[ProviderConfig]):
        self.providers = list(providers)
        self._http = httpx.AsyncClient(limits=httpx.Limits(max_connections=32, max_keepalive_connections=16))
        self._dropped: dict[str, set[str]] = {}  # provider label -> params it rejected

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete(self, provider: ProviderConfig, system: str, user: str, timeout: float) -> str:
        """Send one system+user prompt and return the model's text reply."""
        if provider.style == "anthropic":
            url = f"{provider.base_url}/v1/messages"
            headers = {
                "x-api-key": provider.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {
                "model": provider.model,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "temperature": 0,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            droppable = _DROPPABLE_ANTHROPIC
        else:
            url = f"{provider.base_url}/chat/completions"
            headers = {"content-type": "application/json"}
            if provider.api_key:
                headers["authorization"] = f"Bearer {provider.api_key}"
            body = {
                "model": provider.model,
                "temperature": 0,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            droppable = _DROPPABLE_OPENAI
        if set(provider.extra_body) & {"model", "messages", "system", "stream", "max_tokens", "max_completion_tokens"}:
            raise LLMError("EXTRA_BODY overrides a protected request field", retryable=False)
        body.update(provider.extra_body)

        dropped = self._dropped.setdefault(provider.label, set())
        for _ in range(4):
            payload = {k: v for k, v in body.items() if k not in dropped}
            try:
                response = await self._http.post(url, headers=headers, json=payload, timeout=timeout)
            except httpx.TimeoutException:
                raise LLMError(f"{provider.label} LLM timed out after {timeout:.1f}s", retryable=True) from None
            except httpx.HTTPError as exc:
                raise LLMError(f"{provider.label} LLM network error: {type(exc).__name__}", retryable=True) from None

            if response.status_code in (400, 422):
                if provider.style == "openai" and "max_tokens" in payload and "max_completion_tokens" in response.text:
                    body["max_completion_tokens"] = body.pop("max_tokens")
                    continue
                bad_param = self._rejected_param(response.text, payload, droppable)
                if bad_param:
                    dropped.add(bad_param)
                    log.info("%s LLM rejected '%s'; retrying without it", provider.label, bad_param)
                    continue
            if response.status_code >= 400:
                raise self._http_error(provider, response)
            return self._extract_text(provider, response)
        raise LLMError(f"{provider.label} LLM kept rejecting request parameters", retryable=False)

    @staticmethod
    def _rejected_param(error_text: str, payload: dict, droppable: tuple[str, ...]) -> str | None:
        lowered = error_text.lower()
        # Groq-style "json_validate_failed": JSON mode itself failed -> retry in plain mode, we parse JSON ourselves
        if "response_format" in payload and ("json_validate_failed" in lowered or "failed to generate json" in lowered):
            return "response_format"
        for key in droppable:
            if key in payload and key.lower() in lowered:
                return key
        return None

    @staticmethod
    def _http_error(provider: ProviderConfig, response: httpx.Response) -> LLMError:
        status = response.status_code
        retry_after = _retry_after_seconds(response.headers.get("retry-after"))
        if status in (401, 403):
            return LLMError(f"{provider.label} LLM auth failed (HTTP {status}) - check the API key", retryable=False)
        retryable = status in (408, 409, 425, 429) or status >= 500
        return LLMError(f"{provider.label} LLM HTTP {status}", retryable=retryable, retry_after=retry_after)

    @staticmethod
    def _extract_text(provider: ProviderConfig, response: httpx.Response) -> str:
        try:
            data = response.json()
            if provider.style == "anthropic":
                text = "".join(
                    block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
                )
            else:
                content = data["choices"][0]["message"].get("content")
                if isinstance(content, list):  # some providers return content parts
                    content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                text = content or ""
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError(f"{provider.label} LLM returned an unexpected response shape", retryable=True) from None
        if not isinstance(text, str) or not text.strip():
            raise LLMError(f"{provider.label} LLM returned an empty reply", retryable=True)
        return text
