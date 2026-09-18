"""Provider cooldowns must recover without exhausting attempts or the deadline."""
import asyncio
import json
from dataclasses import replace
from email.utils import formatdate
from types import SimpleNamespace

import httpx
import pytest

from app import interpreter, llm_client
from app.config import ProviderConfig, Settings
from app.interpreter import InterpretationError, NoteCache, interpret_notes
from app.llm_client import LLMClient, LLMError
from app.schemas import Battery

PRIMARY = ProviderConfig("primary", "custom", "openai", "https://example.invalid/v1", "test", "")
BACKUP = replace(PRIMARY, label="backup")
SETTINGS = Settings((PRIMARY,), 8, 2, 20, 0)
BATTERY = Battery(0, 0, 0, 0, 0)
REPLY = json.dumps({"interpretations": [{"note_index": 0, "directive_type": "no_op"}]})


@pytest.fixture
def clock(monkeypatch):
    """Advance only application cooldowns; real asyncio still executes each call."""
    clock = SimpleNamespace(now=100.0, sleeps=[])

    async def sleep(seconds):
        assert seconds > 0
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(interpreter, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(interpreter, "asyncio", SimpleNamespace(
        sleep=sleep, wait_for=asyncio.wait_for, TimeoutError=asyncio.TimeoutError))
    return clock


def interpret(client, settings=SETTINGS):
    return interpret_notes(["The cafeteria menu changed."], BATTERY, client, NoteCache(0), settings)


def test_http_429_waits_for_retry_after_then_succeeds(clock):
    calls = []

    def handler(request):
        calls.append(clock.now)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, json={"choices": [{"message": {"content": REPLY}}]})

    async def run():
        client = LLMClient([PRIMARY])
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result, sources = await interpret(client)
            assert result[0]["directive_type"] == "no_op"
            assert sources == ["llm:primary"]
        finally:
            await client.aclose()

    asyncio.run(run())
    assert len(calls) == 2
    assert 5 <= calls[1] - calls[0] < SETTINGS.interpret_budget_s


def test_backup_is_used_without_waiting_for_primary(clock):
    calls = []

    class Fake:
        providers = [PRIMARY, BACKUP]

        async def complete(self, provider, *args, **kwargs):
            calls.append(provider.label)
            if provider == PRIMARY:
                raise LLMError("rate limited", retryable=True, retry_after=10)
            return REPLY

    _, sources = asyncio.run(interpret(Fake()))
    assert sources == ["llm:backup"]
    assert calls == ["primary", "backup"]
    assert not clock.sleeps


def test_backup_retry_does_not_wait_for_primary_cooldown(clock):
    calls = []

    class Fake:
        providers = [PRIMARY, BACKUP]

        async def complete(self, provider, *args, **kwargs):
            calls.append(provider.label)
            if provider == PRIMARY:
                raise LLMError("rate limited", retryable=True, retry_after=10)
            if calls.count("backup") == 1:
                raise LLMError("busy", retryable=True, retry_after=1)
            return REPLY

    _, sources = asyncio.run(interpret(Fake()))
    assert sources == ["llm:backup"]
    assert calls == ["primary", "backup", "backup"]
    assert 1 <= sum(clock.sleeps) < 10


def test_retry_after_beyond_deadline_never_retries_early(clock):
    calls = []

    class Fake:
        providers = [PRIMARY]

        async def complete(self, *args, **kwargs):
            calls.append(clock.now)
            raise LLMError("rate limited", retryable=True, retry_after=60)

    with pytest.raises(InterpretationError):
        asyncio.run(interpret(Fake()))
    assert len(calls) == 1
    assert not clock.sleeps


def test_cooldown_reduces_remaining_network_budget(clock):
    timeouts = []

    class Fake:
        providers = [PRIMARY]

        async def complete(self, *args, timeout, **kwargs):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                raise LLMError("rate limited", retryable=True, retry_after=7.5)
            return REPLY

    asyncio.run(interpret(Fake(), replace(SETTINGS, llm_timeout_s=12, interpret_budget_s=10)))
    assert len(timeouts) == 2
    assert 0 < timeouts[1] <= 10 - sum(clock.sleeps)
    assert sum(clock.sleeps) + timeouts[1] <= 10


def test_missing_header_uses_backoff_and_attempt_limit(clock):
    calls = []

    class Fake:
        providers = [PRIMARY]

        async def complete(self, *args, **kwargs):
            calls.append(clock.now)
            raise LLMError("temporarily unavailable", retryable=True)

    with pytest.raises(InterpretationError):
        asyncio.run(interpret(Fake(), replace(SETTINGS, llm_max_tries=3)))
    assert len(calls) == 3
    assert calls[1] > calls[0]
    assert calls[2] - calls[1] > calls[1] - calls[0]
    assert sum(clock.sleeps) < SETTINGS.interpret_budget_s


def test_auth_failure_is_not_retried(clock):
    calls = []

    class Fake:
        providers = [PRIMARY]

        async def complete(self, *args, **kwargs):
            calls.append(clock.now)
            raise LLMError("auth failed", retryable=False, retry_after=5)

    with pytest.raises(InterpretationError):
        asyncio.run(interpret(Fake()))
    assert len(calls) == 1
    assert not clock.sleeps


@pytest.mark.parametrize("header,expected", [
    ("5", 5), ("0.25", 0.25), ("0", 0),
    ("-1", None), ("NaN", None), ("Infinity", None), ("1e309", None),
    ("invalid", None), ("", None), (None, None),
])
def test_retry_after_numeric_validation(header, expected):
    headers = {"Retry-After": header} if header is not None else {}
    error = LLMClient._http_error(PRIMARY, httpx.Response(429, headers=headers))
    assert error.retryable
    assert error.retry_after == expected


@pytest.mark.parametrize("offset,expected", [(5, 5), (-5, 0)])
def test_retry_after_http_date(monkeypatch, offset, expected):
    now = 1_700_000_000
    monkeypatch.setattr(llm_client.time, "time", lambda: now)
    error = LLMClient._http_error(PRIMARY, httpx.Response(
        503, headers={"Retry-After": formatdate(now + offset, usegmt=True)}))
    assert error.retry_after == expected
