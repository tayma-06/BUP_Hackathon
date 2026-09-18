"""End-to-end pipeline tests.

Full chain:  request JSON -> parse_scenario -> (mocked) LLM interpret ->
guardrails -> optimizer -> build_response -> replay validation. These tests run
through the real FastAPI app, so schema, headers and response shape are all
covered, with the LLM the only replaced component.
"""
import asyncio

import pytest

from app import main
from app.interpreter import NoteCache, interpret_notes
from app.llm_client import LLMClient
from app.optimizer import optimize
from app.schemas import parse_scenario
from app.validator import replay_plan

SOLAR_NOTE = "Solar output drops to 25% from noon to 2 PM."
NO_CHARGE_NOTE = "Do not charge between 2 PM and 4 PM."
NO_DISCHARGE_NOTE = "Do not discharge the battery from 4 PM to 6 PM."
RESERVE_NOTE = "Keep 60 kWh reserve from 8 PM to 10 PM."
GRID_CAP_NOTE = "Cap grid to 100 kWh from 6 PM to 8 PM."


def test_full_pipeline_through_api(client, body):
    response = client.post("/optimize-energy", json=body)
    assert response.status_code == 200
    assert response.headers["X-Self-Check"] == "pass"
    assert "llm:primary" in response.headers["X-Interpreter-Sources"]

    scenario = parse_scenario(body)
    data = response.json()
    assert not replay_plan(scenario, data["directive_interpretation"], data)
    assert set(data) == {
        "scenario_id", "directive_interpretation", "hourly_plan",
        "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary",
    }
    assert data["scenario_id"] == "TEST-001"


def test_offline_pipeline_with_real_interpreter(fake_llm, provider, settings, body_builder):
    body = body_builder(notes=(NO_CHARGE_NOTE, SOLAR_NOTE))
    scenario = parse_scenario(body)

    async def run():
        llm = LLMClient([provider])
        cache = NoteCache(8)
        try:
            entries, sources = await interpret_notes(scenario.notes, scenario.battery, llm, cache, settings)
        finally:
            await llm.aclose()
        return entries, sources

    entries, sources = asyncio.run(run())
    assert sources == ["llm:primary", "llm:primary"]
    assert [e["directive_type"] for e in entries] == ["no_charge_window", "solar_reduction"]

    result = optimize(scenario, entries)
    response = main.build_response(scenario, entries, result)
    assert not replay_plan(scenario, entries, response)


def test_solar_reduction_directive_is_applied(client, body_builder):
    body = body_builder(notes=(SOLAR_NOTE,))
    data = client.post("/optimize-energy", json=body).json()

    entry = data["directive_interpretation"][0]
    assert entry["directive_type"] == "solar_reduction"
    assert entry["structured_adjustment"]["factor"] == 0.25
    assert entry["structured_adjustment"]["hours"] == [12, 13]

    row12 = data["hourly_plan"][12]
    row13 = data["hourly_plan"][13]
    base_solar = 40.0  # fixture hours have 40 kWh solar from 07:00-17:59
    assert row12["solar_used_kwh"] == pytest.approx(base_solar * 0.25, abs=1e-4)
    assert row13["solar_used_kwh"] == pytest.approx(base_solar * 0.25, abs=1e-4)


def test_no_charge_window_directive_is_applied(client, body_builder):
    body = body_builder(notes=(NO_CHARGE_NOTE,))
    data = client.post("/optimize-energy", json=body).json()

    entry = data["directive_interpretation"][0]
    assert entry["directive_type"] == "no_charge_window"
    assert entry["structured_adjustment"]["hours"] == [14, 15]

    for hour in (14, 15):
        row = data["hourly_plan"][hour]
        assert row["battery_action"] != "charge"
        assert row["battery_kwh"] == 0

    scenario = parse_scenario(body)
    assert not replay_plan(scenario, data["directive_interpretation"], data)

    # A blocking constraint never lowers the cost below the unconstrained optimum.
    entries = data["directive_interpretation"]
    noops = [{"note_index": i, "applies": False, "directive_type": "no_op",
              "structured_adjustment": None, "explanation": "Unrelated."}
             for i in range(len(scenario.notes))]
    unconstrained = main.build_response(scenario, noops, optimize(scenario, noops))
    assert data["total_cost_bdt"] >= unconstrained["total_cost_bdt"]


def test_no_discharge_window_directive_is_applied(client, body_builder):
    body = body_builder(notes=(NO_DISCHARGE_NOTE,))
    data = client.post("/optimize-energy", json=body).json()

    entry = data["directive_interpretation"][0]
    assert entry["directive_type"] == "no_discharge_window"
    assert entry["structured_adjustment"]["hours"] == [16, 17]

    for hour in (16, 17):
        assert data["hourly_plan"][hour]["battery_action"] != "discharge"


def test_reserve_directive_is_applied(client, body_builder):
    body = body_builder(notes=(RESERVE_NOTE,))
    data = client.post("/optimize-energy", json=body).json()

    entry = data["directive_interpretation"][0]
    assert entry["directive_type"] == "minimum_battery_reserve"
    assert entry["structured_adjustment"]["minimum_energy_kwh"] == 60.0

    for hour in (20, 21):
        assert data["hourly_plan"][hour]["battery_energy_after_kwh"] >= 60.0 - 1e-9


def test_grid_cap_directive_is_applied(client, body_builder):
    body = body_builder(notes=(GRID_CAP_NOTE,))
    data = client.post("/optimize-energy", json=body).json()

    entry = data["directive_interpretation"][0]
    assert entry["directive_type"] == "max_grid_window"
    assert entry["structured_adjustment"]["max_grid_kwh"] == 100.0

    for hour in (18, 19):
        assert data["hourly_plan"][hour]["grid_kwh"] <= 100.0 + 1e-9


def test_noop_note_keeps_the_plan_valid(client, body_builder):
    body = body_builder(notes=("Cafeteria menu changes tomorrow.",))
    data = client.post("/optimize-energy", json=body).json()

    entry = data["directive_interpretation"][0]
    assert entry["directive_type"] == "no_op"
    assert entry["applies"] is False
    assert entry["structured_adjustment"] is None

    scenario = parse_scenario(body)
    assert not replay_plan(scenario, data["directive_interpretation"], data)