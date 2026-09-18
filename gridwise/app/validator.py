"""Independent replay validator - mirrors what the judge says it checks.

Used in three places:
  * the API rejects any plan that fails replay before returning it
  * tests/ verify the optimizer against the public samples
  * scripts/run_public_samples.py replays a live response against the
    organizer's ground-truth directives (exactly like the hidden judge)
"""

from __future__ import annotations

import math

from .guardrails import ALLOWED_TYPES
from .schemas import Scenario

H = 24

ADJUSTMENT_KEYS = {
    "solar_reduction": {"hours", "factor"},
    "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
    "no_charge_window": {"hours"},
    "no_discharge_window": {"hours"},
    "max_grid_window": {"hours", "max_grid_kwh"},
}
RESPONSE_KEYS = {
    "scenario_id", "directive_interpretation", "hourly_plan",
    "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary",
}
PLAN_KEYS = {"hour", "grid_kwh", "solar_used_kwh", "battery_action", "battery_kwh", "battery_energy_after_kwh"}
ENTRY_KEYS = {"note_index", "applies", "directive_type", "structured_adjustment", "explanation"}


def _is_num(value: object) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def check_interpretation_schema(note_count: int, entries: object, capacity: float | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(entries, list) or len(entries) != note_count:
        return [f"directive_interpretation must have exactly {note_count} entries"]
    for i, entry in enumerate(entries):
        where = f"directive_interpretation[{i}]"
        if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
            errors.append(f"{where}: keys must be exactly {sorted(ENTRY_KEYS)}")
            continue
        if type(entry["note_index"]) is not int or entry["note_index"] != i:
            errors.append(f"{where}: note_index must be {i}")
        kind = entry["directive_type"]
        if kind not in ALLOWED_TYPES:
            errors.append(f"{where}: unsupported directive_type {kind!r}")
            continue
        if not isinstance(entry["explanation"], str):
            errors.append(f"{where}: explanation must be a string")
        if kind == "no_op":
            if entry["applies"] is not False or entry["structured_adjustment"] is not None:
                errors.append(f"{where}: no_op needs applies=false and structured_adjustment=null")
            continue
        if entry["applies"] is not True:
            errors.append(f"{where}: non-no_op directives need applies=true")
        adj = entry["structured_adjustment"]
        if not isinstance(adj, dict) or set(adj) != ADJUSTMENT_KEYS[kind]:
            errors.append(f"{where}: structured_adjustment keys must be {sorted(ADJUSTMENT_KEYS[kind])}")
            continue
        hours = adj["hours"]
        if (
            not isinstance(hours, list)
            or not hours
            or any(isinstance(h, bool) or not isinstance(h, int) or not 0 <= h <= 23 for h in hours)
            or hours != sorted(set(hours))
        ):
            errors.append(f"{where}: hours must be unique ascending integers 0-23")
        for key in ADJUSTMENT_KEYS[kind] - {"hours"}:
            if not _is_num(adj[key]) or adj[key] < 0:
                errors.append(f"{where}: {key} must be a finite non-negative number")
        if kind == "solar_reduction" and _is_num(adj.get("factor")) and adj["factor"] > 1:
            errors.append(f"{where}: factor must be <= 1")
        if kind == "minimum_battery_reserve" and capacity is not None and _is_num(adj.get("minimum_energy_kwh")) and adj["minimum_energy_kwh"] > capacity:
            errors.append(f"{where}: reserve exceeds battery capacity")
    return errors


def replay_plan(scenario: Scenario, directives: list[dict], response: dict, tol: float = 0.01) -> list[str]:
    """Replay hourly_plan against the given directives + GridWise rules. Returns a list of problems."""
    errors: list[str] = []
    if not isinstance(response, dict):
        return ["response is not a JSON object"]
    if set(response) != RESPONSE_KEYS:
        errors.append("response fields do not match the required schema")
    if not isinstance(response.get("plan_summary"), str):
        errors.append("plan_summary must be a string")
    errors += check_interpretation_schema(len(scenario.notes), response.get("directive_interpretation"), scenario.battery.capacity)
    directive_errors = check_interpretation_schema(len(scenario.notes), directives, scenario.battery.capacity)
    if directive_errors:
        return errors + directive_errors
    if response.get("scenario_id") != scenario.scenario_id:
        errors.append("scenario_id does not match the request")
    plan = response.get("hourly_plan")
    if not isinstance(plan, list) or len(plan) != H:
        return errors + ["hourly_plan must have exactly 24 entries"]
    rows = {}
    for row in plan:
        if not isinstance(row, dict) or set(row) != PLAN_KEYS:
            return errors + [f"hourly_plan rows must have exactly the keys {sorted(PLAN_KEYS)}"]
        h = row["hour"]
        if type(h) is not int or h not in range(H) or h in rows:
            return errors + ["hourly_plan must contain unique integer hours 0..23"]
        rows[h] = row
    if set(rows) != set(range(H)):
        return errors + ["hourly_plan must contain each hour 0..23 exactly once"]

    b = scenario.battery
    # Independent derivation: intentionally do NOT reuse the optimizer's limit builder.
    solar_limit = [hour.solar for hour in scenario.hours]
    grid_limit = [None] * H
    charge_limit = [b.max_charge] * H
    discharge_limit = [b.max_discharge] * H
    reserve = [b.minimum] * H
    for entry in directives:
        if not entry["applies"]:
            continue
        adj = entry["structured_adjustment"]
        for h in adj["hours"]:
            kind = entry["directive_type"]
            if kind == "solar_reduction":
                solar_limit[h] *= adj["factor"]
            elif kind == "minimum_battery_reserve":
                reserve[h] = max(reserve[h], adj["minimum_energy_kwh"])
            elif kind == "no_charge_window":
                charge_limit[h] = 0
            elif kind == "no_discharge_window":
                discharge_limit[h] = 0
            elif kind == "max_grid_window":
                grid_limit[h] = adj["max_grid_kwh"] if grid_limit[h] is None else min(grid_limit[h], adj["max_grid_kwh"])
    energy = b.initial
    total_grid = total_cost = peak = 0.0
    for h in range(H):
        row, hour = rows[h], scenario.hours[h]
        numbers = ("grid_kwh", "solar_used_kwh", "battery_kwh", "battery_energy_after_kwh")
        if not all(_is_num(row[k]) for k in numbers):
            errors.append(f"hour {h}: non-numeric value")
            continue
        if any(row[k] < -tol for k in numbers):
            errors.append(f"hour {h}: negative value")
        action, amount = row["battery_action"], row["battery_kwh"]
        if action not in ("charge", "discharge", "idle"):
            errors.append(f"hour {h}: invalid battery_action {action!r}")
            continue
        if action == "idle" and abs(amount) > tol:
            errors.append(f"hour {h}: idle but battery_kwh={amount}")
        charge = amount if action == "charge" else 0.0
        discharge = amount if action == "discharge" else 0.0
        if charge > charge_limit[h] + tol:
            errors.append(f"hour {h}: charge exceeds allowed limit")
        if discharge > discharge_limit[h] + tol:
            errors.append(f"hour {h}: discharge exceeds allowed limit")
        energy = energy + charge - discharge
        if abs(row["battery_energy_after_kwh"] - energy) > tol:
            errors.append(f"hour {h}: battery_energy_after_kwh {row['battery_energy_after_kwh']:g} != replayed {energy:g}")
        if energy < reserve[h] - tol or energy > b.capacity + tol:
            errors.append(f"hour {h}: battery energy outside reserve/capacity bounds")
        if row["solar_used_kwh"] > solar_limit[h] + tol:
            errors.append(f"hour {h}: solar used exceeds effective solar")
        balance = row["grid_kwh"] + row["solar_used_kwh"] + discharge - hour.demand - charge
        if abs(balance) > tol:
            errors.append(f"hour {h}: energy balance off by {balance:g}")
        cap = grid_limit[h]
        if cap is not None and row["grid_kwh"] > cap + tol:
            errors.append(f"hour {h}: grid {row['grid_kwh']:g} exceeds cap {cap:g}")
        total_grid += row["grid_kwh"]
        total_cost += row["grid_kwh"] * hour.tariff
        peak = max(peak, row["grid_kwh"])

    if abs(energy - b.initial) > tol:
        errors.append(f"end-of-day battery {energy:g} != initial {b.initial:g}")
    for key, value in (("total_grid_kwh", total_grid), ("total_cost_bdt", total_cost), ("peak_grid_kwh", peak)):
        reported = response.get(key)
        if not _is_num(reported) or abs(reported - value) > tol:
            errors.append(f"{key} {reported} != recalculated {value:g}")
    return errors
