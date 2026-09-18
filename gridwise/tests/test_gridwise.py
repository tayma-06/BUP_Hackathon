"""Offline checks. Mocked LLM replies test plumbing, NOT live language accuracy."""
import asyncio
import copy
import json
import random
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import ProviderConfig, Settings
from app.guardrails import GuardrailError, validate_entry
from app.interpreter import InterpretationError, NoteCache, interpret_notes, parse_llm_json
from app.llm_client import LLMClient, LLMError
from app.optimizer import InfeasiblePlanError, optimize
from app.schemas import Battery, HourData, RequestError, Scenario, parse_scenario
from app.validator import check_interpretation_schema, replay_plan

CASES = json.loads((Path(__file__).parents[1] / "data/public_samples.json").read_text())["cases"]
PROVIDER = ProviderConfig("primary", "custom", "openai", "https://example.invalid/v1", "test-model", "")
SETTINGS = Settings((PROVIDER,), 0.5, 2, 2, 20)


def noop(i=0):
    return {"note_index": i, "applies": False, "directive_type": "no_op",
            "structured_adjustment": None, "explanation": "Unrelated to energy."}


def directive(kind, hours, i=0, **values):
    return {"note_index": i, "applies": True, "directive_type": kind,
            "structured_adjustment": {"hours": hours, **values}, "explanation": "Test constraint."}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_public_optimal_cost_and_replay(case):
    scenario = parse_scenario(case["input"])
    expected = case["expected_output"]
    entries = expected["directive_interpretation"]
    response = main.build_response(scenario, entries, optimize(scenario, entries))
    assert not replay_plan(scenario, entries, response, tol=1e-6)
    assert response["total_cost_bdt"] == pytest.approx(expected["total_cost_bdt"], abs=0.001)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "settings", SETTINGS)
    async def complete(self, provider, system, user, timeout):
        request = json.loads(user)
        notes = [x["text"] for x in request["operator_notes"]]
        case = next(c for c in CASES if c["input"]["operator_notes"] == notes)
        requested = request["requested_note_indices"]
        return json.dumps({"interpretations": [e for e in case["expected_output"]["directive_interpretation"]
                                               if e["note_index"] in requested]})
    monkeypatch.setattr(LLMClient, "complete", complete)
    with TestClient(main.app, raise_server_exceptions=False) as api:
        yield api


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_full_api_with_mocked_llm(client, case):
    assert client.get("/health").json() == {"status": "ok"}
    response = client.post("/optimize-energy", json=case["input"])
    assert response.status_code == 200, response.text
    assert response.headers["X-Self-Check"] == "pass"
    assert "llm:primary" in response.headers["X-Interpreter-Sources"]
    data = response.json()
    assert not replay_plan(parse_scenario(case["input"]), case["expected_output"]["directive_interpretation"], data)
    assert data["total_cost_bdt"] == pytest.approx(case["expected_output"]["total_cost_bdt"], abs=0.001)
    again = client.post("/optimize-energy", json=case["input"])
    assert again.status_code == 200
    assert "cache:llm" in again.headers["X-Interpreter-Sources"]
    assert again.json() == data


@pytest.mark.parametrize("body", ["{", "null", "[]", '{"value":NaN}', '{"value":Infinity}', "not JSON"])
def test_malformed_input(client, body):
    assert client.post("/optimize-energy", content=body).status_code == 400


@pytest.mark.parametrize("kind", ["duplicate_hour", "bool_hour", "string_number", "empty_note", "missing_battery", "giant_number"])
def test_invalid_structure(client, kind):
    body = copy.deepcopy(CASES[0]["input"])
    if kind == "duplicate_hour": body["hours"][1]["hour"] = 0
    if kind == "bool_hour": body["hours"][0]["hour"] = False
    if kind == "string_number": body["hours"][0]["demand_kwh"] = "20"
    if kind == "empty_note": body["operator_notes"] = [" "]
    if kind == "missing_battery": del body["battery"]
    if kind == "giant_number": body["hours"][0]["demand_kwh"] = 10**400
    assert client.post("/optimize-energy", json=body).status_code == 400


def test_semantic_errors(client):
    body = copy.deepcopy(CASES[0]["input"])
    body["battery"]["initial_energy_kwh"] = -1
    assert client.post("/optimize-energy", json=body).status_code == 422


def test_no_provider_not_ready(monkeypatch):
    monkeypatch.setattr(main, "settings", replace(SETTINGS, providers=()))
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 500
        response = client.post("/optimize-energy", json=CASES[0]["input"])
        assert response.status_code == 500
        assert response.json()["error"] == "llm_unavailable"
        assert "hourly_plan" not in response.json()


def test_provider_outage_never_becomes_noop(client, monkeypatch):
    async def fail(*args, **kwargs):
        raise LLMError("outage", retryable=True)
    monkeypatch.setattr(LLMClient, "complete", fail)
    response = client.post("/optimize-energy", json=CASES[0]["input"])
    assert response.status_code == 500
    assert "hourly_plan" not in response.json()


def test_invalid_model_json_never_becomes_noop(client, monkeypatch):
    async def invalid(*args, **kwargs): return "not JSON"
    monkeypatch.setattr(LLMClient, "complete", invalid)
    assert client.post("/optimize-energy", json=CASES[0]["input"]).status_code == 500


def test_replay_failure_blocks_success(client, monkeypatch):
    original = main.build_response
    def corrupt(*args):
        response = original(*args)
        response["hourly_plan"][0]["grid_kwh"] += 1
        return response
    monkeypatch.setattr(main, "build_response", corrupt)
    response = client.post("/optimize-energy", json=CASES[0]["input"])
    assert response.status_code == 500
    assert response.json()["error"] == "plan_validation_failed"


def test_infeasible_constraints_are_not_dropped(client):
    body = copy.deepcopy(CASES[4]["input"])
    body["battery"]["capacity_kwh"] = 0
    body["battery"]["initial_energy_kwh"] = 0
    body["battery"]["minimum_energy_kwh"] = 0
    response = client.post("/optimize-energy", json=body)
    assert response.status_code == 422
    assert response.json()["error"] == "infeasible_directives"


@pytest.mark.parametrize("indices", [[0, 0], [1, 2], [False, 1], [0.0, 1], [0], [0, 3]])
def test_bad_note_mapping_rejected(indices):
    with pytest.raises(ValueError):
        parse_llm_json(json.dumps({"interpretations": [noop(i) for i in indices]}), 2)


def test_guardrail_percent_precision_and_hours():
    b = Battery(200, 100, 0, 50, 50)
    entry = validate_entry({"directive_type": "minimum_battery_reserve", "windows": [[22, 2]],
                            "minimum_energy_percent": 0.5}, b)
    assert entry["structured_adjustment"] == {"hours": [0, 1, 22, 23], "minimum_energy_kwh": 1}
    solar = validate_entry({"directive_type": "solar_reduction", "hours": [13, 12, 13], "factor": 1/3}, b)
    assert solar["structured_adjustment"]["hours"] == [12, 13]
    assert solar["structured_adjustment"]["factor"] == pytest.approx(1/3, abs=1e-12)


@pytest.mark.parametrize("raw", [
    {"directive_type": "solar_reduction", "hours": [12], "factor": 1.1},
    {"directive_type": "solar_reduction", "hours": [12], "factor": float("nan")},
    {"directive_type": "solar_reduction", "hours": [12], "factor": 0.2, "applies": False},
    {"directive_type": "no_op", "structured_adjustment": {"hours": [1]}},
    {"directive_type": "no_charge_window", "hours": [True]},
    {"directive_type": "no_charge_window", "windows": [[1, 2]], "hours": [3]},
    {"directive_type": "no_charge_window", "hours": [1], "demand_kwh": 0},
    {"directive_type": "minimum_battery_reserve", "hours": [1], "minimum_energy_kwh": 201},
    {"directive_type": "max_grid_window", "hours": [1], "max_grid_kwh": 10**400},
    {"directive_type": "invented", "hours": [1]},
])
def test_guardrails_reject_bad_output(raw):
    with pytest.raises(GuardrailError):
        validate_entry(raw, Battery(200, 100, 0, 50, 50))


def test_cache_uses_context_and_battery():
    cache = NoteCache(2)
    b = Battery(200, 100, 0, 50, 50)
    cache.put(["half full"], b, [noop()])
    assert cache.get(["half full"], b)
    assert cache.get(["half full"], replace(b, capacity=400)) is None
    assert cache.get(["half full", "other"], b) is None
    assert cache.get(["half full "], b) is None


def test_backup_before_primary_retry():
    async def run():
        backup = replace(PROVIDER, label="backup")
        calls = []
        class Fake:
            providers = [PROVIDER, backup]
            async def complete(self, p, system, user, timeout):
                calls.append(p.label)
                if p.label == "primary": raise LLMError("quota", True)
                return json.dumps({"interpretations": [noop()]})
        result, sources = await interpret_notes(["unrelated"], Battery(0, 0, 0, 0, 0), Fake(), NoteCache(0), SETTINGS)
        assert calls == ["primary", "backup"]
        assert sources == ["llm:backup"]
        assert result == [noop()]
    asyncio.run(run())


def test_partial_retry_preserves_original_note_indices():
    async def run():
        calls = []
        class Fake:
            providers = [PROVIDER]
            async def complete(self, p, system, user, timeout):
                requested = json.loads(user)["requested_note_indices"]
                calls.append(requested)
                if len(calls) == 1:
                    return json.dumps({"interpretations": [noop(0), {"note_index": 1, "directive_type": "bad"}]})
                return json.dumps({"interpretations": [noop(1)]})
        result, sources = await interpret_notes(["one", "two"], Battery(0,0,0,0,0), Fake(), NoteCache(0), SETTINGS)
        assert calls == [[0, 1], [1]]
        assert [r["note_index"] for r in result] == [0, 1]
    asyncio.run(run())


def test_hanging_provider_is_bounded():
    async def run():
        class Fake:
            providers = [PROVIDER]
            async def complete(self, *args, **kwargs):
                await asyncio.sleep(10)
        settings = replace(SETTINGS, llm_timeout_s=0.02, interpret_budget_s=0.06)
        with pytest.raises(InterpretationError):
            await asyncio.wait_for(interpret_notes(["one"], Battery(0,0,0,0,0), Fake(), NoteCache(0), settings), 0.5)
    asyncio.run(run())


@pytest.mark.parametrize("style", ["openai", "anthropic"])
def test_actual_http_wire_format(style):
    async def run():
        provider = replace(PROVIDER, style=style, api_key="test-key")
        def handler(request):
            body = json.loads(request.content)
            assert body["model"] == "test-model"
            if style == "anthropic":
                assert request.url.path.endswith("/messages")
                assert request.headers["x-api-key"] == "test-key"
                assert body["system"] == "system"
                return httpx.Response(200, json={"content": [{"type": "text", "text": "hello"}]})
            assert request.headers["authorization"] == "Bearer test-key"
            return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})
        client = LLMClient([provider])
        await client._http.aclose()
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            assert await client.complete(provider, "system", "user", 1) == "hello"
        finally:
            await client.aclose()
    asyncio.run(run())


def test_provider_errors_do_not_leak_body():
    error = LLMClient._http_error(PROVIDER, httpx.Response(500, text="private-token-and-prompt"))
    assert "private-token" not in str(error)


def test_zero_capacity_and_zero_tariffs():
    s = Scenario("zero", ["unrelated"], [HourData(h, 1.25, 2.5, 0) for h in range(24)], Battery(0, 0, 0, 0, 0))
    response = main.build_response(s, [noop()], optimize(s, [noop()]))
    assert response["total_cost_bdt"] == 0
    assert not replay_plan(s, [noop()], response, tol=1e-8)


def test_tiny_tariffs_are_not_distorted_by_cycle_penalty():
    hours = [HourData(h, 0, 0, 0) for h in range(24)]
    hours[1] = HourData(1, 1, 0, 1e-7)
    s = Scenario("tiny", ["unrelated"], hours, Battery(1, 0, 0, 1, 1))
    r = main.build_response(s, [noop()], optimize(s, [noop()]))
    assert r["total_cost_bdt"] == 0


@pytest.mark.parametrize("seed", range(20))
def test_optimizer_against_independent_discrete_oracle(seed):
    """Integral network-flow cases have an integral optimum; enumerate all SOC states."""
    rng = random.Random(seed)
    capacity, initial, minimum, rate = 6, 3, 1, 2
    hours = [HourData(h, rng.randrange(2, 8), rng.randrange(0, 6), rng.randrange(0, 9)) for h in range(24)]
    entries = [directive("no_charge_window", [3, 4]),
               directive("minimum_battery_reserve", [18, 19], i=1, minimum_energy_kwh=2),
               directive("max_grid_window", [12, 13], i=2, max_grid_kwh=6)]
    s = Scenario("random", ["charge", "reserve", "cap"], hours, Battery(capacity, initial, minimum, rate, rate))
    states = {initial: 0}
    for h, row in enumerate(hours):
        next_states = {}
        floor = 2 if h in [18, 19] else minimum
        for before, cost in states.items():
            for after in range(floor, capacity+1):
                delta = after-before
                if abs(delta) > rate or (h in [3, 4] and delta > 0): continue
                if row.demand+delta < 0: continue
                grid = max(0, row.demand + delta - row.solar)
                if h in [12, 13] and grid > 6: continue
                value = cost + grid*row.tariff
                next_states[after] = min(next_states.get(after, float("inf")), value)
        states = next_states
    if initial not in states:
        with pytest.raises(InfeasiblePlanError): optimize(s, entries)
    else:
        r = main.build_response(s, entries, optimize(s, entries))
        assert not replay_plan(s, entries, r, tol=1e-6)
        assert r["total_cost_bdt"] == pytest.approx(states[initial], abs=1e-6)


def test_replay_rejects_noninteger_hour():
    s = parse_scenario(CASES[0]["input"])
    d = CASES[0]["expected_output"]["directive_interpretation"]
    r = main.build_response(s, d, optimize(s, d))
    r["hourly_plan"][0]["hour"] = False
    assert replay_plan(s, d, r)
