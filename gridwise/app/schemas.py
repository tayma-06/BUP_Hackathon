"""Parse and validate the POST /optimize-energy request body.

Two layers live here:

  * the pydantic models below exist ONLY so FastAPI exposes a real JSON request
    body and a structured JSON response in Swagger / OpenAPI (docs);

  * `parse_scenario` stays the single source of truth for validation. The
    pydantic models are deliberately lenient (extra="allow", no number bounds),
    and app/main.py routes FastAPI's body errors back through parse_scenario,
    so the exact Problem Statement status-code contract is preserved:
      400 -> malformed JSON or structurally invalid request (missing field, wrong type,
             wrong array length, duplicate hours, empty note, ...)
      422 -> well-formed but physically impossible numbers (negative demand,
             initial battery energy outside [minimum, capacity], ...)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

HOURS_PER_DAY = 24


class RequestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class HourData:
    hour: int
    demand: float
    solar: float
    tariff: float


@dataclass(frozen=True)
class Battery:
    capacity: float
    initial: float
    minimum: float
    max_charge: float
    max_discharge: float


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    notes: list[str]
    hours: list[HourData]  # always sorted, hours[h].hour == h
    battery: Battery


def _bad(message: str) -> RequestError:
    return RequestError(400, "invalid_request", message)


def _impossible(message: str) -> RequestError:
    return RequestError(422, "invalid_values", message)


def _number(obj: dict, key: str, where: str) -> float:
    if key not in obj:
        raise _bad(f"{where}: missing required field '{key}'")
    value = obj[key]
    # bool is a subclass of int in Python, so reject it explicitly
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _bad(f"{where}.{key} must be a number")
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise _bad(f"{where}.{key} must be finite") from None
    if not math.isfinite(value):
        raise _bad(f"{where}.{key} must be finite")
    return float(value)


def parse_scenario(body: object) -> Scenario:
    if not isinstance(body, dict):
        raise _bad("request body must be a JSON object")

    # ---- scenario_id ----
    if "scenario_id" not in body:
        raise _bad("missing required field 'scenario_id'")
    scenario_id = body["scenario_id"]
    if not isinstance(scenario_id, str):
        raise _bad("scenario_id must be a string")

    # ---- operator_notes: 1..3 non-empty strings ----
    notes = body.get("operator_notes")
    if not isinstance(notes, list):
        raise _bad("operator_notes must be an array of 1-3 strings")
    if not 1 <= len(notes) <= 3:
        raise _bad("operator_notes must contain 1-3 notes")
    for i, note in enumerate(notes):
        if not isinstance(note, str) or not note.strip():
            raise _bad(f"operator_notes[{i}] must be a non-empty string")

    # ---- hours: exactly 24 entries, each hour 0..23 exactly once ----
    raw_hours = body.get("hours")
    if not isinstance(raw_hours, list) or len(raw_hours) != HOURS_PER_DAY:
        raise _bad("hours must be an array of exactly 24 entries")
    by_hour: dict[int, HourData] = {}
    for i, item in enumerate(raw_hours):
        where = f"hours[{i}]"
        if not isinstance(item, dict):
            raise _bad(f"{where} must be an object")
        hour = item.get("hour")
        if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
            raise _bad(f"{where}.hour must be an integer from 0 to 23")
        if hour in by_hour:
            raise _bad(f"duplicate hour {hour} in hours")
        by_hour[hour] = HourData(
            hour=hour,
            demand=_number(item, "demand_kwh", where),
            solar=_number(item, "solar_kwh", where),
            tariff=_number(item, "tariff_bdt_per_kwh", where),
        )
    hours = [by_hour[h] for h in range(HOURS_PER_DAY)]

    # ---- battery ----
    raw_battery = body.get("battery")
    if not isinstance(raw_battery, dict):
        raise _bad("battery must be an object")
    battery = Battery(
        capacity=_number(raw_battery, "capacity_kwh", "battery"),
        initial=_number(raw_battery, "initial_energy_kwh", "battery"),
        minimum=_number(raw_battery, "minimum_energy_kwh", "battery"),
        max_charge=_number(raw_battery, "max_charge_kwh_per_hour", "battery"),
        max_discharge=_number(raw_battery, "max_discharge_kwh_per_hour", "battery"),
    )

    # ---- physical sanity (well-formed but impossible -> 422) ----
    for h in hours:
        if h.demand < 0 or h.solar < 0:
            raise _impossible(f"hour {h.hour}: demand_kwh and solar_kwh must be non-negative")
        if h.tariff < 0:
            raise _impossible(f"hour {h.hour}: tariff_bdt_per_kwh must be non-negative")
    b = battery
    if min(b.capacity, b.initial, b.minimum, b.max_charge, b.max_discharge) < 0:
        raise _impossible("battery values must be non-negative")
    if b.minimum > b.capacity:
        raise _impossible("battery.minimum_energy_kwh cannot exceed capacity_kwh")
    if not b.minimum <= b.initial <= b.capacity:
        raise _impossible("battery.initial_energy_kwh must be between minimum_energy_kwh and capacity_kwh")

    return Scenario(scenario_id=scenario_id, notes=list(notes), hours=hours, battery=battery)


class OptimizationRequest(BaseModel):
    """POST /optimize-energy request body, as shown in OpenAPI /docs.

    Deliberately lenient (extra keys / any nested shapes are allowed):
    FastAPI only needs a well-formed JSON object here. The exact Problem
    Statement contract (400 vs 422) is enforced by `parse_scenario`, which
    the endpoint re-runs on the raw body, and by the RequestValidationError
    handler in app/main.py.
    """

    model_config = ConfigDict(extra="allow")

    scenario_id: str
    operator_notes: list[str]
    hours: list[dict[str, Any]]
    battery: dict[str, Any]


class OptimizationResponse(BaseModel):
    """POST /optimize-energy success response, as shown in OpenAPI /docs.

    hourly_plan / directive_interpretation are kept as bare lists so their
    exact nested content is passed through unmodified.
    """

    model_config = ConfigDict(extra="allow")

    scenario_id: str
    directive_interpretation: list
    hourly_plan: list
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
