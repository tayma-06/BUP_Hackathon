"""Shared fixtures for the GridWise offline test suite.

Every test here is hermetic: the LLM client is replaced with a deterministic
double that never dials out (no real Gemini / Groq / OpenAI calls), and the
runtime settings are fixed to a known provider configuration.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import ProviderConfig, Settings
from app.llm_client import LLMClient

PROVIDER = ProviderConfig("primary", "custom", "openai", "https://example.invalid/v1", "test-model", "")
SETTINGS = Settings((PROVIDER,), 0.5, 2, 2, 20)

DEFAULT_BATTERY = {
    "capacity_kwh": 220,
    "initial_energy_kwh": 110,
    "minimum_energy_kwh": 40,
    "max_charge_kwh_per_hour": 50,
    "max_discharge_kwh_per_hour": 50,
}

RESPONSE_KEYS = {
    "scenario_id", "directive_interpretation", "hourly_plan",
    "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary",
}
PLAN_KEYS = {"hour", "grid_kwh", "solar_used_kwh", "battery_action", "battery_kwh", "battery_energy_after_kwh"}
ENTRY_KEYS = {"note_index", "applies", "directive_type", "structured_adjustment", "explanation"}


def make_hours():
    hours = []
    for h in range(24):
        hours.append({
            "hour": h,
            "demand_kwh": 100 + (20 if 8 <= h <= 21 else 0),
            "solar_kwh": 40 if 7 <= h <= 17 else 0,
            "tariff_bdt_per_kwh": 12 if 17 <= h <= 21 else 6,
        })
    return hours


def make_body(notes=("Cafeteria menu changes tomorrow.",), battery=None, hours=None, **overrides):
    body = {
        "scenario_id": "TEST-001",
        "operator_notes": list(notes),
        "hours": make_hours() if hours is None else hours,
        "battery": dict(DEFAULT_BATTERY) if battery is None else battery,
    }
    body.update(overrides)
    return body


def canned_interpretation(text: str, index: int) -> dict:
    """Deterministic keyword-driven fake interpretation (never hits a real model)."""
    lowered = text.lower()
    if "solar" in lowered:
        return {"note_index": index, "applies": True, "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [12, 13], "factor": 0.25},
                "explanation": "Solar availability is reduced."}
    if "do not charge" in lowered:
        return {"note_index": index, "applies": True, "directive_type": "no_charge_window",
                "structured_adjustment": {"hours": [14, 15]}, "explanation": "No charging allowed."}
    if "do not discharge" in lowered:
        return {"note_index": index, "applies": True, "directive_type": "no_discharge_window",
                "structured_adjustment": {"hours": [16, 17]}, "explanation": "No discharging allowed."}
    if "reserve" in lowered:
        return {"note_index": index, "applies": True, "directive_type": "minimum_battery_reserve",
                "structured_adjustment": {"hours": [20, 21], "minimum_energy_kwh": 60.0},
                "explanation": "Battery reserve kept."}
    if "cap" in lowered or "max grid" in lowered:
        return {"note_index": index, "applies": True, "directive_type": "max_grid_window",
                "structured_adjustment": {"hours": [18, 19], "max_grid_kwh": 100.0},
                "explanation": "Grid import capped."}
    return {"note_index": index, "applies": False, "directive_type": "no_op",
            "structured_adjustment": None, "explanation": "No effect on the energy schedule."}


@pytest.fixture
def provider():
    return PROVIDER


@pytest.fixture
def settings():
    return SETTINGS


@pytest.fixture
def hours():
    return make_hours()


@pytest.fixture
def body_builder():
    return make_body


@pytest.fixture
def battery_builder():
    def build(**overrides):
        battery = dict(DEFAULT_BATTERY)
        battery.update(overrides)
        return battery
    return build


@pytest.fixture
def body(body_builder):
    return body_builder()


@pytest.fixture
def fake_llm(monkeypatch):
    """Deterministic, counting double for LLMClient.complete. No real network."""
    calls = []

    async def complete(self, provider, system, user, timeout):
        payload = json.loads(user)
        requested = payload["requested_note_indices"]
        notes = [n["text"] for n in payload["operator_notes"]]
        calls.append(list(requested))
        entries = [canned_interpretation(notes[i], i) for i in requested]
        return json.dumps({"interpretations": entries})

    monkeypatch.setattr(LLMClient, "complete", complete)
    return calls


@pytest.fixture
def client(monkeypatch, fake_llm):
    """FastAPI TestClient with fixed settings and a mocked LLM."""
    monkeypatch.setattr(main, "settings", SETTINGS)
    with TestClient(main.app, raise_server_exceptions=False) as api:
        yield api