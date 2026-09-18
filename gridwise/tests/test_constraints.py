"""Per-hour physical invariant checks over optimized plans (independent oracle).

For every hour of every plan produced by the optimizer we verify, directly:
  * energy balance:  grid + solar_used + discharge = demand + charge
  * battery state transition: energy_after = energy_before + charge - discharge
  * capacity upper bound and minimum reserve lower bound
  * charge / discharge rate limits (including zeroed no_charge/no_discharge hours)
  * solar cap (after solar_reduction) and grid cap (after max_grid_window)
  * end-of-day neutrality: final battery energy == initial battery energy

The expected per-hour limits are recomputed here from the directives instead of
re-using the optimizer's build_limits, so this is a genuinely independent check.
"""
import json
from pathlib import Path

import pytest

from app.optimizer import optimize
from app.schemas import Battery, HourData, Scenario, parse_scenario

CASES_PATH = Path(__file__).resolve().parents[1] / "data" / "public_samples.json"

TOL = 1e-3


def noop_entry():
    return {"note_index": 0, "applies": False, "directive_type": "no_op",
            "structured_adjustment": None, "explanation": "None."}


def expected_limits(scenario, entries):
    """Independent derivation of per-hour legal caps from the directives."""
    battery = scenario.battery
    solar = [h.solar for h in scenario.hours]
    grid = [None] * 24
    charge = [battery.max_charge] * 24
    discharge = [battery.max_discharge] * 24
    floor = [battery.minimum] * 24
    for entry in entries:
        if not entry["applies"]:
            continue
        adjustment = entry["structured_adjustment"]
        kind = entry["directive_type"]
        for h in adjustment["hours"]:
            if kind == "solar_reduction":
                solar[h] *= adjustment["factor"]
            elif kind == "minimum_battery_reserve":
                floor[h] = max(floor[h], adjustment["minimum_energy_kwh"])
            elif kind == "no_charge_window":
                charge[h] = 0.0
            elif kind == "no_discharge_window":
                discharge[h] = 0.0
            elif kind == "max_grid_window":
                grid[h] = adjustment["max_grid_kwh"] if grid[h] is None else min(grid[h], adjustment["max_grid_kwh"])
    return solar, grid, charge, discharge, floor


def assert_plan_obeys_physics(scenario, entries, plan):
    solar, grid_cap, charge_cap, discharge_cap, floor = expected_limits(scenario, entries)
    battery = scenario.battery
    energy = battery.initial

    for row, hour in zip(plan, scenario.hours):
        h = row["hour"]
        action, amount = row["battery_action"], row["battery_kwh"]
        charge = amount if action == "charge" else 0.0
        discharge = amount if action == "discharge" else 0.0

        # --- energy balance: grid + solar_used + discharge = demand + charge ---
        balance = row["grid_kwh"] + row["solar_used_kwh"] + discharge - hour.demand - charge
        assert abs(balance) < TOL, f"hour {h}: energy balance off by {balance}"

        # --- battery state transition ---
        energy += charge - discharge
        assert abs(row["battery_energy_after_kwh"] - energy) < TOL, f"hour {h}: stored energy mismatch"

        # --- capacity and reserve bounds ---
        assert row["battery_energy_after_kwh"] <= battery.capacity + TOL, f"hour {h}: over capacity"
        assert row["battery_energy_after_kwh"] >= floor[h] - TOL, f"hour {h}: below reserve floor"

        # --- rate limits ---
        assert 0.0 <= charge <= charge_cap[h] + TOL, f"hour {h}: charge limit breached"
        assert 0.0 <= discharge <= discharge_cap[h] + TOL, f"hour {h}: discharge limit breached"
        if action == "idle":
            assert amount == 0, f"hour {h}: idle with battery_kwh={amount}"
        else:
            assert amount > 0, f"hour {h}: {action} with battery_kwh={amount}"

        # --- solar and grid caps ---
        assert row["solar_used_kwh"] <= solar[h] + TOL, f"hour {h}: solar cap breached"
        assert row["solar_used_kwh"] >= 0
        if grid_cap[h] is not None:
            assert row["grid_kwh"] <= grid_cap[h] + TOL, f"hour {h}: grid cap breached"
        assert row["grid_kwh"] >= 0

    # --- end-of-day neutrality ---
    assert abs(energy - battery.initial) < TOL, "end-of-day battery not neutral"


def test_default_scenario_invariants(body_builder):
    scenario = parse_scenario(body_builder())
    result = optimize(scenario, [noop_entry()])
    assert_plan_obeys_physics(scenario, [noop_entry()], result.plan)


def test_directed_scenario_invariants():
    hours = [HourData(h, 100.0, 40.0 if 7 <= h <= 17 else 0.0, 10.0 if 17 <= h <= 21 else 5.0)
             for h in range(24)]
    battery = Battery(220, 110, 40, 50, 50)
    entries = [
        {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
         "structured_adjustment": {"hours": [12, 13], "factor": 0.5}, "explanation": ""},
        {"note_index": 1, "applies": True, "directive_type": "no_charge_window",
         "structured_adjustment": {"hours": [14, 15]}, "explanation": ""},
        {"note_index": 2, "applies": True, "directive_type": "no_discharge_window",
         "structured_adjustment": {"hours": [16, 17]}, "explanation": ""},
        {"note_index": 3, "applies": True, "directive_type": "minimum_battery_reserve",
         "structured_adjustment": {"hours": [20, 21], "minimum_energy_kwh": 60.0}, "explanation": ""},
        {"note_index": 4, "applies": True, "directive_type": "max_grid_window",
         "structured_adjustment": {"hours": [18, 19], "max_grid_kwh": 100.0}, "explanation": ""},
    ]
    scenario = Scenario("DIRECTED", ["solar", "no-charge", "no-discharge", "reserve", "cap"], hours, battery)
    result = optimize(scenario, entries)
    assert_plan_obeys_physics(scenario, entries, result.plan)


def test_tariff_arbitrage_scenario_invariants():
    hours = [HourData(h, 30.0, 0.0, 100.0 if h == 10 else 1.0) for h in range(24)]
    scenario = Scenario("ARB", ["note"], hours, Battery(100, 50, 0, 20, 20))
    result = optimize(scenario, [noop_entry()])
    assert_plan_obeys_physics(scenario, [noop_entry()], result.plan)


def test_zero_capacity_scenario_invariants():
    hours = [HourData(h, 50.0, 0.0, 1.0) for h in range(24)]
    scenario = Scenario("ZERO", ["note"], hours, Battery(0, 0, 0, 0, 0))
    result = optimize(scenario, [noop_entry()])
    assert_plan_obeys_physics(scenario, [noop_entry()], result.plan)


@pytest.mark.parametrize("index", range(10), ids=lambda i: f"SAMPLE-{i + 1:02d}")
def test_organizer_public_samples_invariants(index):
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    case = cases[index]
    scenario = parse_scenario(case["input"])
    entries = case["expected_output"]["directive_interpretation"]
    result = optimize(scenario, entries)
    assert_plan_obeys_physics(scenario, entries, result.plan)