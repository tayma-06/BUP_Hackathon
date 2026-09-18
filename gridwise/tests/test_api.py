"""POST /optimize-energy endpoint contract tests.

Structurally invalid requests are answered 400 (malformed JSON / missing or
wrong-typed fields / wrong array lengths), well-formed but physically
impossible values are answered 422. This mirrors app/schemas.parse_scenario.
"""
from app.schemas import parse_scenario
from app.validator import replay_plan
from conftest import ENTRY_KEYS, PLAN_KEYS, RESPONSE_KEYS


def test_valid_request_returns_exact_schema(client, body):
    response = client.post("/optimize-energy", json=body)
    assert response.status_code == 200
    assert response.headers["X-Self-Check"] == "pass"
    assert response.headers["X-Interpreter-Sources"].startswith("llm:")

    data = response.json()
    assert set(data) == RESPONSE_KEYS
    assert data["scenario_id"] == body["scenario_id"]

    plan = data["hourly_plan"]
    assert isinstance(plan, list) and len(plan) == 24
    assert [row["hour"] for row in plan] == list(range(24))
    assert all(set(row) == PLAN_KEYS for row in plan)

    interpretations = data["directive_interpretation"]
    assert len(interpretations) == len(body["operator_notes"])
    assert all(set(entry) == ENTRY_KEYS for entry in interpretations)

    scenario = parse_scenario(body)
    assert not replay_plan(scenario, interpretations, data)
    assert data["total_cost_bdt"] > 0
    assert isinstance(data["plan_summary"], str) and data["plan_summary"]


def test_response_headers_mark_validated_plan(client, body):
    response = client.post("/optimize-energy", json=body)
    assert response.headers["X-Plan-Status"] == "optimal"
    assert response.headers["X-Self-Check"] == "pass"


def test_two_valid_notes_are_both_interpreted(client, body_builder):
    body = body_builder(notes=("Do not charge between 2 PM and 4 PM.",
                               "Solar output drops to 25% from noon to 2 PM."))
    response = client.post("/optimize-energy", json=body)
    assert response.status_code == 200
    entries = response.json()["directive_interpretation"]
    assert [e["note_index"] for e in entries] == [0, 1]
    assert [e["directive_type"] for e in entries] == ["no_charge_window", "solar_reduction"]


def test_missing_scenario_id_is_structural_error(client, body_builder):
    body = body_builder()
    del body["scenario_id"]
    response = client.post("/optimize-energy", json=body)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_empty_operator_notes_are_rejected_by_contract(client, body_builder):
    # The production contract requires exactly 1-3 non-empty notes: an empty
    # array is a structural error, not an accepted no-op day.
    body = body_builder(notes=())
    response = client.post("/optimize-energy", json=body)
    assert response.status_code == 400
    assert "operator_notes" in response.json()["detail"]


def test_more_than_three_notes_rejected(client, body_builder):
    body = body_builder(notes=("a", "b", "c", "d"))
    assert client.post("/optimize-energy", json=body).status_code == 400


def test_invalid_hour_count_rejected(client, body_builder):
    body = body_builder()
    body["hours"] = body["hours"][:23]
    response = client.post("/optimize-energy", json=body)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_duplicate_hour_rejected(client, body_builder):
    body = body_builder()
    body["hours"][1]["hour"] = 0
    assert client.post("/optimize-energy", json=body).status_code == 400


def test_malformed_json_returns_400(client):
    assert client.post("/optimize-energy", content="{").status_code == 400
    assert client.post("/optimize-energy", content="not JSON").status_code == 400
    assert client.post("/optimize-energy", content="null").status_code == 400


def test_physically_impossible_values_return_422(client, body_builder, battery_builder):
    body = body_builder(battery=battery_builder(initial_energy_kwh=-1))
    response = client.post("/optimize-energy", json=body)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_values"


def test_negative_demand_returns_422(client, body_builder):
    body = body_builder()
    body["hours"][7]["demand_kwh"] = -5
    assert client.post("/optimize-energy", json=body).status_code == 422