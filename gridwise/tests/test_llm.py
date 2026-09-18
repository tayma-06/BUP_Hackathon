"""LLM client + interpreter tests with fully mocked providers (no live calls).

The application deliberately refuses to invent a no_op when the model fails:
exhausted retries raise InterpretationError (surfaced as HTTP 500), they never
become a fake no-op. The safety contract is: a bad model output must fail
closed, not silently pass through.
"""
import asyncio
import json

import httpx
import pytest

from app.config import ProviderConfig, Settings
from app.guardrails import GuardrailError, validate_entry
from app.interpreter import InterpretationError, NoteCache, interpret_notes, parse_llm_json
from app.llm_client import LLMClient, LLMError
from app.schemas import Battery

PROVIDER = ProviderConfig("primary", "custom", "openai", "https://example.invalid/v1", "test-model", "")
SETTINGS = Settings((PROVIDER,), 0.5, 2, 2, 20)
BATTERY = Battery(220, 110, 40, 50, 50)


def noop(index=0):
    return {"note_index": index, "applies": False, "directive_type": "no_op",
            "structured_adjustment": None, "explanation": "Unrelated to energy."}


class FakeLLM:
    """Configurable fake: providers + a scripted reply list (last one repeats)."""

    def __init__(self, replies, provider=PROVIDER):
        self.providers = [provider]
        self.replies = list(replies)
        self.calls = []

    async def complete(self, provider, system, user, timeout):
        attempt = len(self.calls)
        self.calls.append(json.loads(user))
        return self.replies[min(attempt, len(self.replies) - 1)]


# --------------------------------------------------------------------- parsing

def test_valid_gemini_response_is_parsed_correctly():
    reply_text = json.dumps({
        "interpretations": [{
            "note_index": 0,
            "applies": True,
            "directive_type": "solar_reduction",
            "windows": [[12, 14]],
            "factor": 0.25,
            "explanation": "Panel cleaning today.",
        }]
    })

    def handler(request):
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"choices": [{"message": {"content": reply_text}}]})

    async def run():
        provider = ProviderConfig("primary", "gemini", "openai",
                                  "https://generativelanguage.googleapis.com/v1beta/openai",
                                  "gemini-2.0-flash", "test-key")
        client = LLMClient([provider])
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            text = await client.complete(provider, "system", "user", 1)
        finally:
            await client.aclose()

    asyncio.run(run())
    mapping = parse_llm_json(reply_text, 1)
    assert mapping[0]["directive_type"] == "solar_reduction"
    entry = validate_entry(mapping[0], BATTERY)
    assert entry["applies"] is True
    assert entry["structured_adjustment"] == {"hours": [12, 13], "factor": 0.25}


def test_invalid_json_reply_is_rejected():
    with pytest.raises(ValueError):
        parse_llm_json("Solar output reduced tomorrow", 1)


def test_reply_with_duplicate_note_index_is_rejected():
    reply = json.dumps({"interpretations": [noop(0), noop(0)]})
    with pytest.raises(ValueError):
        parse_llm_json(reply, 2)


def test_reply_with_missing_note_index_is_rejected():
    reply = json.dumps({"interpretations": [noop(5)]})
    with pytest.raises(ValueError):
        parse_llm_json(reply, 1)


def test_fenced_markdown_json_is_stripped():
    reply = "```json\n" + json.dumps({"interpretations": [noop(0)]}) + "\n```"
    assert parse_llm_json(reply, 1)[0]["directive_type"] == "no_op"


# ------------------------------------------------------------- interpretation

def test_invalid_json_triggers_retry_then_success():
    good = json.dumps({"interpretations": [noop(0), noop(1)]})
    fake = FakeLLM(["Solar output reduced tomorrow", good])

    async def run():
        result, sources = await interpret_notes(["a", "b"], BATTERY, fake, NoteCache(0), SETTINGS)
        return result, sources

    result, sources = asyncio.run(run())
    assert len(fake.calls) == 2
    assert sources == ["llm:primary", "llm:primary"]
    assert [r["directive_type"] for r in result] == ["no_op", "no_op"]


def test_exhausted_retries_raise_never_noop():
    fake = FakeLLM(["not json at all"])

    async def run():
        with pytest.raises(InterpretationError):
            await interpret_notes(["one"], BATTERY, fake, NoteCache(0), SETTINGS)

    asyncio.run(run())
    assert len(fake.calls) == SETTINGS.llm_max_tries
    cache = NoteCache(4)
    assert cache.get(["one"], BATTERY) is None  # nothing poisoned the cache


def test_persistent_unknown_directive_raises_never_noop():
    bad = json.dumps({"interpretations": [{
        "note_index": 0, "directive_type": "random_command", "windows": [[0, 1]], "explanation": "",
    }]})
    fake = FakeLLM([bad])

    async def run():
        with pytest.raises(InterpretationError):
            await interpret_notes(["one"], BATTERY, fake, NoteCache(0), SETTINGS)

    asyncio.run(run())
    assert len(fake.calls) == SETTINGS.llm_max_tries


def test_unknown_directive_is_rejected_directly():
    with pytest.raises(GuardrailError):
        validate_entry({"directive_type": "random_command", "hours": [1]}, BATTERY)


def test_bad_factor_is_rejected_directly():
    with pytest.raises(GuardrailError):
        validate_entry({"directive_type": "solar_reduction", "hours": [12], "factor": 2.0}, BATTERY)


def test_backup_provider_is_used_after_primary_fails(mocker):
    async def run():
        backup = ProviderConfig("backup", "custom", "openai", "https://backup.invalid/v1", "m2", "")
        calls = []

        class Fake:
            providers = [PROVIDER, backup]

            async def complete(self, provider, system, user, timeout):
                calls.append(provider.label)
                if provider.label == "primary":
                    raise LLMError("quota", retryable=True)
                return json.dumps({"interpretations": [noop()]})

        result, sources = await interpret_notes(["unrelated"], BATTERY, Fake(), NoteCache(0), SETTINGS)
        assert calls == ["primary", "backup"]
        assert sources == ["llm:backup"]
        assert result[0]["directive_type"] == "no_op"

    asyncio.run(run())