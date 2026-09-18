"""Runtime settings, read once from environment variables (see .env.example).

API keys are read here and handed to the HTTP client only.
They are never logged and never returned in any API response.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field

log = logging.getLogger("gridwise.config")

# provider name -> (wire format, default base URL, default model)
# "openai" wire format = POST {base_url}/chat/completions (OpenAI-compatible APIs)
# "anthropic" wire format = POST {base_url}/v1/messages (Claude API)
PRESETS: dict[str, tuple[str, str, str]] = {
    "anthropic": ("anthropic", "https://api.anthropic.com", ""),
    "groq": ("openai", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "gemini": ("openai", "https://generativelanguage.googleapis.com/v1beta/openai", ""),
    "openai": ("openai", "https://api.openai.com/v1", ""),
    "openrouter": ("openai", "https://openrouter.ai/api/v1", ""),
    "ollama": ("openai", "http://localhost:11434/v1", ""),
    "custom": ("openai", "", ""),  # any OpenAI-compatible server: set LLM_BASE_URL + LLM_MODEL
}


@dataclass(frozen=True)
class ProviderConfig:
    label: str  # "primary" or "backup"
    provider: str  # preset name, e.g. "groq"
    style: str  # "openai" or "anthropic"
    base_url: str
    model: str
    api_key: str = field(repr=False)  # repr=False: never printed by accident
    extra_body: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Settings:
    providers: tuple[ProviderConfig, ...]
    llm_timeout_s: float
    llm_max_tries: int
    interpret_budget_s: float
    cache_size: int


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _env_number(name: str, default: float, low: float, high: float) -> float:
    value = _env(name)
    if not value:
        return default
    try:
        number = float(value)
    except ValueError:
        log.warning("%s is not a number; using default", name)
        return default
    if not math.isfinite(number):
        return default
    return min(max(number, low), high)


def _provider_from_env(prefix: str, label: str) -> ProviderConfig | None:
    name = _env(prefix + "PROVIDER").lower()
    if not name:
        return None
    if name not in PRESETS:
        log.error("Unknown %sPROVIDER. Allowed: %s", prefix, ", ".join(PRESETS))
        return None

    style, default_url, default_model = PRESETS[name]
    base_url = (_env(prefix + "BASE_URL") or default_url).rstrip("/")
    model = _env(prefix + "MODEL") or default_model
    api_key = _env(prefix + "API_KEY")

    extra_body: dict = {}
    raw_extra = _env(prefix + "EXTRA_BODY")
    if raw_extra:
        try:
            parsed = json.loads(raw_extra)
            if isinstance(parsed, dict):
                extra_body = parsed
            else:
                log.warning("%sEXTRA_BODY must be a JSON object; ignored", prefix)
        except json.JSONDecodeError:
            log.warning("%sEXTRA_BODY is not valid JSON; ignored", prefix)

    if not base_url or not model:
        log.error("%s LLM is missing a base URL or model (set %sBASE_URL / %sMODEL)", label, prefix, prefix)
        return None
    if not api_key and name not in ("ollama", "custom"):
        log.warning("%sAPI_KEY is empty; provider disabled", prefix)
        return None

    return ProviderConfig(label, name, style, base_url, model, api_key, extra_body)


def load_settings() -> Settings:
    providers = [
        p
        for p in (
            _provider_from_env("LLM_", "primary"),
            _provider_from_env("BACKUP_LLM_", "backup"),
        )
        if p is not None
    ]
    return Settings(
        providers=tuple(providers),
        llm_timeout_s=_env_number("LLM_TIMEOUT_S", 8.0, 1.0, 12.0),
        llm_max_tries=int(_env_number("LLM_MAX_TRIES", 2, 1, 4)),
        interpret_budget_s=_env_number("INTERPRET_BUDGET_S", 20.0, 2.0, 22.0),
        cache_size=int(_env_number("NOTE_CACHE_SIZE", 2048, 0, 100_000)),
    )
