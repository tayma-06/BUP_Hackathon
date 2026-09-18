"""Directive validation tests.

Two layers are exercised:
  * app.guardrails.validate_entry  - raw model output -> safe directive (rejects)
  * app.validator.check_interpretation_schema / replay_plan - final response shape
Note: guardrails deduplicate raw hour lists, but the final response schema
validator rejects duplicate hours so they never reach the reported plan.
"""
import pytest

from app import main
from app.guardrails import ALLOWED_TYPES, GuardrailError, validate_entry
from app.optimizer import optimize
from app.schemas import Battery, parse_scenario
from app.validator import check_interpretation_schema, replay_plan

BATTERY = Battery(220, 110, 40, 50, 50)


def raw_entry(kind, hours=(10, 11), **values):
    if kind == "no_op":
        return {"directive_type": "no_op"}
    return {"directive_type": kind, "hours": list(hours), **values}


def final_entry(kind, **adjustment):
    """Final response-schema shape: applies + structured_adjustment."""
    if kind == "no_op":
        return {"note_index": 0, "applies": False, "directive_type": "no_op",
                "structured_adjustment": None, "explanation": "None."}
    adjustment = {"hours": [10, 11], **adjustment}
    return {"note_index": 0, "applies": True, "directive_type": kind,
            "structured_adjustment": adjustment, "explanation": "None."}


@pytest.mark.parametrize("kind,values,expected", [
    ("solar_reduction", {"factor": 0.3}, {"hours": [10, 11], "factor": 0.3}),
    ("minimum_battery_reserve", {"minimum_energy_kwh": 60.0}, {"hours": [10, 11], "minimum_energy_kwh": 60}),
    ("no_charge_window", {}, {"hours": [10, 11]}),
    ("no_discharge_window", {}, {"hours": [10, 11]}),
    ("max_grid_window", {"max_grid_kwh": 120.0}, {"hours": [10, 11], "max_grid_kwh": 120}),
    ("no_op", {}, None),
])
def test_all_directive_types_are_accepted(kind, values, expected):
    assert kind in ALLOWED_TYPES
    entry = validate_entry(raw_entry(kind, **values), BATTERY)
    assert entry["applies"] is (kind != "no_op")
    assert entry["structured_adjustment"] == expected
    assert check_interpretation_schema(1, [final_entry(kind, **values)], BATTERY.capacity) == []


def test_invalid_directive_type_rejected():
    with pytest.raises(GuardrailError):
        validate_entry({"directive_type": "increase_power", "hours": [1]}, BATTERY)
    errors = check_interpretation_schema(1, [final_entry("increase_power")], BATTERY.capacity)
    assert any("unsupported directive_type" in e for e in errors)


def test_hour_out_of_range_rejected():
    with pytest.raises(GuardrailError):
        validate_entry(raw_entry("no_charge_window", hours=(25,)), BATTERY)
    errors = check_interpretation_schema(1, [final_entry("no_charge_window", hours=[25])], BATTERY.capacity)
    assert any("hours must be unique ascending integers" in e for e in errors)


def test_duplicate_hours_are_deduplicated_then_rejected_at_schema_layer():
    validated = validate_entry(raw_entry("no_charge_window", hours=(12, 12)), BATTERY)
    assert validated["structured_adjustment"]["hours"] == [12]
    errors = check_interpretation_schema(1, [final_entry("no_charge_window", hours=[12, 12])], BATTERY.capacity)
    assert errors  # duplicates must never surface in the final response


def test_factor_above_one_rejected():
    with pytest.raises(GuardrailError):
        validate_entry(raw_entry("solar_reduction", factor=2.0), BATTERY)
    errors = check_interpretation_schema(1, [final_entry("solar_reduction", factor=2.0)], BATTERY.capacity)
    assert any("factor must be <= 1" in e for e in errors)


def test_negative_factor_rejected():
    with pytest.raises(GuardrailError):
        validate_entry(raw_entry("solar_reduction", factor=-0.1), BATTERY)


def test_applies_flag_mismatch_rejected():
    with pytest.raises(GuardrailError):
        validate_entry({**raw_entry("no_charge_window"), "applies": False}, BATTERY)


def test_reserve_above_capacity_rejected():
    with pytest.raises(GuardrailError):
        validate_entry(raw_entry("minimum_battery_reserve", minimum_energy_kwh=221.0), BATTERY)
    errors = check_interpretation_schema(
        1, [final_entry("minimum_battery_reserve", minimum_energy_kwh=221.0)], BATTERY.capacity)
    assert any("reserve exceeds battery capacity" in e for e in errors)


def test_noop_with_time_window_rejected():
    with pytest.raises(GuardrailError):
        validate_entry({"directive_type": "no_op", "hours": [1]}, BATTERY)


def test_valid_plan_passes_replay():
    body = {
        "scenario_id": "T",
        "operator_notes": ["solar reduced"],
        "hours": [{"hour": h, "demand_kwh": 100, "solar_kwh": 40 if 7 <= h <= 17 else 0,
                   "tariff_bdt_per_kwh": 6} for h in range(24)],
        "battery": {"capacity_kwh": 220, "initial_energy_kwh": 110, "minimum_energy_kwh": 40,
                    "max_charge_kwh_per_hour": 50, "max_discharge_kwh_per_hour": 50},
    }
    scenario = parse_scenario(body)
    entries = [final_entry("solar_reduction", factor=0.5)]
    response = main.build_response(scenario, entries, optimize(scenario, entries))
    assert replay_plan(scenario, entries, response) == []


def test_replay_rejects_corrupted_plan():
    body = {
        "scenario_id": "T",
        "operator_notes": ["solar reduced"],
        "hours": [{"hour": h, "demand_kwh": 100, "solar_kwh": 40 if 7 <= h <= 17 else 0,
                   "tariff_bdt_per_kwh": 6} for h in range(24)],
        "battery": {"capacity_kwh": 220, "initial_energy_kwh": 110, "minimum_energy_kwh": 40,
                    "max_charge_kwh_per_hour": 50, "max_discharge_kwh_per_hour": 50},
    }
    scenario = parse_scenario(body)
    entries = [final_entry("solar_reduction", factor=0.5)]
    response = main.build_response(scenario, entries, optimize(scenario, entries))
    response["total_cost_bdt"] += 10
    assert replay_plan(scenario, entries, response)


def test_replay_rejects_noninteger_hour():
    body = {
        "scenario_id": "T",
        "operator_notes": ["solar reduced"],
        "hours": [{"hour": h, "demand_kwh": 100, "solar_kwh": 40 if 7 <= h <= 17 else 0,
                   "tariff_bdt_per_kwh": 6} for h in range(24)],
        "battery": {"capacity_kwh": 220, "initial_energy_kwh": 110, "minimum_energy_kwh": 40,
                    "max_charge_kwh_per_hour": 50, "max_discharge_kwh_per_hour": 50},
    }
    scenario = parse_scenario(body)
    entries = [final_entry("solar_reduction", factor=0.5)]
    response = main.build_response(scenario, entries, optimize(scenario, entries))
    response["hourly_plan"][0]["hour"] = False
    assert replay_plan(scenario, entries, response)